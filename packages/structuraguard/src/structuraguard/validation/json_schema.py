"""Draft 2020-12 service с bounded LRU и запретом внешнего retrieval."""

from collections import OrderedDict
from typing import TYPE_CHECKING

from pydantic import ValidationError as PydanticValidationError

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.json_schema import (
    JsonSchemaCacheInfo,
    JsonSchemaIssue,
    JsonSchemaPolicy,
    JsonSchemaResource,
    JsonSchemaResult,
)

from ._schema_input import Intake, Json, fail, fingerprint

if TYPE_CHECKING:
    from ._schema_backend import Compiled


class JsonSchemaValidator:
    """Проверяет caller-projected JSON без repairs, network, file/DB retrieval.

    Args:
        policy: Ограничения схемы, instance и cache; None выбирает defaults.
        resources: Локальные схемы с URI, явно разрешёнными policy владельца.

    Raises:
        ValidationError: Невалидные policy/resources либо превышение их budgets.

    Конструктор загружает bundled meta-schemas из установленных dependencies.
    Вызовы validate не выполняют I/O. Один instance хранит собственные policy,
    immutable по владению resources и bounded cache. CPU-bound работа синхронна
    внутри async API; budgets не обещают OS/RSS или wall-clock isolation.
    """

    def __init__(
        self,
        *,
        policy: JsonSchemaPolicy | None = None,
        resources: tuple[JsonSchemaResource, ...] = (),
    ) -> None:
        candidate = JsonSchemaPolicy() if policy is None else policy
        if (
            type(candidate) is not JsonSchemaPolicy
            or type(resources) is not tuple
            or len(resources) > 32
        ):
            fail("JSON_SCHEMA_POLICY_INVALID")
        state: dict[str, object] = vars(candidate)
        # Тип storage и ключей проверяется до set/iteration: subclass hooks
        # не должны исполняться при отказе недоверенной конфигурации.
        if (
            type(state) is not dict
            or len(state) != len(JsonSchemaPolicy.model_fields)
            or any(type(name) is not str for name in state)
            or set(state) != set(JsonSchemaPolicy.model_fields)
        ):
            fail("JSON_SCHEMA_POLICY_INVALID")
        for name, option in state.items():
            if name == "allowed_resource_uris":
                if (
                    type(option) is not tuple
                    or len(option) > 32
                    or any(type(uri) is not str or len(uri) > 256 for uri in option)
                ):
                    fail("JSON_SCHEMA_POLICY_INVALID")
            elif type(option) is not int:
                fail("JSON_SCHEMA_POLICY_INVALID")
        try:
            self._policy = JsonSchemaPolicy.model_validate(state)
        except (PydanticValidationError, TypeError, ValueError):
            fail("JSON_SCHEMA_POLICY_INVALID")
        self._resources: dict[str, Json] = {}
        intake = Intake(self._policy)
        for resource in resources:
            if type(resource) is not JsonSchemaResource:
                fail("JSON_SCHEMA_RESOURCE_INVALID")
            resource_state: dict[str, object] = vars(resource)
            if (
                type(resource_state) is not dict
                or len(resource_state) != 2
                or any(type(name) is not str for name in resource_state)
                or set(resource_state) != {"uri", "document_json"}
                or any(type(v) is not str for v in resource_state.values())
            ):
                fail("JSON_SCHEMA_RESOURCE_INVALID")
            if (
                len(resource.document_json) > self._policy.max_bytes
                or len(resource.uri) > 256
            ):
                fail()
            try:
                item = JsonSchemaResource.model_validate(resource_state)
            except (PydanticValidationError, TypeError, ValueError):
                fail("JSON_SCHEMA_RESOURCE_INVALID")
            if (
                item.uri not in self._policy.allowed_resource_uris
                or item.uri in self._resources
            ):
                fail("JSON_SCHEMA_RESOURCE_INVALID")
            self._resources[item.uri] = intake.parse(item.document_json)
        self._resource_size = intake.size
        self._resource_nodes = intake.nodes
        # Lazy import: импорт SDK не читает bundled dependency data или metadata.
        from ._schema_backend import Backend

        self._backend = Backend(self._policy)
        self._cache: OrderedDict[str, Compiled] = OrderedDict()
        self._bytes = self._hits = self._misses = self._evictions = 0

    @property
    def cache_info(self) -> JsonSchemaCacheInfo:
        """Безопасные счётчики; cache entries и schema payload не выдаются."""
        return JsonSchemaCacheInfo(
            entries=len(self._cache),
            bytes=self._bytes,
            hits=self._hits,
            misses=self._misses,
            evictions=self._evictions,
        )

    async def validate(self, instance: object, *, schema: object) -> JsonSchemaResult:
        """Собрать все schema/instance issues с paths в пределах finite budgets.

        Args:
            instance: Native JSON containers/scalars и Decimal. Проекцию DTO,
                date/datetime и привязку к исходным значениям выполняет caller.
            schema: JSON object или bool в поддерживаемом профиле Draft 2020-12.

        Returns:
            Immutable JsonSchemaResult с code, JSON path и schema path. Ошибки
            meta-validation возвращаются как issues без проверки instance.

        Raises:
            ValidationError: Неподдерживаемый Python input или исчерпание budget
                (JSON_SCHEMA_INPUT_INVALID / JSON_SCHEMA_LIMIT_EXCEEDED).

        Входы копируются без изменения; I/O отсутствует. Меняются только счётчики
        и bounded cache этого validator. Formats являются annotations.
        При malformed schema instance не проверяется. Превышение лимита даёт
        SDK ValidationError(JSON_SCHEMA_LIMIT_EXCEEDED), без частичного отчёта.
        Только native JSON containers/scalars и Decimal; DTO/date проецирует caller.
        """
        intake = Intake(
            self._policy, nodes=self._resource_nodes, size=self._resource_size
        )
        copied = intake.copy(schema)
        schema_hash = "sha256:" + fingerprint(
            {"schema": copied, "resources": self._resources}
        )
        key = schema_hash
        compiled = self._cache.get(key)
        if compiled is None:
            self._misses += 1
            # Compilation удаляет dialect markers только в собственных copies.
            resource_intake = Intake(self._policy)
            resources = {
                uri: resource_intake.copy(value)
                for uri, value in self._resources.items()
            }
            try:
                compiled, issues = self._backend.compile(copied, resources, intake.size)
            except (RecursionError, OverflowError):
                fail()
            if compiled is None:
                return JsonSchemaResult(
                    schema_valid=False,
                    schema_fingerprint=schema_hash,
                    policy_fingerprint=canonical_sha256_value(self._policy),
                    issues=self._ordered(issues),
                )
            self._cache_schema(key, compiled)
        else:
            self._hits += 1
            self._cache.move_to_end(key)
        value = Intake(self._policy).copy(instance)
        issues = self._backend.validate(value, compiled)
        return JsonSchemaResult(
            schema_valid=True,
            schema_fingerprint=schema_hash,
            policy_fingerprint=canonical_sha256_value(self._policy),
            issues=self._ordered(issues),
        )

    @staticmethod
    def _ordered(
        issues: tuple["JsonSchemaIssue", ...],
    ) -> tuple["JsonSchemaIssue", ...]:
        return tuple(
            sorted(
                issues,
                key=lambda i: (i.resource_uri, i.json_path, i.code, str(i.schema_path)),
            )
        )

    def _cache_schema(self, key: str, compiled: "Compiled") -> None:
        if (
            not self._policy.max_cache_entries
            or compiled.size > self._policy.max_cache_bytes
        ):
            return
        while self._cache and (
            len(self._cache) >= self._policy.max_cache_entries
            or self._bytes + compiled.size > self._policy.max_cache_bytes
        ):
            _, removed = self._cache.popitem(last=False)
            self._bytes -= removed.size
            self._evictions += 1
        self._cache[key] = compiled
        self._bytes += compiled.size
