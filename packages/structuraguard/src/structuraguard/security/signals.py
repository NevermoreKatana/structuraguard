"""Bounded signals по недоверенному тексту; не интерпретирует и не очищает input."""

import asyncio
import hashlib
import json
import time
import unicodedata
from collections.abc import Awaitable, Callable, Iterator
from typing import Literal

from structuraguard.contracts.injection import (
    InjectionLocation,
    InjectionOrigin,
    InjectionPolicy,
    InjectionReport,
    InjectionSeverity,
    InjectionSignal,
)
from structuraguard.contracts.privacy import ScanLimits
from structuraguard.exceptions import SecurityPolicyError

from ._privacy import ScanBudget, failure, safe_failure
from ._signal_patterns import PATTERNS

_ZERO_WIDTH = frozenset("\u200b\u200c\u200d\ufeff\u2060")


def _checked_policy(policy: InjectionPolicy) -> InjectionPolicy:
    if type(policy) is not InjectionPolicy or type(policy.limits) is not ScanLimits:
        raise failure("SECURITY_POLICY_INVALID") from None
    if any(
        type(getattr(policy.limits, name)) is not int
        for name in ScanLimits.model_fields
    ):
        raise failure("SECURITY_POLICY_INVALID") from None
    try:
        return InjectionPolicy.model_validate(policy.model_dump(warnings="error"))
    except (ValueError, TypeError):
        raise failure("SECURITY_POLICY_INVALID") from None


def _json_preflight(text: str, policy: InjectionPolicy) -> None:
    depth = items = 0
    quoted = escaped = atom = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char.isspace() or char in ":,]}":
            atom = False
            if char in "]}":
                depth -= 1
            continue
        if char in '[{"':
            quoted = char == '"'
            if not quoted:
                depth += 1
            items += 1
            atom = False
        elif not atom:
            atom = True
            items += 1
        if depth > policy.max_json_depth or items > policy.max_json_items:
            raise failure("SECURITY_LIMIT_EXCEEDED") from None


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise failure() from None
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise failure() from None


def _strings(value: object) -> Iterator[tuple[Literal["key", "value"], str]]:
    pending: list[tuple[Literal["key", "value"], object]] = [("value", value)]
    while pending:
        part, current = pending.pop()
        if type(current) is dict:
            for name, child in reversed(tuple(current.items())):
                pending.append(("value", child))
                pending.append(("key", name))
        elif type(current) is list:
            pending.extend(("value", child) for child in reversed(current))
        elif type(current) is str:
            yield part, current


