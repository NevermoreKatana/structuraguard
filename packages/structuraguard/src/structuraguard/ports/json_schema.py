"""Port проверки caller-projected JSON instance."""

from typing import Protocol, runtime_checkable

from structuraguard.contracts.json_schema import JsonSchemaResult


@runtime_checkable
class JsonSchemaValidator(Protocol):
    """Локальная meta/instance validation без исправлений и внешнего retrieval."""

    async def validate(self, instance: object, *, schema: object) -> JsonSchemaResult:
        """Проверить schema, затем instance без I/O и изменения входов.

        Args:
            instance: JSON containers/scalars и Decimal, спроецированные caller.
            schema: Локальный JSON Schema object либо bool.

        Returns:
            Bounded JsonSchemaResult со всеми issues и JSON/schema paths.
            Ошибочная схема исключает проверку instance.

        Raises:
            ValidationError: Невалидный Python input либо превышение budgets;
                частичный pass запрещён. Remote refs/retrieval не допускаются.
        """
        ...
