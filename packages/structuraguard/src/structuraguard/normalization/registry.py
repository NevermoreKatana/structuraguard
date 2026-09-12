"""Instance-local registry и атомарное исполнение scalar цепочек."""

from dataclasses import dataclass, field
from inspect import iscoroutinefunction
from typing import Protocol

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import RawScalar
from structuraguard.contracts.normalization import (
    NormalizationPolicy,
    NormalizationResult,
    NormalizationStep,
    NormalizerDescriptor,
    NormalizerOutput,
    NormalizerParameter,
    NormalizerSpec,
)
from structuraguard.contracts.normalized import NormalizedValue
from structuraguard.exceptions import ValidationError
from structuraguard.normalization._boundary import checked, failure
from structuraguard.ports.normalization import Normalizer


class _Call(Protocol):
    def __call__(
        self,
        value: RawScalar,
        *,
        policy: NormalizationPolicy,
        parameters: tuple[NormalizerParameter, ...],
    ) -> NormalizerOutput: ...


@dataclass(frozen=True, slots=True)
class _Registration:
    descriptor: NormalizerDescriptor
    call: _Call = field(repr=False)


@dataclass(frozen=True, slots=True)
class NormalizerRegistrySnapshot:
    """Замороженный набор registrations для одного run.

    Fingerprint связывает ID/version, а не исходный код plugins. Composition
    owner отвечает за чистоту и неизменяемость явно переданных implementations.
    Изменения исходного registry не меняют этот snapshot.
    """

    _entries: tuple[_Registration, ...] = field(repr=False)

    @property
    def descriptors(self) -> tuple[NormalizerDescriptor, ...]:
        """Получить identities в canonical порядке без вызова normalizers."""
        return tuple(entry.descriptor for entry in self._entries)

    @property
    def fingerprint(self) -> str:
        """Вычислить hash identities, не выполняя code/import/I/O."""
        return canonical_sha256_value(
            (
                "normalizer_registry_v1",
                tuple((item.normalizer_id, item.version) for item in self.descriptors),
            )
        )

    def normalize(
        self,
        value: RawScalar,
        *,
        steps: tuple[NormalizerSpec, ...],
        policy: NormalizationPolicy | None = None,
    ) -> NormalizationResult:
        """Нормализовать scalar атомарно, сохраняя его исходное представление.

        Args:
            value: Tagged raw scalar; контейнеры и Python coercion не допускаются.
            steps: Непустая ordered цепочка явных ID/version/config.
            policy: Locale/tokens/currency/limits; None выбирает conservative defaults.

        Returns:
            Immutable raw/input/output и trace. Невалидные данные — result issues.

        Raises:
            ValidationError: Неизвестный normalizer, malformed DTO, budget либо
                нарушение plugin protocol. Диагностика не содержит raw values.

        I/O, environment, часы и mutable global state не используются.
        """
        return self._normalize(value, steps=steps, policy=policy, source=None)

    def normalize_value(
        self,
        value: NormalizedValue,
        *,
        steps: tuple[NormalizerSpec, ...],
        policy: NormalizationPolicy | None = None,
    ) -> NormalizationResult:
        """Применить scalar steps к selection result, сохранив полный source DTO.

        Args:
            value: Исходный NormalizedValue; вход цепочки — normalized_value.
            steps: Непустая упорядоченная цепочка ID/version/config.
            policy: Явная locale и ограничения; None выбирает conservative defaults.

        Returns:
            Новый immutable sidecar с raw value, source DTO и trace этой цепочки.
            При отказе сохраняется вход цепочки и история попытки преобразования.

        Raises:
            ValidationError: Malformed DTO, неизвестный normalizer, нарушение
                protocol или превышение budget; raw values в ошибку не включаются.

        Значения и provenance исходного DTO не изменяются. Метод проверяет форму,
        но не доказывает physical provenance; replay остаётся отдельной границей.
        Результат нельзя подставлять в прежний batch/MappingPlan без новой lineage.
        Ошибки/limits и отсутствие I/O соответствуют normalize().
        """
        policy = checked(
            NormalizationPolicy() if policy is None else policy,
            NormalizationPolicy,
            code="NORMALIZATION_POLICY_INVALID",
        )
        source = checked(value, NormalizedValue, limits=policy.limits)
        return self._normalize(
            source.normalized_value, steps=steps, policy=policy, source=source
        )

    def _normalize(
        self,
        value: RawScalar,
        *,
        steps: tuple[NormalizerSpec, ...],
        policy: NormalizationPolicy | None,
        source: NormalizedValue | None,
    ) -> NormalizationResult:
        policy = checked(
            NormalizationPolicy() if policy is None else policy,
            NormalizationPolicy,
            code="NORMALIZATION_POLICY_INVALID",
        )
        if type(steps) is not tuple or not steps:
            raise failure("NORMALIZATION_INPUT_INVALID")
        if len(steps) > policy.limits.max_steps:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        requested = tuple(
            checked(step, NormalizerSpec, limits=policy.limits) for step in steps
        )
        entries = {
            (entry.descriptor.normalizer_id, entry.descriptor.version): entry
            for entry in self._entries
        }
        if any((step.normalizer_id, step.version) not in entries for step in requested):
            raise failure("NORMALIZER_NOT_FOUND")
        # Полная исходная копия не передаётся plugin; mutation не затрагивает caller.
        original = checked(value, type(value), limits=policy.limits)
        traces: list[NormalizationStep] = []
        current = original
        trace_bytes = 0
        for spec in requested:
            entry = entries[(spec.normalizer_id, spec.version)]
            call_input = checked(current, type(current), limits=policy.limits)
            call_policy = checked(policy, NormalizationPolicy)
            call_spec = checked(spec, NormalizerSpec, limits=policy.limits)
            before = (
                call_input.model_dump_json(),
                call_policy.model_dump_json(),
                call_spec.model_dump_json(),
            )
            try:
                returned = entry.call(
                    call_input, policy=call_policy, parameters=call_spec.parameters
                )
            except (
                ValidationError,
                ValueError,
                TypeError,
                LookupError,
                AttributeError,
                ArithmeticError,
                OSError,
                RuntimeError,
                AssertionError,
            ):
                raise failure("NORMALIZER_EXECUTION_FAILED") from None
            output = checked(
                returned,
                NormalizerOutput,
                limits=policy.limits,
                code="NORMALIZER_OUTPUT_INVALID",
            )
            if output.issue_code == "SECURITY_LIMIT_EXCEEDED":
                raise failure("SECURITY_LIMIT_EXCEEDED")
            checked(
                call_input,
                type(call_input),
                limits=policy.limits,
                code="NORMALIZER_OUTPUT_INVALID",
            )
            checked(call_policy, NormalizationPolicy, code="NORMALIZER_OUTPUT_INVALID")
            checked(
                call_spec,
                NormalizerSpec,
                limits=policy.limits,
                code="NORMALIZER_OUTPUT_INVALID",
            )
            if before != (
                call_input.model_dump_json(),
                call_policy.model_dump_json(),
                call_spec.model_dump_json(),
            ):
                raise failure("NORMALIZER_OUTPUT_INVALID")
            try:
                trace = NormalizationStep(spec=spec, input_value=current, output=output)
            except (ValueError, TypeError):
                raise failure("NORMALIZER_OUTPUT_INVALID") from None
            trace_bytes += len(trace.model_dump_json().encode("utf-8"))
            if trace_bytes > policy.limits.max_trace_bytes:
                raise failure("SECURITY_LIMIT_EXCEEDED")
            traces.append(trace)
            if output.issue_code is not None:
                current = original
                break
            current = output.value
        result = NormalizationResult(
            raw_value=source.raw_value if source is not None else original,
            input_value=original,
            normalized_value=current,
            source_value=source,
            policy=policy,
            registry_fingerprint=self.fingerprint,
            requested_steps=requested,
            steps=tuple(traces),
        )
        return checked(result, NormalizationResult, limits=policy.limits)


