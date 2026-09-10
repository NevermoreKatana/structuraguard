"""Локальный trusted registry prompts и закрытых Pydantic response schemas."""

import json
from dataclasses import dataclass, field

from pydantic import Field, StrictStr

from structuraguard.contracts._base import (
    FrozenContract,
    canonical_json_value,
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
    """

    schema_id: str
    version: str
    model: type[FrozenContract] = field(repr=False)
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
        schema = self.model.model_json_schema()
        _check_schema(schema)
        if schema.get("type") != "object":
            raise ValueError("Response schema требует root object")
        encoded = canonical_json_value(schema)
        if len(encoded.encode()) > 65536:
            raise ValueError("Response schema превышает byte limit")
        object.__setattr__(self, "schema_json", encoded)
        object.__setattr__(self, "fingerprint", canonical_sha256_value(schema))

    def validate(self, content: str) -> None:
        """Проверить типы/extra fields без coercion, repair и выполнения вывода."""
        try:
            self.model.model_validate_json(content, strict=True)
        except (ValueError, TypeError, RecursionError):
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
