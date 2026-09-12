"""Неизменяемые policy и отчёты локальной JSON Schema validation."""

import json
from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, StrictStr, model_validator

from ._base import FrozenContract
from .common import FingerprintStr, NonNegativeInt, ValidationIssue

type JsonPathPart = StrictStr | NonNegativeInt


class JsonSchemaPolicy(FrozenContract):
    """Trusted finite budgets; внешние URI никогда не разрешают retrieval."""

    allowed_resource_uris: tuple[StrictStr, ...] = Field(default=(), max_length=32)
    max_depth: Annotated[StrictInt, Field(ge=1, le=48)] = 24
    max_nodes: Annotated[StrictInt, Field(ge=1, le=16384)] = 4096
    max_properties: Annotated[StrictInt, Field(ge=1, le=2048)] = 256
    max_bytes: Annotated[StrictInt, Field(ge=1, le=4_194_304)] = 262144
    max_string_chars: Annotated[StrictInt, Field(ge=1, le=65536)] = 4096
    max_numeric_digits: Annotated[StrictInt, Field(ge=1, le=512)] = 128
    max_decimal_exponent: Annotated[StrictInt, Field(ge=1, le=2048)] = 512
    max_regex_chars: Annotated[StrictInt, Field(ge=1, le=256)] = 128
    max_evaluations: Annotated[StrictInt, Field(ge=1, le=1_000_000)] = 100000
    max_evaluation_depth: Annotated[StrictInt, Field(ge=1, le=96)] = 64
    max_issues: Annotated[StrictInt, Field(ge=1, le=4096)] = 256
    max_cache_entries: Annotated[StrictInt, Field(ge=0, le=128)] = 16
    max_cache_bytes: Annotated[StrictInt, Field(ge=0, le=33_554_432)] = 4_194_304

    @model_validator(mode="after")
    def unique_resources(self) -> Self:
        if len(set(self.allowed_resource_uris)) != len(self.allowed_resource_uris):
            raise ValueError("Resource allowlist должна быть уникальной")
        for uri in self.allowed_resource_uris:
            if not _local_uri(uri):
                raise ValueError("Допустим только локальный schema URN")
        return self


def _local_uri(uri: str) -> bool:
    prefix = "urn:structuraguard:schema:"
    suffix = uri.removeprefix(prefix)
    return (
        uri.startswith(prefix)
        and 0 < len(suffix) <= 128
        and all(c.isascii() and (c.isalnum() or c in "._-") for c in suffix)
    )


class JsonSchemaResource(FrozenContract):
    """Явно переданная локальная схема; JSON не является адресом для загрузки."""

    uri: StrictStr = Field(max_length=256, repr=False)
    document_json: StrictStr = Field(max_length=4_194_304, repr=False)


class JsonSchemaIssue(FrozenContract):
    """Diagnostic code и пути; имена ключей могут содержать restricted data."""

    issue: ValidationIssue
    phase: Literal["schema", "instance"]
    instance_path: tuple[JsonPathPart, ...] = Field(
        default=(), max_length=128, repr=False
    )
    schema_path: tuple[JsonPathPart, ...] = Field(
        default=(), max_length=256, repr=False
    )
    resource_uri: StrictStr = Field(default="", max_length=256, repr=False)

    @property
    def code(self) -> str:
        """Вернуть machine-readable code вложенного ValidationIssue без I/O."""
        return self.issue.code

    @property
    def json_pointer(self) -> str:
        """RFC 6901 pointer к instance, либо к схеме в phase=schema."""
        return "".join(
            "/" + str(p).replace("~", "~0").replace("/", "~1")
            for p in self.instance_path
        )

    @property
    def json_path(self) -> str:
        """Однозначная bracket notation без интерпретации имён как выражений."""
        return "$" + "".join(
            f"[{json.dumps(p, ensure_ascii=True)}]" for p in self.instance_path
        )


class JsonSchemaResult(FrozenContract):
    """Полный bounded отчёт; при budget failure результат не выдаётся."""

    schema_valid: bool
    schema_fingerprint: FingerprintStr
    policy_fingerprint: FingerprintStr
    issues: tuple[JsonSchemaIssue, ...] = Field(default=(), max_length=4096, repr=False)

    @property
    def accepted(self) -> bool:
        """Вернуть True только при корректной схеме и отсутствии schema/instance issues."""
        return self.schema_valid and not self.issues


class JsonSchemaCacheInfo(FrozenContract):
    """Счётчики instance-owned LRU; содержимое схем не раскрывается."""

    entries: NonNegativeInt
    bytes: NonNegativeInt
    hits: NonNegativeInt
    misses: NonNegativeInt
    evictions: NonNegativeInt
