"""Protocol чистого настраиваемого normalizer."""

from typing import Protocol, runtime_checkable

from structuraguard.contracts.common import RawScalar
from structuraguard.contracts.normalization import (
    NormalizationPolicy,
    NormalizerOutput,
    NormalizerParameter,
)


@runtime_checkable
class Normalizer(Protocol):
    """Trusted pure extension, регистрируемый явно composition owner.

    Реализация не выполняет I/O, не читает environment/clock, не меняет входы
    или собственное состояние. Registry не является sandbox для Python plugins.
    Идентичный value/policy/parameters должен давать идентичный output.
    """

    def normalize(
        self,
        value: RawScalar,
        *,
        policy: NormalizationPolicy,
        parameters: tuple[NormalizerParameter, ...],
    ) -> NormalizerOutput:
        """Проверить/нормализовать scalar без полномочий на внешние операции.

        Args:
            value: Immutable selected scalar; полное raw origin не передаётся.
            policy: Проверенные locale, tokens, currency и finite limits.
            parameters: Явный immutable config конкретного шага.

        Returns:
            Связная история каждого изменения либо исходный value с issue_code.

        Raises:
            ValidationError: Ошибка конфигурации реализации. Registry удаляет
                свободный exception context с недоверенными данными.

        Невалидные данные возвращаются как issue, не исправляются по догадке.
        """
        ...
