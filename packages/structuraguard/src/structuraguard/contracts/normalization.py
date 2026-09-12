"""Immutable contracts чистой scalar normalization без изменения ParsePlan DTO."""

from collections.abc import Iterator
from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, StrictStr, model_validator

from structuraguard.contracts._base import FrozenContract, canonical_sha256_value
from structuraguard.contracts.common import (
    FingerprintStr,
    IssueCodeStr,
    IssueSeverity,
    ParserIdentifierStr,
    RawScalar,
    ValidationIssue,
    VersionStr,
)
from structuraguard.contracts.normalized import NormalizedValue
from structuraguard.contracts.profiling import LocalePolicy


class _SensitiveContract(FrozenContract):
    def __repr_args__(self) -> Iterator[tuple[str | None, object]]:
        return iter(())


class NormalizationLimits(FrozenContract):
    """Конечные бюджеты одного scalar вызова; не лимиты всего dataset."""

    max_text_chars: Annotated[StrictInt, Field(ge=1, le=65_536)] = 4_096
    max_numeric_digits: Annotated[StrictInt, Field(ge=1, le=1_024)] = 128
    max_decimal_exponent: Annotated[StrictInt, Field(ge=1, le=10_000)] = 1_024
    max_steps: Annotated[StrictInt, Field(ge=1, le=64)] = 32
    max_trace_bytes: Annotated[StrictInt, Field(ge=1, le=4_194_304)] = 262_144


class NormalizationPolicy(_SensitiveContract):
    """Явные locale/tokens/currency; process locale и environment не читаются.

    Tokens сравниваются через casefold без Unicode compatibility normalization.
    Empty-to-null применяется отдельным шагом; пробелы не означают null без trim.
    Currency задаёт ожидаемую единицу money, но не выполняет конверсию валют.
    """

    locale: LocalePolicy = LocalePolicy.UNSPECIFIED
    currency: Literal["RUB", "USD", "EUR", "GBP"] | None = None
    true_tokens: Annotated[
        tuple[Annotated[StrictStr, Field(max_length=128)], ...],
        Field(min_length=1, max_length=32),
    ] = ("true", "yes", "да")
    false_tokens: Annotated[
        tuple[Annotated[StrictStr, Field(max_length=128)], ...],
        Field(min_length=1, max_length=32),
    ] = ("false", "no", "нет")
    empty_tokens: Annotated[
        tuple[Annotated[StrictStr, Field(max_length=128)], ...], Field(max_length=32)
    ] = ("",)
    limits: NormalizationLimits = NormalizationLimits()

    @model_validator(mode="after")
    def _tokens(self) -> Self:
        true = tuple(token.casefold() for token in self.true_tokens)
        false = tuple(token.casefold() for token in self.false_tokens)
        if (
            any(not token or token != token.strip() for token in (*true, *false))
            or len(set(true)) != len(true)
            or len(set(false)) != len(false)
            or set(true) & set(false)
            or len(set(self.empty_tokens)) != len(self.empty_tokens)
        ):
            raise ValueError("Tokens должны быть уникальными и непересекающимися")
        return self


class NormalizerParameter(_SensitiveContract):
    """Именованный scalar параметр custom normalizer; без callable/объектов."""

    name: ParserIdentifierStr
    value: RawScalar


class NormalizerDescriptor(FrozenContract):
    """Идентичность явно зарегистрированной реализации, не Python import path."""

    normalizer_id: ParserIdentifierStr
    version: VersionStr = "1.0.0"


class NormalizerSpec(_SensitiveContract):
    """Один вызов известного ID/version с неизменяемыми параметрами."""

    normalizer_id: ParserIdentifierStr
    version: VersionStr = "1.0.0"
    parameters: Annotated[tuple[NormalizerParameter, ...], Field(max_length=32)] = ()

    @model_validator(mode="after")
    def _parameters(self) -> Self:
        names = tuple(parameter.name for parameter in self.parameters)
        if len(set(names)) != len(names):
            raise ValueError("Имена parameters должны быть уникальны")
        return self


class ValueTransformation(_SensitiveContract):
    """Один фактический переход; raw values доступны только в явной serialization."""

    operation: ParserIdentifierStr
    input_value: RawScalar
    output_value: RawScalar

    @model_validator(mode="after")
    def _change(self) -> Self:
        if self.input_value.model_dump_json() == self.output_value.model_dump_json():
            raise ValueError("Transformation должна изменять представление значения")
        return self


