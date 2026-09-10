"""Чистая нормализация DB types и структурных identifiers без SQLAlchemy."""

from __future__ import annotations

import re
from typing import Literal

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.database import DatabaseType


def catalog_identifier(kind: str, *parts: str) -> str:
    """Получить stable ID из структурного пути без I/O.

    Args:
        kind: Категория объекта и префикс ID.
        parts: Упорядоченные компоненты пути; case и границы строк сохраняются.

    Returns:
        ``kind:`` и SHA-256 JSON-представления пути. Это идентификатор metadata,
        а не schema fingerprint, проверка полномочий или шифрование данных.
    """
    return f"{kind}:{canonical_sha256_value([kind, *parts]).removeprefix('sha256:')}"


def normalize_type(
    native_type: str, *, dialect: Literal["generic", "sqlite"] = "generic"
) -> DatabaseType:
    """Нормализовать объявленный тип без проверки значений или строгой типизации.

    Неизвестные типы остаются unknown. SQLite affinity — отдельная характеристика,
    а не доказательство storage type каждой строки. PostgreSQL-specific types
    должны обогащаться реальной reflection в отдельном adapter.

    Args:
        native_type: Объявленный тип длиной до 4096 символов.
        dialect: ``generic`` для переносимой семьи или ``sqlite`` для affinity.

    Returns:
        DatabaseType с исходным native name, canonical family и известными
        length/precision/scale/timezone. Неизвестный тип имеет семью ``unknown``.

    Raises:
        pydantic.ValidationError: Недопустимый текст или параметры типа.

    Side effects:
        Нет I/O, исполнения SQL и чтения значений. SQLite affinity не доказывает
        фактический тип данных; STRICT ANY отдельно уточняет SQLite adapter.
    """
    DatabaseType(native_type=native_type, canonical_type="unknown")
    normalized = " ".join(native_type.upper().split())
    match = re.fullmatch(
        r"([A-Z ]+?)(?:\(\s*(\d+)\s*(?:,\s*(-?\d+)\s*)?\))?"
        r"( (?:WITH|WITHOUT) TIME ZONE)?",
        normalized,
    )
    base = match[1] + (match[4] or "") if match else normalized
    canonical: Literal[
        "integer",
        "decimal",
        "float",
        "text",
        "binary",
        "boolean",
        "date",
        "time",
        "datetime",
        "json",
        "uuid",
        "unknown",
    ] = "unknown"
    if base in {"INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT"}:
        canonical = "integer"
    elif base in {"NUMERIC", "DECIMAL", "NUMBER"}:
        canonical = "decimal"
    elif base in {"FLOAT", "REAL", "DOUBLE", "DOUBLE PRECISION"}:
        canonical = "float"
    elif base in {
        "TEXT",
        "CHAR",
        "CHARACTER",
        "VARCHAR",
        "CHARACTER VARYING",
        "CLOB",
        "NCHAR",
        "NVARCHAR",
    }:
        canonical = "text"
    elif base in {"BLOB", "BINARY", "VARBINARY", "BYTEA"}:
        canonical = "binary"
    elif base in {"BOOL", "BOOLEAN"}:
        canonical = "boolean"
    elif base == "DATE":
        canonical = "date"
    elif base in {"TIME", "TIME WITH TIME ZONE", "TIME WITHOUT TIME ZONE"}:
        canonical = "time"
    elif base in {
        "DATETIME",
        "TIMESTAMP",
        "TIMESTAMP WITH TIME ZONE",
        "TIMESTAMP WITHOUT TIME ZONE",
    }:
        canonical = "datetime"
    elif base in {"JSON", "JSONB"}:
        canonical = "json"
    elif base == "UUID":
        canonical = "uuid"

    size = int(match[2]) if match and match[2] else None
    scale = int(match[3]) if match and match[3] else None
    timezone = None
    if canonical in {"time", "datetime"}:
        if base.endswith("WITH TIME ZONE"):
            timezone = True
        elif base.endswith("WITHOUT TIME ZONE"):
            timezone = False
    affinity: Literal["integer", "decimal", "float", "text", "binary"] | None = None
    if dialect == "sqlite":
        if "INT" in normalized:
            affinity = "integer"
        elif any(part in normalized for part in ("CHAR", "CLOB", "TEXT")):
            affinity = "text"
        elif not normalized or "BLOB" in normalized:
            affinity = "binary"
        elif any(part in normalized for part in ("REAL", "FLOA", "DOUB")):
            affinity = "float"
        else:
            affinity = "decimal"
    return DatabaseType(
        native_type=native_type,
        canonical_type=canonical,
        length=size if canonical in {"text", "binary"} else None,
        precision=size if canonical == "decimal" else None,
        scale=scale if canonical == "decimal" else None,
        timezone=timezone,
        affinity=affinity,
    )
