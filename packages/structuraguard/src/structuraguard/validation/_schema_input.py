"""Bounded копирование JSON до library traversal и serialization."""

import hashlib
import json
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Never

from structuraguard.contracts.json_schema import JsonSchemaPolicy
from structuraguard.exceptions import ValidationError

type Json = bool | str | int | Decimal | list[Json] | dict[str, Json] | None


def fail(code: str = "JSON_SCHEMA_LIMIT_EXCEEDED") -> Never:
    raise ValidationError(
        error_code=code, message="JSON Schema validation не завершена"
    ) from None


@dataclass
class Intake:
    """Совокупный бюджет одного schema bundle либо instance."""

    policy: JsonSchemaPolicy
    nodes: int = 0
    size: int = 0

    def copy(self, value: object, depth: int = 0) -> Json:
        self.nodes += 1
        self.size += 8
        if self.nodes > self.policy.max_nodes or depth > self.policy.max_depth:
            fail()
        result: Json
        if value is None or type(value) is bool:
            result = value
        elif type(value) is str:
            if len(value) > self.policy.max_string_chars:
                fail()
            try:
                self.size += len(value.encode("utf-8"))
            except UnicodeError:
                fail("JSON_SCHEMA_INPUT_INVALID")
            result = value
        elif type(value) is int:
            if value.bit_length() > self.policy.max_numeric_digits * 4:
                fail()
            digits = len(str(abs(value)))
            if digits > self.policy.max_numeric_digits:
                fail()
            self.size += digits
            result = value
        elif type(value) is float or type(value) is Decimal:
            if isinstance(value, float):
                if not math.isfinite(value):
                    fail("JSON_SCHEMA_INPUT_INVALID")
                number = Decimal(str(value))
            elif isinstance(value, Decimal):
                number = value
            else:
                fail("JSON_SCHEMA_INPUT_INVALID")
            if not number.is_finite():
                fail("JSON_SCHEMA_INPUT_INVALID")
            parts = number.as_tuple()
            if (
                len(parts.digits) > self.policy.max_numeric_digits
                or not isinstance(parts.exponent, int)
                or abs(parts.exponent) > self.policy.max_decimal_exponent
            ):
                fail()
            self.size += len(parts.digits) + 16
            result = number
        elif type(value) is list:
            if len(value) > self.policy.max_nodes - self.nodes:
                fail()
            result = [self.copy(item, depth + 1) for item in value]
        elif type(value) is dict:
            if len(value) > self.policy.max_properties:
                fail()
            result = {}
            if any(type(key) is not str for key in value):
                fail("JSON_SCHEMA_INPUT_INVALID")
            for key, item in sorted(value.items()):
                if type(key) is not str:
                    fail("JSON_SCHEMA_INPUT_INVALID")
                self.copy(key, depth + 1)
                result[key] = self.copy(item, depth + 1)
        else:
            fail("JSON_SCHEMA_INPUT_INVALID")
        if self.size > self.policy.max_bytes:
            fail()
        return result

    def parse(self, text: str) -> Json:
        if len(text) > self.policy.max_bytes:
            fail()
        depth = 0
        quoted = escaped = False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > self.policy.max_depth:
                    fail()
            elif char in "]}":
                depth -= 1
        try:
            parsed: object = json.loads(
                text,
                parse_float=self._decimal,
                parse_int=self._integer,
                parse_constant=self._constant,
                object_pairs_hook=self._pairs,
            )
        except (ValueError, RecursionError):
            fail("JSON_SCHEMA_INPUT_INVALID")
        return self.copy(parsed)

    def _integer(self, text: str) -> int:
        if len(text.lstrip("-")) > self.policy.max_numeric_digits:
            fail()
        return int(text)

    def _decimal(self, text: str) -> Decimal:
        if len(text) > self.policy.max_numeric_digits + 16:
            fail()
        mantissa, separator, exponent = text.lower().partition("e")
        if separator and (
            len(exponent.lstrip("+-")) > 4
            or abs(int(exponent)) > self.policy.max_decimal_exponent
        ):
            fail()
        if sum(c.isdigit() for c in mantissa) > self.policy.max_numeric_digits:
            fail()
        return Decimal(text)

    @staticmethod
    def _constant(text: str) -> Never:
        fail("JSON_SCHEMA_INPUT_INVALID")

    @staticmethod
    def _pairs(pairs: list[tuple[str, Json]]) -> dict[str, Json]:
        result: dict[str, Json] = {}
        for key, value in pairs:
            if key in result:
                fail("JSON_SCHEMA_INPUT_INVALID")
            result[key] = value
        return result


def fingerprint(value: Json) -> str:
    """Type-tagged encoding различает Decimal literal и текст той же формы."""
    digest = hashlib.sha256()

    def visit(item: Json) -> None:
        if isinstance(item, dict):
            digest.update(b"{")
            for key in sorted(item):
                visit(key)
                visit(item[key])
            digest.update(b"}")
        elif isinstance(item, list):
            digest.update(b"[")
            for child in item:
                visit(child)
            digest.update(b"]")
        else:
            payload = str(item).encode("utf-8")
            digest.update(
                type(item).__name__.encode("ascii")
                + b":"
                + str(len(payload)).encode("ascii")
                + b":"
                + payload
            )

    visit(value)
    return digest.hexdigest()
