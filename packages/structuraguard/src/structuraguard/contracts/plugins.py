"""Декларативные контракты безопасного discovery parser plugins."""

from __future__ import annotations

import re
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AfterValidator,
    Field,
    StrictInt,
    StrictStr,
    StringConstraints,
    model_validator,
)

from structuraguard.contracts._base import FrozenContract, canonical_sha256_value
from structuraguard.contracts.common import (
    FingerprintStr,
    ParserIdentifierStr,
    SchemaVersionStr,
    _safe_text,
)

PARSER_ENTRY_POINT_GROUP: Final = "structuraguard.parsers"

_AUTO_FINGERPRINT: Final = "sha256:" + "0" * 64
_DISTRIBUTION_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DISTRIBUTION_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}$")
_PYTHON_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


def _canonical_distribution_name(value: str) -> str:
    if _DISTRIBUTION_NAME.fullmatch(value) is None:
        raise ValueError("Distribution name должен быть в canonical PEP 503 form")
    return value


def _safe_distribution_version(value: str) -> str:
    if _DISTRIBUTION_VERSION.fullmatch(value) is None:
        raise ValueError("Distribution version содержит недопустимые символы")
    return value


def _python_path(value: str) -> str:
    if _PYTHON_PATH.fullmatch(value) is None:
        raise ValueError("Entry-point path должен содержать Python identifiers")
    return value


DistributionNameStr = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=255),
    AfterValidator(_canonical_distribution_name),
    AfterValidator(_safe_text),
]
DistributionVersionStr = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=128),
    AfterValidator(_safe_distribution_version),
    AfterValidator(_safe_text),
]
PythonPathStr = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=512),
    AfterValidator(_python_path),
    AfterValidator(_safe_text),
]


class ParserPluginDescriptor(FrozenContract):
    """Неисполняемое описание обнаруженного parser plugin.

    Args:
        schema_version: Версия wire-контракта descriptor.
        adapter_id: Канонический идентификатор parser adapter.
        distribution_name: Имя distribution в канонической форме PEP 503.
        distribution_version: Проверенная версия distribution.
        module: Python module из entry point без его импорта.
        attribute: Python attribute из entry point без его разрешения.
        entry_point_group: Фиксированная группа ``structuraguard.parsers``.
        metadata_fingerprint: Canonical SHA-256 полей descriptor. Если значение
            не передано, fingerprint вычисляется при создании модели.

    Raises:
        pydantic.ValidationError: Если поле небезопасно, имеет неверный формат
            или переданный fingerprint не соответствует содержимому.

    Создание descriptor не импортирует plugin, не выполняет его код и не делает
    I/O. ``metadata_fingerprint`` подтверждает только эти метаданные, а не байты
    установленного distribution.
    """

    schema_version: SchemaVersionStr = "1.0.0"
    adapter_id: ParserIdentifierStr
    distribution_name: DistributionNameStr
    distribution_version: DistributionVersionStr
    module: PythonPathStr
    attribute: PythonPathStr
    entry_point_group: Literal["structuraguard.parsers"] = PARSER_ENTRY_POINT_GROUP
    metadata_fingerprint: FingerprintStr = _AUTO_FINGERPRINT

    @model_validator(mode="after")
    def _validate_descriptor(self) -> Self:
        if len(self.module) + 1 + len(self.attribute) > 512:
            raise ValueError("Entry-point target превышает 512 символов")
        expected = self.content_fingerprint()
        if (
            "metadata_fingerprint" not in self.model_fields_set
            and self.metadata_fingerprint == _AUTO_FINGERPRINT
        ):
            object.__setattr__(self, "metadata_fingerprint", expected)
        elif self.metadata_fingerprint != expected:
            raise ValueError("Plugin metadata fingerprint не соответствует descriptor")
        return self

    def content_fingerprint(self) -> str:
        """Вычислить canonical SHA-256 descriptor без поля fingerprint.

        Returns:
            Fingerprint в форме ``sha256:<lowercase-hex>``.

        Метод детерминирован и не выполняет I/O.
        """

        return canonical_sha256_value(
            self,
            exclude_top_level=frozenset({"metadata_fingerprint"}),
        )

    def validate_content_fingerprint(self) -> None:
        """Проверить fingerprint после возможного обхода обычной валидации.

        Raises:
            ValueError: Если descriptor был изменён без пересчёта fingerprint.

        Метод не импортирует и не активирует plugin.
        """

        if self.metadata_fingerprint != self.content_fingerprint():
            raise ValueError("Plugin metadata fingerprint не соответствует descriptor")


class ParserDiscoveryPolicy(FrozenContract):
    """Ограниченная allowlist policy одного явного вызова discovery.

    Args:
        allowed_distributions: От одного до 64 уникальных имён distributions в
            канонической форме PEP 503. Модель сохраняет их в сортированном виде.
        max_entries: Максимальное число принимаемых descriptors, от 1 до 256.

    Raises:
        pydantic.ValidationError: Если allowlist пуста, содержит duplicates или
            небезопасные имена либо если ``max_entries`` выходит за пределы.

    Создание policy не сканирует окружение и не запускает discovery.
    """

    allowed_distributions: Annotated[
        tuple[DistributionNameStr, ...],
        Field(min_length=1, max_length=64),
    ]
    max_entries: Annotated[StrictInt, Field(ge=1, le=256)] = 256

    @model_validator(mode="after")
    def _canonicalize_allowlist(self) -> Self:
        if len(self.allowed_distributions) != len(set(self.allowed_distributions)):
            raise ValueError("Distribution allowlist не должна содержать duplicates")
        object.__setattr__(
            self,
            "allowed_distributions",
            tuple(sorted(self.allowed_distributions)),
        )
        return self