class NormalizerRegistry:
    """Регистрация pure implementations в пределах одного composition owner.

    Новые runs получают snapshot(). freeze() дополнительно запрещает дальнейшую
    регистрацию. Нет discovery, dynamic imports и глобального registry.
    """

    def __init__(self) -> None:
        self._entries: tuple[_Registration, ...] = ()
        self._frozen = False

    @classmethod
    def with_builtins(cls) -> "NormalizerRegistry":
        """Создать независимый registry стандартных normalizers без I/O."""
        from structuraguard.normalization.builtins import builtin_normalizers

        registry = cls()
        for descriptor, normalizer in builtin_normalizers():
            registry.register(descriptor, normalizer)
        return registry

    def register(
        self, descriptor: NormalizerDescriptor, normalizer: Normalizer
    ) -> None:
        """Зарегистрировать точный ID/version; дубли/невалидный port дают ValidationError.

        Args:
            descriptor: Идентификатор и версия, выбранные владельцем приложения.
            normalizer: Доверенная синхронная pure реализация Normalizer.

        Raises:
            ValidationError: Frozen registry, повторный ID/version, невалидный
                descriptor/port или превышение 128 registrations.

        Изменяет только этот registry; существующие snapshots сохраняют состав.
        Регистрация не запускает normalize(). Переданный Python object доверенный;
        snapshot фиксирует его bound method, но не изолирует closure/state plugin.
        """
        if self._frozen:
            raise failure("NORMALIZER_REGISTRY_FROZEN")
        descriptor = checked(
            descriptor, NormalizerDescriptor, code="NORMALIZER_INVALID_ADAPTER"
        )
        if len(self._entries) >= 128:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        if any(entry.descriptor == descriptor for entry in self._entries):
            raise failure("NORMALIZER_DUPLICATE_REGISTRATION")
        if (
            not isinstance(normalizer, Normalizer)
            or not callable(normalizer.normalize)
            or iscoroutinefunction(normalizer.normalize)
        ):
            raise failure("NORMALIZER_INVALID_ADAPTER")
        self._entries = tuple(
            sorted(
                (*self._entries, _Registration(descriptor, normalizer.normalize)),
                key=lambda entry: (
                    entry.descriptor.normalizer_id,
                    entry.descriptor.version,
                ),
            )
        )

    def snapshot(self) -> NormalizerRegistrySnapshot:
        """Вернуть immutable snapshot без изменения других runs."""
        return NormalizerRegistrySnapshot(self._entries)

    def freeze(self) -> NormalizerRegistrySnapshot:
        """Запретить дальнейшие registrations и вернуть run snapshot."""
        self._frozen = True
        return self.snapshot()