class InjectionDetector:
    """Выполнить bounded heuristic scan, сохраняя исходные evidence offsets.

    Args:
        policy: Trusted action/scan caps; high risk не допускает observe.
        monotonic: Монотонные секунды для deadline.

    Создание/scan не выполняет I/O. Fixed rules используют отдельную NFKC/casefold
    копию, raw не меняется. Неверная policy/input, overflow/failure дают
    SecurityPolicyError; отмена — CancelledError без raw текста. Ни наличие, ни
    отсутствие signals не доказывает intent/безопасность; partial clean не выдаётся."""

    def __init__(
        self,
        policy: InjectionPolicy,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = _checked_policy(policy)
        self._clock = monotonic

    @property
    def policy(self) -> InjectionPolicy:
        """Вернуть проверенный immutable snapshot limits и действий detector."""
        return self._policy

    async def _call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        try:
            async with asyncio.timeout(self.policy.limits.max_time_ms / 1000):
                return await operation()
        except asyncio.CancelledError:
            raise asyncio.CancelledError from None
        except TimeoutError:
            raise failure("PROCESSING_TIMEOUT") from None
        except SecurityPolicyError as error:
            raise safe_failure(error) from None
        except (
            ValueError,
            TypeError,
            AttributeError,
            RuntimeError,
            OSError,
            RecursionError,
        ):
            raise failure("SECURITY_SCAN_FAILED") from None

    async def scan(
        self, text: str, *, origin: InjectionOrigin = InjectionOrigin.SOURCE_TEXT
    ) -> InjectionReport:
        """Вернуть InjectionReport для точного str с offsets в code points text.

        origin маркирует происхождение, не выдаёт authority. Сканируется вся допустимая
        строка без изменения input/I/O; end не включён в span. SecurityPolicyError
        обозначает input/cap/deadline/failure. Отсутствие signals не даёт approval."""

        async def operation() -> InjectionReport:
            budget = ScanBudget(self.policy.limits, self._clock)
            budget.add_text(text)
            return await self._scan(text, (("text", text),), origin, budget)

        return await self._call(operation)

    async def scan_json(self, payload: str) -> InjectionReport:
        """Вернуть InjectionReport для строк и ключей JSON object из payload str.

        Byte/char/depth/item caps проверяются до decode. Duplicate keys, non-finite
        numbers и unsupported root дают SecurityPolicyError. Locations относятся
        к decoded строкам в порядке обхода, не к JSON bytes. Ничего не исполняется."""

        async def operation() -> InjectionReport:
            budget = ScanBudget(self.policy.limits, self._clock)
            budget.add_text(payload)
            await budget.spend(len(payload) * 4)
            _json_preflight(payload, self.policy)
            decoded: object = json.loads(
                payload, object_pairs_hook=_unique, parse_constant=_constant
            )
            if type(decoded) is not dict:
                raise failure() from None
            return await self._scan(
                payload, _strings(decoded), InjectionOrigin.LLM_PAYLOAD, budget
            )

        return await self._call(operation)

    def _normalized(
        self, text: str, remaining: int, budget: ScanBudget
    ) -> tuple[str, list[int]]:
        pieces: list[str] = []
        positions: list[int] = []
        for index, char in enumerate(text):
            if char in _ZERO_WIDTH:
                continue
            piece = unicodedata.normalize("NFKC", char).casefold()
            if len(piece) > remaining - len(positions):
                raise failure("SECURITY_LIMIT_EXCEEDED") from None
            pieces.append(piece)
            positions.extend([index] * len(piece))
            if index % 256 == 0:
                budget.check_time()
        return "".join(pieces), positions

    async def _scan(
        self,
        payload: str,
        values: Iterator[tuple[Literal["key", "value"], str]]
        | tuple[tuple[Literal["text"], str], ...],
        origin: InjectionOrigin,
        budget: ScanBudget,
    ) -> InjectionReport:
        if type(origin) is not InjectionOrigin:
            raise failure() from None
        signals: list[InjectionSignal] = []
        normalized_chars = 0
        for index, (part, text) in enumerate(values):
            if index >= self.policy.limits.max_fields:
                raise failure("SECURITY_LIMIT_EXCEEDED") from None
            # Decoded escapes могут содержать invalid Unicode surrogate.
            if any(0xD800 <= ord(c) <= 0xDFFF for c in text):
                raise failure() from None
            await budget.spend(len(text) * 20)
            normalized, positions = self._normalized(
                text, self.policy.max_normalized_chars - normalized_chars, budget
            )
            normalized_chars += len(normalized)
            for pattern in PATTERNS:
                await budget.spend(len(normalized) * pattern.cost)
                for match in pattern.regex.finditer(normalized):
                    budget.finding()
                    if len(signals) >= 256:
                        raise failure("SECURITY_LIMIT_EXCEEDED") from None
                    signals.append(
                        InjectionSignal(
                            code=pattern.code,
                            severity=pattern.severity,
                            location=InjectionLocation(
                                origin=origin,
                                item_index=index,
                                part=part,
                                start=positions[match.start()],
                                end=positions[match.end() - 1] + 1,
                            ),
                        )
                    )
        budget.check_time()
        severity = max(
            (s.severity for s in signals),
            key=list(InjectionSeverity).index,
            default=InjectionSeverity.NONE,
        )
        return InjectionReport(
            policy_fingerprint=self.policy.fingerprint,
            payload_fingerprint="sha256:"
            + hashlib.sha256(payload.encode()).hexdigest(),
            signals=tuple(signals),
            severity=severity,
            action=self.policy.action_for(severity),
            scanned_chars=budget.chars,
            work_used=budget.work,
        )
