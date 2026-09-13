"""Однопроходная замена overlapping spans; reversible map отделена от результата."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import DataClassification
from structuraguard.contracts.privacy import (
    DetectionPolicy,
    DetectionReport,
    PlaceholderHandle,
    PrivacySummary,
)
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.ports.privacy import (
    PlaceholderEntry,
    PlaceholderPayload,
    PlaceholderStore,
)

from ._privacy import ScanBudget, failure, safe_failure

_TOKEN = re.compile(r"\[SGR:[0-9a-f]{32}:[0-9a-f]{4}\]", re.ASCII)


@dataclass(frozen=True, slots=True)
class RedactedField:
    """Value/имя доступны явно, но никогда не попадают в repr/str."""

    name: str = field(repr=False)
    value: str = field(repr=False)


def _binding(fields: tuple[RedactedField, ...]) -> str:
    return canonical_sha256_value(tuple((f.name, f.value) for f in fields))


@dataclass(frozen=True, slots=True, repr=False)
class RedactionResult:
    """Masked content всё ещё чувствителен; logs используют только safe_summary."""

    fields: tuple[RedactedField, ...]
    report: DetectionReport
    redaction_fingerprint: str
    handle: PlaceholderHandle | None = None

    @property
    def text(self) -> str:
        """Вернуть masked value единственного поля, иначе SecurityPolicyError.

        Строка может содержать нераспознанные данные и не является safe log."""
        if len(self.fields) != 1:
            raise failure() from None
        return self.fields[0].value

    @property
    def classification(self) -> DataClassification:
        """Вернуть исходный класс из report; маскирование его не понижает."""
        return self.report.classification

    def safe_summary(self) -> PrivacySummary:
        """Вернуть counts/класс без raw/ masked fields, handles и locations."""
        return self.report.safe_summary()

    def __repr__(self) -> str:
        return f"RedactionResult({self.safe_summary()!r})"


def _spans(
    report: DetectionReport, index: int, part: str
) -> tuple[tuple[int, int], ...]:
    found = sorted(
        (f.start, f.end)
        for f in report.findings
        if f.field_index == index and f.part == part
    )
    merged: list[tuple[int, int]] = []
    for start, end in found:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


async def mask(
    fields: tuple[tuple[str, str], ...],
    report: DetectionReport,
    *,
    policy: DetectionPolicy,
    budget: ScanBudget,
    run_id: UUID,
    store: PlaceholderStore | None,
    principal: UUID | None,
) -> RedactionResult:
    if any("[SGR:" in text for pair in fields for text in pair):
        raise failure("SECURITY_REDACTION_DENIED") from None
    namespace = uuid4().hex
    replacements: dict[str, str] = {}
    output: list[RedactedField] = []
    output_chars = 0
    for index, pair in enumerate(fields):
        parts = []
        for part, text in zip(("name", "value"), pair, strict=True):
            cursor, size = 0, len(text)
            spans = _spans(report, index, part)
            chosen: list[tuple[int, int, str]] = []
            for start, end in spans:
                raw = text[start:end]
                if raw not in replacements:
                    replacements[raw] = f"[SGR:{namespace}:{len(replacements):04x}]"
                token = replacements[raw]
                size += len(token) - (end - start)
                chosen.append((start, end, token))
            if size > policy.limits.max_output_chars - output_chars:
                raise failure("SECURITY_LIMIT_EXCEEDED") from None
            output_chars += size
            fragments: list[str] = []
            for start, end, token in chosen:
                fragments.extend((text[cursor:start], token))
                cursor = end
            fragments.append(text[cursor:])
            parts.append("".join(fragments))
        output.append(RedactedField(parts[0], parts[1]))
        budget.check_time()
    masked = tuple(output)
    binding = _binding(masked)
    handle: PlaceholderHandle | None = None
    if policy.redaction_mode == "reversible":
        if store is None or type(principal) is not UUID:
            raise failure("SECURITY_REDACTION_DENIED") from None
        payload = PlaceholderPayload(
            binding,
            tuple(PlaceholderEntry(token, raw) for raw, token in replacements.items()),
        )
        handle = await store.put(
            run_id=str(run_id),
            principal=str(principal),
            payload=payload,
            ttl_seconds=policy.map_ttl_seconds,
        )
        try:
            budget.check_time()
        except SecurityPolicyError:
            await store.discard(
                handle=handle, run_id=str(run_id), principal=str(principal)
            )
            raise
    return RedactionResult(
        masked, report, canonical_sha256_value((policy.fingerprint, binding)), handle
    )


async def restore_fields(
    result: RedactionResult,
    *,
    store: PlaceholderStore,
    run_id: UUID,
    principal: UUID,
    max_chars: int = 1_000_000,
) -> tuple[RedactedField, ...]:
    """Восстановить только exact masked result своего run через отдельный store.

    Args:
        result: Неизменённый RedactionResult reversible операции.
        store: Авторизующий port; get возвращает plaintext после ACL/run/TTL gates.
        run_id: UUID исходного run; чужой run отклоняется.
        principal: UUID из trusted host authentication, не из входных данных.
        max_chars: Cap суммарных names/values до расширения placeholders.

    Returns:
        Tuple RedactedField с исходными names/values в прежнем порядке.

    Raises:
        SecurityPolicyError: Неверный/истёкший handle, tamper, modified result, cap
            или SECURITY_MAP_INVALID при неизвестной ошибке store, без raw текста.
        asyncio.CancelledError: Отмена ожидания store с пустыми args.

    Get — явный I/O; restore не удаляет map. Произвольный LLM output не принимается.
    Возвращённые поля содержат raw restricted data, хотя repr их скрывает."""
    try:
        if (
            type(result) is not RedactionResult
            or result.handle is None
            or type(run_id) is not UUID
            or type(principal) is not UUID
            or type(max_chars) is not int
            or not 0 < max_chars <= 4_000_000
            or type(result.fields) is not tuple
            or len(result.fields) > 1024
        ):
            raise failure("SECURITY_REDACTION_DENIED") from None
        count = 0
        for part in result.fields:
            if (
                type(part) is not RedactedField
                or type(part.name) is not str
                or type(part.value) is not str
            ):
                raise failure("SECURITY_REDACTION_DENIED") from None
            count += len(part.name) + len(part.value)
            if count > 4_000_000:
                raise failure("SECURITY_LIMIT_EXCEEDED") from None
        payload = await store.get(
            handle=result.handle, run_id=str(run_id), principal=str(principal)
        )
        if (
            type(payload) is not PlaceholderPayload
            or type(payload.entries) is not tuple
            or len(payload.entries) > 10000
            or payload.redacted_fingerprint != _binding(result.fields)
        ):
            raise failure("SECURITY_MAP_INVALID") from None
        mapping: dict[str, str] = {}
        for entry in payload.entries:
            if (
                type(entry) is not PlaceholderEntry
                or type(entry.placeholder) is not str
                or type(entry.value) is not str
                or _TOKEN.fullmatch(entry.placeholder) is None
                or entry.placeholder in mapping
            ):
                raise failure("SECURITY_MAP_INVALID") from None
            mapping[entry.placeholder] = entry.value
        restored = []
        size = 0
        for part in result.fields:
            pair = []
            for text in (part.name, part.value):
                length = len(text)
                for match in _TOKEN.finditer(text):
                    if match.group() not in mapping:
                        raise failure("SECURITY_MAP_INVALID") from None
                    length += len(mapping[match.group()]) - len(match.group())
                if length > max_chars - size:
                    raise failure("SECURITY_LIMIT_EXCEEDED") from None
                size += length
                pair.append(_TOKEN.sub(lambda match: mapping[match.group()], text))
            restored.append(RedactedField(pair[0], pair[1]))
        return tuple(restored)
    except asyncio.CancelledError:
        raise asyncio.CancelledError from None
    except SecurityPolicyError as error:
        raise safe_failure(error) from None
    except BaseException as error:
        # PlaceholderStore — внешняя граница; тип ошибки не делает message safe.
        if not isinstance(error, Exception):
            raise
        raise failure("SECURITY_MAP_INVALID") from None


async def restore_text(
    result: RedactionResult, *, store: PlaceholderStore, run_id: UUID, principal: UUID
) -> str:
    """Вернуть raw str единственного поля через авторизованный restore_fields.

    result/store/run_id/principal имеют тот же ACL/TTL/binding контракт, что
    restore_fields. Несколько полей дают SecurityPolicyError. Store может выполнять
    I/O; raw result нельзя отправлять в logs/LLM без отдельной host policy."""
    fields = await restore_fields(
        result, store=store, run_id=run_id, principal=principal
    )
    if len(fields) != 1:
        raise failure("SECURITY_REDACTION_DENIED") from None
    return fields[0].value