class NormalizerOutput(_SensitiveContract):
    """Результат pure plugin: trace успеха либо один безопасный issue code.

    Отказ возвращает вход без частичной конверсии. Registry независимо проверяет
    эту гарантию и связность transformations до публикации результата.
    """

    value: RawScalar
    transformations: Annotated[
        tuple[ValueTransformation, ...], Field(max_length=32)
    ] = ()
    issue_code: IssueCodeStr | None = None

    @model_validator(mode="after")
    def _outcome(self) -> Self:
        if self.issue_code is not None and self.transformations:
            raise ValueError("Отказ normalizer не применяет transformations")
        return self


class NormalizationStep(_SensitiveContract):
    """Trace вызова, включая успешные no-op и единственный failed step."""

    spec: NormalizerSpec
    input_value: RawScalar
    output: NormalizerOutput

    @model_validator(mode="after")
    def _chain(self) -> Self:
        current = self.input_value
        for change in self.output.transformations:
            if current.model_dump_json() != change.input_value.model_dump_json():
                raise ValueError("Transformation chain не связана со входом")
            current = change.output_value
        if current.model_dump_json() != self.output.value.model_dump_json():
            raise ValueError("Output изменён без transformation evidence")
        return self


class NormalizationResult(_SensitiveContract):
    """Атомарная scalar цепочка с raw/selection сохранением и полным trace.

    При отказе normalized_value равен input_value; успешные ранние steps остаются
    историей попытки, а не применённым преобразованием. source_value сохраняет
    исходный NormalizedValue целиком; результат не является NormalizedBatch или
    разрешением использовать прежний MappingPlan для преобразованных данных.
    """

    schema_version: Literal["1.0.0"] = "1.0.0"
    raw_value: RawScalar
    input_value: RawScalar
    normalized_value: RawScalar
    source_value: NormalizedValue | None = None
    policy: NormalizationPolicy
    registry_fingerprint: FingerprintStr
    requested_steps: Annotated[
        tuple[NormalizerSpec, ...], Field(min_length=1, max_length=64)
    ]
    steps: Annotated[tuple[NormalizationStep, ...], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def _result(self) -> Self:
        raw = (
            self.source_value.raw_value
            if self.source_value is not None
            else self.input_value
        )
        if raw.model_dump_json() != self.raw_value.model_dump_json():
            raise ValueError("Raw value не соответствует исходному artifact")
        if (
            self.source_value is not None
            and self.source_value.normalized_value.model_dump_json()
            != self.input_value.model_dump_json()
        ):
            raise ValueError("Вход normalizer не соответствует selection result")
        if len(self.steps) > len(self.requested_steps):
            raise ValueError("Trace содержит незапрошенные steps")
        current = self.input_value
        failed = False
        for spec, step in zip(self.requested_steps, self.steps, strict=False):
            if (
                failed
                or spec != step.spec
                or current.model_dump_json() != step.input_value.model_dump_json()
            ):
                raise ValueError("Trace не соответствует запрошенной цепочке")
            current = step.output.value
            failed = step.output.issue_code is not None
        if not failed and len(self.steps) != len(self.requested_steps):
            raise ValueError("Успешный trace обязан учитывать каждый step")
        expected = self.input_value if failed else current
        if expected.model_dump_json() != self.normalized_value.model_dump_json():
            raise ValueError("Итог не соответствует атомарной цепочке")
        return self

    @property
    def accepted(self) -> bool:
        """Вернуть успех только полностью выполненной цепочки."""
        return self.steps[-1].output.issue_code is None

    @property
    def issues(self) -> tuple[ValidationIssue, ...]:
        """Вернуть codes без raw payload и непроверенных physical refs.

        Custom codes передаются как есть: для logs нужен allowlist владельца
        либо safe_summary итогового report. Этот property не редактирует PII.
        """
        return tuple(
            ValidationIssue(
                code=step.output.issue_code,
                message_key=step.output.issue_code,
                severity=IssueSeverity.ERROR,
            )
            for step in self.steps
            if step.output.issue_code is not None
        )

    @property
    def fingerprint(self) -> str:
        """Вычислить deterministic evidence hash без часов или I/O."""
        return canonical_sha256_value(self)
