"""Полный bounded content scan и redaction; не выпускает LLM approvals."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from typing import TYPE_CHECKING
from uuid import UUID

from structuraguard.contracts.common import DataClassification as Classification
from structuraguard.contracts.privacy import (
    CategoryRule,
    CustomPattern,
    DetectionPolicy,
    DetectionReport,
    ScanLimits,
    SensitiveFinding,
)
from structuraguard.contracts.privacy import (
    SensitiveCategory as Category,
)
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.profiling.pii import maximum_classification

from ._privacy import ScanBudget, failure, safe_failure
from .patterns import BUILTINS, accepted, custom_pattern, field_category, secret_spans

if TYPE_CHECKING:
    from structuraguard.ports.privacy import PlaceholderStore

    from .redaction import RedactionResult
    from .session import SecuritySession


def _checked_policy(policy: DetectionPolicy) -> DetectionPolicy:
    if type(policy) is not DetectionPolicy or type(policy.limits) is not ScanLimits:
        raise failure("SECURITY_POLICY_INVALID") from None
    if (
        any(
            type(getattr(policy.limits, name)) is not int
            for name in ScanLimits.model_fields
        )
        or type(policy.custom_patterns) is not tuple
        or len(policy.custom_patterns) > 32
        or type(policy.categories) is not tuple
        or len(policy.categories) > 12
        or type(policy.custom_secret_fields) is not tuple
        or len(policy.custom_secret_fields) > 32
        or any(
            type(p) is not CustomPattern
            or type(p.pattern) is not str
            or len(p.pattern) > 256
            for p in policy.custom_patterns
        )
        or any(type(p) is not CategoryRule for p in policy.categories)
        or any(type(p) is not str or len(p) > 128 for p in policy.custom_secret_fields)
    ):
        raise failure("SECURITY_POLICY_INVALID") from None
    try:
        return DetectionPolicy.model_validate(policy.model_dump(warnings="error"))
    except (ValueError, TypeError):
        raise failure("SECURITY_POLICY_INVALID") from None


class ContentProtector:
    """Классифицировать и маскировать ограниченный текст по trusted policy.

    Args:
        policy: DetectionPolicy с обязательными detectors и конечными caps.
        resources: Необязательная SecuritySession для общего deadline/events.
        monotonic: Монотонные секунды для проверки длительности scan.

    Создание компилирует safe patterns без files/network/store I/O. Конфигурация,
    scan и limits дают SecurityPolicyError с закрытым code без raw diagnostics.
    Методы возвращают полный DetectionReport или RedactionResult, сохраняя
    максимум baseline, minimum_classification и category floors.
    Store и authenticated principal нужны отдельно только для reversible mode.
    Классификация не подтверждает владельца ID и не выдаёт egress approval.
    Синхронный regex pass ограничен input, но не прерывается OS deadline."""

    def __init__(
        self,
        policy: DetectionPolicy,
        *,
        resources: SecuritySession | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = _checked_policy(policy)
        if resources is not None:
            limits = self.policy.limits.model_dump()
            limits["max_chars"] = min(
                self.policy.limits.max_chars, resources.limits.max_text_chars
            )
            limits["max_time_ms"] = min(
                self.policy.limits.max_time_ms, resources.limits.max_processing_time_ms
            )
            self._policy = self.policy.model_copy(
                update={"limits": ScanLimits.model_validate(limits)}
            )
        self._resources, self._clock = resources, monotonic
        self._custom = tuple(
            (custom_pattern(p.pattern), p.classification, max(1, len(p.pattern)) * 257)
            for p in self.policy.custom_patterns
        )
        self._secret_fields = tuple(
            "".join(c for c in p.casefold() if c.isalnum())
            for p in self.policy.custom_secret_fields
        )

    @property
    def policy(self) -> DetectionPolicy:
        """Effective frozen policy; compiled detectors соответствуют этому snapshot."""
        return self._policy

    async def _call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        async def timed() -> T:
            try:
                async with asyncio.timeout(self.policy.limits.max_time_ms / 1000):
                    return await operation()
            except asyncio.CancelledError:
                raise asyncio.CancelledError from None
            except TimeoutError:
                raise failure("PROCESSING_TIMEOUT") from None
            except SecurityPolicyError as error:
                raise safe_failure(error) from None
            except BaseException as error:
                # Чужие chunk/store adapters могут выбросить Exception с raw data.
                if not isinstance(error, Exception):
                    raise
                raise failure("SECURITY_SCAN_FAILED") from None

        if self._resources is not None:
            return await self._resources.call(timed)
        return await timed()

    def _pairs(
        self, fields: Mapping[str, str], budget: ScanBudget
    ) -> tuple[tuple[str, str], ...]:
        if type(fields) is not dict:
            raise failure() from None
        if len(fields) > self.policy.limits.max_fields:
            raise failure("SECURITY_LIMIT_EXCEEDED") from None
        result = []
        for name, value in fields.items():
            budget.add_text(name)
            budget.add_text(value)
            result.append((name, value))
        return tuple(result)

    def _single(
        self, text: str, field_name: str | None, budget: ScanBudget
    ) -> tuple[tuple[str, str], ...]:
        name = "" if field_name is None else field_name
        budget.add_text(name)
        budget.add_text(text)
        return ((name, text),)

    def _class(self, category: Category) -> Classification:
        base = (
            Classification.RESTRICTED
            if category
            in {
                Category.CARD,
                Category.API_KEY,
                Category.TOKEN,
                Category.PRIVATE_KEY,
                Category.PASSWORD,
            }
            else Classification.CONFIDENTIAL
        )
        return maximum_classification(
            base,
            *(
                r.classification
                for r in self.policy.categories
                if r.category is category
            ),
        )

    async def _scan(
        self,
        fields: tuple[tuple[str, str], ...],
        budget: ScanBudget,
        minimum: Classification,
    ) -> DetectionReport:
        if type(minimum) is not Classification:
            raise failure() from None
        findings: list[SensitiveFinding] = []
        level = maximum_classification(self.policy.baseline, minimum)
        for index, (name, value) in enumerate(fields):
            for part, text in (("name", name), ("value", value)):

                def add(
                    start: int,
                    end: int,
                    category: Category,
                    classification: Classification | None = None,
                    *,
                    field_index: int = index,
                    field_part: str = part,
                ) -> None:
                    nonlocal level
                    budget.finding()
                    chosen = maximum_classification(
                        self._class(category), classification or Classification.PUBLIC
                    )
                    level = maximum_classification(level, chosen)
                    findings.append(
                        SensitiveFinding(
                            field_index=field_index,
                            part="name" if field_part == "name" else "value",
                            start=start,
                            end=end,
                            category=category,
                            classification=chosen,
                        )
                    )

                if (
                    part == "value"
                    and value
                    and (category := field_category(name, self._secret_fields))
                    is not None
                ):
                    add(0, len(value), category)
                for pattern in BUILTINS:
                    await budget.spend(len(text) * pattern.cost)
                    for match in pattern.regex.finditer(text):
                        if accepted(pattern.category, match.group(), self.policy):
                            add(match.start(), match.end(), pattern.category)
                await budget.spend(len(text) * 16)
                for start, end, category in secret_spans(text):
                    add(start, end, category)
                for regex, classification, cost in self._custom:
                    await budget.spend(len(text) * cost)
                    for match in regex.finditer(text):
                        add(match.start(), match.end(), Category.CUSTOM, classification)
        budget.check_time()
        return DetectionReport(
            classification=level,
            scanned_chars=budget.chars,
            work_used=budget.work,
            findings=tuple(findings),
        )

    async def classify(
        self,
        text: str,
        *,
        minimum_classification: Classification = Classification.PUBLIC,
        field_name: str | None = None,
    ) -> DetectionReport:
        """Вернуть DetectionReport для точного str и необязательного field_name.

        Имя также сканируется; minimum_classification нельзя понизить.
        Input не меняется, I/O отсутствует. Неподдержанный input, cap/deadline или
        scan failure даёт SecurityPolicyError без partial clean и raw diagnostics."""

        async def operation() -> DetectionReport:
            budget = ScanBudget(self.policy.limits, self._clock)
            return await self._scan(
                self._single(text, field_name, budget), budget, minimum_classification
            )

        return await self._call(operation)

    async def classify_fields(
        self,
        fields: Mapping[str, str],
        *,
        minimum_classification: Classification = Classification.PUBLIC,
    ) -> DetectionReport:
        """Вернуть полный DetectionReport для имён/значений exact dict[str, str].

        Несмотря на Mapping в annotation, произвольный Mapping и nested input
        дают SecurityPolicyError. Порядок dict задаёт field_index; исходные поля и
        minimum_classification сохраняются. Внешний I/O отсутствует."""

        async def operation() -> DetectionReport:
            budget = ScanBudget(self.policy.limits, self._clock)
            return await self._scan(
                self._pairs(fields, budget), budget, minimum_classification
            )

        return await self._call(operation)

    async def classify_chunks(
        self,
        chunks: AsyncIterable[str],
        *,
        minimum_classification: Classification = Classification.PUBLIC,
    ) -> DetectionReport:
        """Прочитать async chunks и вернуть общий DetectionReport без потери границ.

        Chunks — точные str; count/char/byte caps проверяются до сохранения.
        Это конечная буферизация, включая совпадения между chunks, не бесконечный scan.
        minimum_classification сохраняется. Foreign iterator errors дают безопасный
        SECURITY_SCAN_FAILED; отмена — CancelledError. Закрытие iterator — host."""

        async def operation() -> DetectionReport:
            budget = ScanBudget(self.policy.limits, self._clock)
            parts: list[str] = []
            async for chunk in chunks:
                if len(parts) >= self.policy.limits.max_chunks:
                    raise failure("SECURITY_LIMIT_EXCEEDED") from None
                budget.add_text(chunk)
                parts.append(chunk)
            return await self._scan(
                (("", "".join(parts)),), budget, minimum_classification
            )

        return await self._call(operation)

    async def redact(
        self,
        text: str,
        *,
        run_id: UUID,
        minimum_classification: Classification = Classification.PUBLIC,
        field_name: str | None = None,
        store: PlaceholderStore | None = None,
        principal: UUID | None = None,
    ) -> RedactionResult:
        """Вернуть RedactionResult, самостоятельно сканируя text и field_name.

        run_id — UUID операции; minimum_classification сохраняет исходный класс.
        Reversible mode требует отдельный store и authenticated principal, TTL из
        policy. Иначе store I/O нет и handle отсутствует. Ошибка/cap/deadline даёт
        SecurityPolicyError без raw текста. Masked output не разрешает egress и может
        содержать нераспознанные secrets; для logs используйте safe_summary()."""

        async def operation() -> RedactionResult:
            budget = ScanBudget(self.policy.limits, self._clock)
            fields = self._single(text, field_name, budget)
            return await self._redact(
                fields, budget, minimum_classification, run_id, store, principal
            )

        return await self._call(operation)

    async def redact_fields(
        self,
        fields: Mapping[str, str],
        *,
        run_id: UUID,
        minimum_classification: Classification = Classification.PUBLIC,
        store: PlaceholderStore | None = None,
        principal: UUID | None = None,
    ) -> RedactionResult:
        """Вернуть RedactionResult для exact dict[str, str] в исходном порядке.

        Имена и значения сканируются; credential fields маскируются целиком.
        run_id/minimum_classification/store/principal имеют тот же контракт, что redact.
        Reversible map записывается отдельно; ошибка или отмена не даёт partial clean.
        SecurityPolicyError не содержит raw diagnostics; fields/handle не являются logs."""

        async def operation() -> RedactionResult:
            budget = ScanBudget(self.policy.limits, self._clock)
            pairs = self._pairs(fields, budget)
            return await self._redact(
                pairs, budget, minimum_classification, run_id, store, principal
            )

        return await self._call(operation)

    async def _redact(
        self,
        fields: tuple[tuple[str, str], ...],
        budget: ScanBudget,
        minimum: Classification,
        run_id: UUID,
        store: PlaceholderStore | None,
        principal: UUID | None,
    ) -> RedactionResult:
        from .redaction import mask

        if type(run_id) is not UUID:
            raise failure() from None
        if self.policy.redaction_mode == "reversible" and (
            store is None or type(principal) is not UUID
        ):
            raise failure("SECURITY_REDACTION_DENIED") from None
        report = await self._scan(fields, budget, minimum)
        return await mask(
            fields,
            report,
            policy=self.policy,
            budget=budget,
            run_id=run_id,
            store=store,
            principal=principal,
        )
