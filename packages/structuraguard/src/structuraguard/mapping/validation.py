"""Чистая независимая проверка MappingPlan: все применимые issues до acceptance."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from pydantic import ValidationError as ModelError

from structuraguard.contracts._base import CanonicalValue, canonical_sha256_value
from structuraguard.contracts.common import ValidationDecision
from structuraguard.contracts.database import DatabaseCatalog, MappingPolicyRef
from structuraguard.contracts.mapping import (
    MappingPlan,
    MappingPlanValidationRequest,
    MappingPlanValidationResult,
    ValidatedMappingPlan,
)
from structuraguard.contracts.mapping_rules import (
    MappingIdentity,
    MappingIssueLocation,
    MappingValidationEvidence,
)
from structuraguard.contracts.mapping_validation import (
    MappingPlanInputReport,
    MappingValidationOptions,
    MappingValidationPolicy,
)
from structuraguard.contracts.normalized import NormalizedDatasetManifest
from structuraguard.contracts.profiling import NormalizedDataProfile
from structuraguard.domain.database_fingerprint import database_fingerprint
from structuraguard.exceptions import DatabaseInspectionError, ValidationError

from ._validation_identity import check_identity
from ._validation_input import PlanParts, catalog_limits, decode, parse_parts, preflight
from ._validation_relations import check_relations
from ._validation_report import Issues, failure
from ._validation_scope import check_mapping, check_policy, required
from ._validation_types import check_type


class MappingPlanValidator:
    """Проверить MappingPlan по готовым снимкам без I/O и полномочий на запись.

    Args:
        policy: Доверенная policy приложения; scope и порог не берутся из плана.
        options: Лимиты одного вызова; None выбирает MappingValidationOptions().
        clock: Часы, возвращающие timezone-aware UTC datetime. По умолчанию
            используется текущее UTC время; оно исключено из fingerprint.

    Raises:
        structuraguard.exceptions.ValidationError: Непригодная policy/options
            или превышение лимита при их проверке.

    Успех подтверждает декларацию относительно переданных снимков. Проверка
    живой БД, grants и значений записей, staging и транзакция остаются задачами
    будущего loader. Hash не удостоверяет происхождение снимков или policy.
    """

    def __init__(
        self,
        *,
        policy: MappingValidationPolicy,
        options: MappingValidationOptions | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(policy) is not MappingValidationPolicy or (
            options is not None and type(options) is not MappingValidationOptions
        ):
            raise failure("MAPPING_PLAN_INVALID")
        try:
            # Shape проверяется повторно ниже. Serializer warnings раскрывают raw
            # input повреждённого model_copy/model_construct ещё до этой проверки.
            self._options = MappingValidationOptions.model_validate(
                (options or MappingValidationOptions()).model_dump(
                    mode="python", warnings=False
                )
            )
            preflight(policy, self._options)
            self._policy = MappingValidationPolicy.model_validate(
                policy.model_dump(mode="python", warnings=False)
            )
        except ModelError:
            raise failure("MAPPING_POLICY_MISMATCH") from None
        self._clock = clock

    async def validate(
        self,
        plan: MappingPlan,
        manifest: NormalizedDatasetManifest,
        catalog: DatabaseCatalog,
        *,
        profile: NormalizedDataProfile | None = None,
    ) -> MappingPlanValidationResult | MappingPlanInputReport:
        """Собрать все применимые независимые нарушения готового DTO плана.

        Args:
            plan: Декларация MappingPlan 1.0.0/1.1.0; форма и hash проверяются снова.
            manifest: Связанный normalized manifest версии 1.1.0/1.2.0.
            catalog: Снимок catalog-v1 для PostgreSQL или SQLite.
            profile: Необязательный профиль M8 того же manifest для проверки типов.

        Returns:
            MappingPlanValidationResult с issues и evidence; wrapper присутствует
            только при ACCEPTED. Повреждённая форма или hash плана дают
            MappingPlanInputReport без wrapper. Зависимые проверки непригодной
            части пропускаются, остальные продолжаются в фиксированном порядке.

        Raises:
            structuraguard.exceptions.ValidationError: Непригодный снимок или тип
                аргумента, неподдержанный каталог либо превышение лимита.
                Причина доступна в error_code; частичный result не возвращается.
            pydantic.ValidationError: Часы вернули непригодное значение validated_at.
            asyncio.CancelledError: Отмена coroutine; исключение не подавляется.

        Side effects:
            Снимки не изменяются; БД, файлы, сеть и LLM не вызываются.

        Security:
            Полный result с планом и bindings чувствителен. Для диагностики
            предназначены codes и числовые locations без исходных значений.
            ACCEPTED не является разрешением на запись и не закрывает schema drift
            после получения каталога.
        """
        if type(plan) is not MappingPlan:
            raise failure("MAPPING_PLAN_INVALID")
        preflight((plan, manifest, catalog, profile), self._options)
        issues = Issues(self._options)
        parts = parse_parts(plan.model_dump(mode="python", warnings=False), issues)
        return await self._validate(parts, manifest, catalog, profile, issues)

    async def validate_json(
        self,
        payload: bytes,
        manifest: NormalizedDatasetManifest,
        catalog: DatabaseCatalog,
        *,
        profile: NormalizedDataProfile | None = None,
    ) -> MappingPlanValidationResult | MappingPlanInputReport:
        """Проверить недоверенный JSON, собирая ошибки пригодных частей плана.

        Args:
            payload: JSON MappingPlan в bytes; это содержимое, а не путь к файлу.
            manifest: Связанный normalized manifest версии 1.1.0/1.2.0.
            catalog: Снимок catalog-v1 для PostgreSQL или SQLite.
            profile: Необязательный профиль M8 того же manifest.

        Returns:
            Тот же result, что у validate, для корректной формы плана. При ошибке
            формы/hash — MappingPlanInputReport. Нечитаемый JSON даёт complete=False
            до проверки снимков; complete=True означает проверку пригодных частей,
            а не всех semantic rules. Отчёт не содержит исходный payload.

        Raises:
            structuraguard.exceptions.ValidationError: Превышение лимита, неверный
                тип аргумента, непригодный снимок или неподдержанный каталог.
                error_code уточняет причину; усечённый result не возвращается.
            pydantic.ValidationError: Часы вернули непригодное значение validated_at.
            asyncio.CancelledError: Отмена coroutine; исключение не подавляется.

        Side effects:
            Снимки не изменяются; текст не исполняется, I/O и LLM отсутствуют.

        Security:
            Действуют явная policy и конечные лимиты, включая размер результата.
            Полный semantic result чувствителен и не разрешает исполнение плана.
        """
        if type(payload) is not bytes:
            raise failure("MAPPING_PLAN_INVALID")
        issues = Issues(self._options)
        try:
            raw = decode(payload, self._options)
        except ValidationError as error:
            if error.error_code == "MAPPING_LIMIT_EXCEEDED":
                raise
            issues.add("MAPPING_PLAN_INVALID")
            found, locations = issues.ordered()
            report = MappingPlanInputReport(
                complete=False, issues=found, locations=locations
            )
            self._check_result_size(report)
            return report
        parts = parse_parts(raw, issues)
        return await self._validate(parts, manifest, catalog, profile, issues)

    async def _validate(
        self,
        parts: PlanParts,
        manifest: NormalizedDatasetManifest,
        catalog: DatabaseCatalog,
        profile: NormalizedDataProfile | None,
        issues: Issues,
    ) -> MappingPlanValidationResult | MappingPlanInputReport:
        await asyncio.sleep(0)
        if (
            type(manifest) is not NormalizedDatasetManifest
            or type(catalog) is not DatabaseCatalog
            or (profile is not None and type(profile) is not NormalizedDataProfile)
        ):
            raise failure("MAPPING_PLAN_INVALID")
        preflight((manifest, catalog, profile), self._options)
        catalog_limits(catalog, self._options)
        try:
            manifest = NormalizedDatasetManifest.model_validate(
                manifest.model_dump(mode="python", warnings=False)
            )
            catalog = DatabaseCatalog.model_validate(
                catalog.model_dump(mode="python", warnings=False)
            )
            if profile is not None:
                profile = NormalizedDataProfile.model_validate(
                    profile.model_dump(mode="python", warnings=False)
                )
        except ModelError:
            raise failure("MAPPING_PLAN_INVALID") from None
        tables = {t.table_id: t for s in catalog.schemas for t in s.tables}
        column_count = sum(len(t.columns) for t in tables.values())
        edge_count = sum(len(t.foreign_keys) for t in tables.values())
        issues.work(
            (len(parts.mappings) + len(parts.relations) + 1)
            * (
                column_count
                + edge_count
                + len(self._policy.scope.allow)
                + len(self._policy.scope.deny)
                + len(self._policy.allow_tables)
                + len(self._policy.deny_tables)
                + 1
            )
        )
        if catalog.dialect not in {"postgresql", "sqlite"}:
            raise failure("DATABASE_METADATA_UNSUPPORTED")
        try:
            actual_db = database_fingerprint(catalog)
        except DatabaseInspectionError:
            raise failure("DATABASE_METADATA_UNSUPPORTED") from None
        if actual_db != catalog.database_fingerprint:
            issues.add("MAPPING_CATALOG_FINGERPRINT_MISMATCH")
        if actual_db != parts.payload.get("database_fingerprint"):
            issues.add("DATABASE_SCHEMA_DRIFT")
        expected = (
            ("source_fingerprint", manifest.source.source_fingerprint),
            ("extraction_fingerprint", manifest.extraction_fingerprint),
            ("parse_plan_fingerprint", manifest.parse_plan_fingerprint),
            ("normalized_fingerprint", manifest.normalized_fingerprint),
        )
        for index, (name, value) in enumerate(expected):
            if parts.payload.get(name) != value:
                issues.add(
                    "MAPPING_SOURCE_LINEAGE_MISMATCH",
                    MappingIssueLocation(section="manifest", index=index),
                )
        if manifest.schema_version not in {"1.1.0", "1.2.0"}:
            issues.add("MAPPING_SOURCE_LINEAGE_MISMATCH")
        if (
            parts.payload.get("target_id") != catalog.target_id
            or self._policy.scope.target_id != catalog.target_id
        ):
            issues.add("MAPPING_TARGET_MISMATCH")
        if (
            parts.payload.get("target_policy_fingerprint")
            != catalog.target_policy_fingerprint
            or self._policy.scope.target_policy_fingerprint
            != catalog.target_policy_fingerprint
        ):
            issues.add("MAPPING_POLICY_MISMATCH")
        if profile is not None and (
            profile.source != manifest.source
            or profile.extraction_fingerprint != manifest.extraction_fingerprint
            or profile.parse_plan_fingerprint != manifest.parse_plan_fingerprint
            or profile.normalized_manifest_fingerprint
            != manifest.normalized_fingerprint
        ):
            issues.add(
                "MAPPING_SOURCE_LINEAGE_MISMATCH",
                MappingIssueLocation(section="profile"),
            )
            profile = None
        check_policy(catalog, self._policy, issues)
        mappings = tuple(m for _, m in parts.mappings)
        field_profiles = (
            {f.field: f for f in profile.fields} if profile is not None else {}
        )
        for index, mapping in parts.mappings:
            await asyncio.sleep(0)
            loc = MappingIssueLocation(section="mappings", index=index)
            resolved = check_mapping(
                index, mapping, manifest, tables, self._policy, catalog.dialect, issues
            )
            if profile is not None and mapping.source not in field_profiles:
                issues.add("MAPPING_TYPE_UNVERIFIED", loc)
            if (
                resolved is not None
                and (semantic := manifest.semantic_type_for(mapping.source)) is not None
            ):
                check_type(
                    resolved[1],
                    semantic,
                    field_profiles.get(mapping.source),
                    catalog.dialect,
                    loc,
                    issues,
                )
            if mapping.confidence < self._policy.confidence_threshold:
                issues.add("MAPPING_CONFIDENCE_BELOW_THRESHOLD", loc)
        if (
            parts.confidence is not None
            and parts.confidence < self._policy.confidence_threshold
        ):
            issues.add("MAPPING_CONFIDENCE_BELOW_THRESHOLD")
        if (
            parts.operation is not None
            and parts.operation not in self._policy.allowed_operations
        ):
            issues.add("MAPPING_OPERATION_FORBIDDEN")
        selected = sorted({m.target.table_id for m in mappings} & tables.keys())
        resolved_identities: list[MappingIdentity] = []
        for index, table_id in enumerate(selected):
            await asyncio.sleep(0)
            table = tables[table_id]
            covered = {
                m.target.column_id for m in mappings if m.target.table_id == table_id
            }
            if (
                len(
                    {
                        m.source.entity_type
                        for m in mappings
                        if m.target.table_id == table_id
                    }
                )
                > 1
            ):
                issues.add(
                    "MAPPING_TARGET_AMBIGUOUS",
                    MappingIssueLocation(section="catalog", index=index),
                )
            for component, column in enumerate(
                sorted(table.columns, key=lambda c: c.column_id)
            ):
                if required(column) and column.column_id not in covered:
                    issues.add(
                        "MAPPING_REQUIRED_TARGET_MISSING",
                        MappingIssueLocation(
                            section="catalog", index=index, component=component
                        ),
                    )
            identity = check_identity(
                table,
                mappings,
                tuple(identity for _, identity in parts.identities),
                parts.operation,
                self._policy,
                MappingIssueLocation(section="catalog", index=index),
                issues,
            )
            if identity is not None:
                resolved_identities.append(identity)
        for index, identity in parts.identities:
            if identity.table_id not in selected:
                issues.add(
                    "MAPPING_UPSERT_KEY_INVALID",
                    MappingIssueLocation(section="identities", index=index),
                )
        order = check_relations(
            mappings,
            parts.relations,
            tables,
            catalog,
            manifest,
            profile,
            self._policy,
            issues,
        )
        found, locations = issues.ordered()
        if parts.plan is None:
            # Payload приходит только из JSON decoder либо recursive model_dump;
            # preflight уже отклонил произвольные объекты и чрезмерные числа.
            report = MappingPlanInputReport(
                complete=True,
                issues=found,
                locations=locations,
                payload_fingerprint=canonical_sha256_value(
                    cast(CanonicalValue, parts.payload)
                ),
            )
            self._check_result_size(report)
            return report
        plan = parts.plan
        evidence = MappingValidationEvidence(
            policy_fingerprint=self._policy.fingerprint,
            options_fingerprint=self._options.fingerprint,
            actual_database_fingerprint=actual_db,
            profile_fingerprint=profile.profile_fingerprint if profile else None,
            manifest_fingerprint=manifest.normalized_fingerprint,
            catalog_target_id=catalog.target_id,
            catalog_policy_fingerprint=catalog.target_policy_fingerprint,
            profile_content_fingerprint=profile.normalized_data_fingerprint
            if profile
            else None,
            writable_fingerprint=canonical_sha256_value(
                tuple(
                    (
                        t,
                        tables[t].writable,
                        tuple(
                            (c.column_id, c.writable)
                            for c in sorted(
                                tables[t].columns, key=lambda c: c.column_id
                            )
                        ),
                    )
                    for t in sorted(tables)
                )
            ),
            locations=locations,
            load_order=order,
            identities=tuple(resolved_identities),
        )
        validated_at = self._clock() if self._clock is not None else datetime.now(UTC)
        validation_hash = canonical_sha256_value(
            {
                "plan": plan.fingerprint,
                "evidence": evidence.canonical_json(),
                "decision": issues.decision,
                "issues": tuple(i.canonical_json() for i in found),
                "validator_version": "1.0.0",
            }
        )
        common = dict(
            validator_id="mapping-plan-validator",
            validator_version="1.0.0",
            validated_at=validated_at,
            validation_fingerprint=validation_hash,
            plan_fingerprint=plan.fingerprint,
            normalized_fingerprint=plan.normalized_fingerprint,
            database_fingerprint=plan.database_fingerprint,
            target_id=plan.target_id,
            target_policy_fingerprint=plan.target_policy_fingerprint,
            issues=found,
            evidence=evidence,
        )
        checked = None
        if issues.decision is ValidationDecision.ACCEPTED:
            MappingPlanValidationRequest(
                plan=plan,
                manifest=manifest,
                catalog=catalog,
                policy=MappingPolicyRef(
                    policy_id=self._policy.policy_id,
                    policy_fingerprint=catalog.target_policy_fingerprint,
                ),
            )
            checked = ValidatedMappingPlan.model_validate({**common, "plan": plan})
        result = MappingPlanValidationResult.model_validate(
            {
                **common,
                "decision": issues.decision,
                "source_fingerprint": plan.source_fingerprint,
                "extraction_fingerprint": plan.extraction_fingerprint,
                "parse_plan_fingerprint": plan.parse_plan_fingerprint,
                "validated_plan": checked,
            }
        )
        self._check_result_size(result)
        return result

    def _check_result_size(
        self, result: MappingPlanValidationResult | MappingPlanInputReport
    ) -> None:
        preflight(result, self._options)
        if (
            len(result.canonical_json().encode("utf-8"))
            > self._options.max_result_bytes
        ):
            raise failure("MAPPING_LIMIT_EXCEEDED")
