"""Локальный trusted registry prompts и закрытых Pydantic response schemas."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

from jsonschema import Draft202012Validator
from pydantic import Field, StrictStr
from referencing import Registry

from structuraguard.contracts._base import (
    CanonicalValue,
    FrozenContract,
    canonical_sha256_value,
)
from structuraguard.contracts.common import ParserIdentifierStr, SchemaVersionStr
from structuraguard.contracts.llm import LLMErrorCode, LLMPrompt
from structuraguard.exceptions import LLMProviderError


class LLMPromptTemplate(FrozenContract):
    """Trusted system instruction; payload никогда не интерполируется в text."""

    prompt_id: ParserIdentifierStr
    version: SchemaVersionStr
    text: StrictStr = Field(min_length=1, max_length=16384, repr=False, exclude=True)

    @property
    def identity(self) -> LLMPrompt:
        """Hash точных UTF-8 bytes шаблона, без system/user envelope."""
        from hashlib import sha256

        return LLMPrompt(
            prompt_id=self.prompt_id,
            version=self.version,
            fingerprint="sha256:" + sha256(self.text.encode()).hexdigest(),
        )


def _check_schema(node: object) -> None:
    if isinstance(node, dict):
        if "$ref" in node and (
            not isinstance(node["$ref"], str) or not node["$ref"].startswith("#/$defs/")
        ):
            raise ValueError("Response schema запрещает внешние references")
        if node.get("type") == "object":
            properties = node.get("properties", {})
            if (
                node.get("additionalProperties") is not False
                or not isinstance(properties, dict)
                or set(node.get("required", [])) != set(properties)
            ):
                raise ValueError(
                    "Response schema требует закрытые objects без optional defaults"
                )
        for value in node.values():
            _check_schema(value)
    elif isinstance(node, list):
        for value in node:
            _check_schema(value)


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMResponseSchema:
    """Trusted DTO type из code registry; JSON Schema не загружается из ответа.

    Используются закрытые objects с required fields, без default filling.
    Nullable required fields разрешены. Validators модели — trusted code.
    Optional decoding_projector — также trusted code из immutable registry:
    получает отдельную schema и approved payload, меняет только native grammar.
    JSON-object fallback сохраняет статическую schema без source data в system.
    Изменение projector требует новой version factory; validate всегда использует
    исходную модель независимо от grammar и ответа provider.
    """

    schema_id: str
    version: str
    model: type[FrozenContract] = field(repr=False)
    decoding_projector: Callable[[dict[str, object], str], dict[str, object]] | None = (
        field(default=None, repr=False)
    )
    schema_json: str = field(init=False, repr=False)
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        LLMPrompt(
            prompt_id=self.schema_id,
            version=self.version,
            fingerprint="sha256:" + "0" * 64,
        )
        if not isinstance(self.model, type) or not issubclass(
            self.model, FrozenContract
        ):
            raise ValueError("Response model должен наследовать FrozenContract")
        if self.decoding_projector is not None and not callable(
            self.decoding_projector
        ):
            raise ValueError("Decoding projector должен быть trusted callable")
        schema = self.model.model_json_schema()
        _check_schema(schema)
        if schema.get("type") != "object":
            raise ValueError("Response schema требует root object")
        # Декодеры строят последовательную грамматику по порядку properties.
        # Сортировка ставила confidence/delimiter раньше source_id/operation.
        # Для identity остаётся канонический hash, для генерации — порядок DTO.
        encoded = json.dumps(
            schema, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        if len(encoded.encode()) > 65536:
            raise ValueError("Response schema превышает byte limit")
        object.__setattr__(self, "schema_json", encoded)
        object.__setattr__(self, "fingerprint", canonical_sha256_value(schema))

    def prepare_decoding(
        self, payload_json: str | None = None
    ) -> "_PreparedResponseSchema":
        """Проверить bounded grammar на отдельной копии, не менять validator/registry."""
        original = parse_object(self.schema_json)
        if self.decoding_projector is None or payload_json is None:
            return _PreparedResponseSchema(self, self.schema_json, self.fingerprint)
        try:
            projected = self.decoding_projector(original, payload_json)
            encoded = json.dumps(
                projected, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )
            size = len(encoded.encode())
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            # Callback может включить private source/credentials в exception.
            raise LLMProviderError(LLMErrorCode.REQUEST_INVALID) from None
        if size > 65536:
            raise LLMProviderError(LLMErrorCode.CONTEXT_LIMIT)
        try:
            # Ещё одна копия не позволяет callback удержать mutable wire schema.
            checked = parse_object(encoded)
            if checked.get("type") != "object":
                raise ValueError
            _check_schema(checked)
            Draft202012Validator.check_schema(checked)
            prepared = _PreparedResponseSchema(
                self,
                encoded,
                canonical_sha256_value(cast(CanonicalValue, checked)),
                Draft202012Validator(checked, registry=Registry()),
            )
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise LLMProviderError(LLMErrorCode.REQUEST_INVALID) from None
        return prepared

    def validate(self, content: str) -> None:
        """Проверить типы/extra fields без coercion, repair и выполнения вывода."""
        try:
            self.model.model_validate_json(content, strict=True)
        except (ValueError, TypeError, RecursionError):
            raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION) from None


@dataclass(frozen=True, slots=True)
class _PreparedResponseSchema:
    """Run-local grammar и независимый исходный DTO; registry остаётся неизменным."""

    original: LLMResponseSchema = field(repr=False)
    schema_json: str = field(repr=False)
    fingerprint: str
    validator: Draft202012Validator | None = field(default=None, repr=False)

    def validate(self, content: str) -> None:
        self.original.validate(content)
        if self.validator is None:
            return
        try:
            if not self.validator.is_valid(parse_object(content)):
                raise ValueError
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION) from None


def parse_object(raw: bytes | str) -> dict[str, object]:
    """Отвергнуть duplicate keys, NaN, массивы и trailing bytes до DTO parsing."""

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def constant(value: str) -> object:
        raise ValueError

    try:
        value: object = json.loads(
            raw, object_pairs_hook=pairs, parse_constant=constant
        )
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError, RecursionError):
        raise LLMProviderError(LLMErrorCode.INVALID_RESPONSE) from None
