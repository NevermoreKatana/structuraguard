"""Проверки typed DB values без coercion, округления или вычисления CHECK SQL."""

from decimal import Decimal
from uuid import UUID

from structuraguard.contracts.common import (
    BooleanScalar,
    DateScalar,
    DateTimeScalar,
    DecimalScalar,
    IntegerScalar,
    NormalizedScalar,
    NullScalar,
    NumberScalar,
    StringScalar,
)
from structuraguard.contracts.database import ColumnCatalog, DatabaseType


def field_codes(
    column: ColumnCatalog, value: NormalizedScalar | None, dialect: str
) -> tuple[str, ...]:
    info = column.inspection
    if info is None:
        return ("DB_CONSTRAINT_UNVERIFIED",)
    chain: list[DatabaseType] = []
    dt = info.data_type
    chain.append(dt)
    while dt.type_kind == "domain" and dt.base_type is not None:
        dt = dt.base_type
        chain.append(dt)
    required = not column.nullable or any(t.domain_not_null for t in chain)
    if value is None:
        default = (
            info.default is not None
            or info.identity is not None
            or info.rowid_alias
            or column.generated
            or any(t.domain_default is not None for t in chain)
        )
        return ("DB_NOT_NULL",) if required and not default else ()
    if isinstance(value, NullScalar):
        return ("DB_NOT_NULL",) if required else ()
    code: list[str] = []
    expected = dt.canonical_type
    matches = {
        "integer": isinstance(value, IntegerScalar),
        "decimal": isinstance(value, IntegerScalar | DecimalScalar),
        "float": isinstance(value, NumberScalar),
        "text": isinstance(value, StringScalar),
        "boolean": isinstance(value, BooleanScalar),
        "date": isinstance(value, DateScalar),
        "datetime": isinstance(value, DateTimeScalar),
        "uuid": isinstance(value, StringScalar),
    }
    if expected not in matches:
        return (*code, "DB_CONSTRAINT_UNVERIFIED")
    if not matches[expected]:
        return (*code, "DB_TYPE_MISMATCH")
    if isinstance(value, StringScalar):
        if "\x00" in value.value and dialect == "postgresql":
            code.append("DB_VALUE_INVALID")
        if dt.length is not None and len(value.value) > dt.length:
            # SQLite не enforce VARCHAR(n); это намеренная conservative guard.
            code.append("DB_LENGTH")
        if dt.type_kind == "enum" and value.value not in dt.enum_labels:
            code.append("DB_ENUM")
        if expected == "uuid":
            try:
                valid = str(UUID(value.value)) == value.value
            except ValueError:
                valid = False
            if not valid:
                code.append("DB_VALUE_INVALID")
    if isinstance(value, IntegerScalar) and expected == "integer":
        bits = {
            "smallint": 16,
            "int2": 16,
            "integer": 32,
            "int4": 32,
            "bigint": 64,
            "int8": 64,
        }.get(dt.native_type.casefold())
        if dialect == "sqlite":
            bits = 64
        if bits is None:
            code.append("DB_CONSTRAINT_UNVERIFIED")
        elif not -(2 ** (bits - 1)) <= value.value < 2 ** (bits - 1):
            code.append("DB_NUMERIC_BOUNDS")
    if isinstance(value, IntegerScalar | DecimalScalar) and expected == "decimal":
        number = Decimal(value.value)
        if dt.precision is not None and dt.scale is not None:
            if number and number.copy_abs().adjusted() >= dt.precision - dt.scale:
                code.append("DB_NUMERIC_BOUNDS")
            parts = number.as_tuple()
            exponent = parts.exponent
            assert isinstance(exponent, int)
            trailing = 0
            for digit in reversed(parts.digits):
                if digit != 0:
                    break
                trailing += 1
            if number and exponent + trailing < -dt.scale:
                code.append("DB_NUMERIC_SCALE")
        if dialect == "sqlite":
            code.append("DB_CONSTRAINT_UNVERIFIED")
    if (
        isinstance(value, NumberScalar)
        and expected == "float"
        and dt.native_type.casefold() in ("real", "float4")
        and abs(value.value) > float.fromhex("0x1.fffffep+127")
    ):
        code.append("DB_NUMERIC_BOUNDS")
    # Catalog пока не моделирует temporal precision. Сравнение полного datetime
    # до typmod rounding может пропустить UNIQUE-конфликт или потерю точности.
    if isinstance(value, DateTimeScalar) and (
        dt.timezone is not True
        or (
            dialect == "postgresql"
            and dt.native_type.casefold()
            not in ("timestamp with time zone", "timestamptz")
        )
    ):
        code.append("DB_CONSTRAINT_UNVERIFIED")
    if dt.type_kind in ("unknown", "array"):
        code.append("DB_CONSTRAINT_UNVERIFIED")
    return tuple(code)
