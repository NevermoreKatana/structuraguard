"""Асинхронный bounded deterministic mapper, потребляющий готовые snapshots."""

import asyncio
from decimal import Decimal

from pydantic import ValidationError

from structuraguard.contracts.database import CatalogColumnRef, DatabaseCatalog
from structuraguard.contracts.deterministic_mapping import (
    DeterministicMappingOptions,
    DeterministicMappingResult,
    MappingScope,
)
from structuraguard.contracts.normalized import SemanticFieldRef
from structuraguard.contracts.profiling import NormalizedDataProfile
from structuraguard.contracts.semantic_catalog import DatabaseSemanticCatalog

from ._compatibility import expected_patterns
from ._context import MappingContext
from ._inputs import Budget, bounded_size, failure, validate_inputs
from ._names import Name, normalize_name
from ._ranking import (
    Ranked,
    Source,
    Target,
    TopCandidates,
    base_evidence,
    score_signals,
)
from ._results import build_result
from ._scores import difference


def _sources(
    profile: NormalizedDataProfile, options: DeterministicMappingOptions
) -> tuple[Source, ...]:
    result = []
    for field in sorted(
        profile.fields, key=lambda f: (f.field.entity_type, f.field.field_name)
    ):
        names = tuple(
            normalize_name(
                name, max_bytes=options.max_name_bytes, max_tokens=options.max_tokens
            )
            for name in sorted({field.field.field_name, *field.source_names})
        )
        labels = {
            label.text.rsplit(".", 1)[0]
            if label.kind == "file_name" and "." in label.text
            else label.text
            for label in field.labels
            if label.kind != "source_name"
        }
        if field.field.entity_type not in {"row", "entity"}:
            labels.add(field.field.entity_type)
        context = tuple(
            normalize_name(
                label, max_bytes=options.max_name_bytes, max_tokens=options.max_tokens
            )
            for label in sorted(labels)
        )
        result.append(Source(field, names, context))
    return tuple(result)


def _targets(
    catalog: DatabaseCatalog,
    scope: MappingScope,
    semantic: DatabaseSemanticCatalog,
    options: DeterministicMappingOptions,
) -> tuple[tuple[Target, ...], frozenset[CatalogColumnRef]]:
    known = {
        CatalogColumnRef(table_id=t.table_id, column_id=c.column_id)
        for s in catalog.schemas
        for t in s.tables
        for c in t.columns
    }
    if not set(scope.allow) <= known or not set(scope.deny) <= known:
        raise failure("MAPPING_BINDING_MISMATCH", "unknown_scope_reference")
    allowed = frozenset(set(scope.allow) - set(scope.deny))
    semantics = {(t.schema_name, t.table_name): t for t in semantic.tables}
    result: list[Target] = []
    metadata_allowed: set[CatalogColumnRef] = set()
    for schema in catalog.schemas:
        for table in schema.tables:
            if catalog.dialect == "postgresql" and (
                schema.name == "information_schema" or schema.name.startswith("pg_")
            ):
                continue
            if catalog.dialect == "sqlite" and table.name.casefold().startswith(
                "sqlite_"
            ):
                continue
            if not table.writable:
                continue
            entry = semantics.get((table.schema_name, table.name))
            columns = {c.column_name: c for c in entry.columns} if entry else {}
            table_names: tuple[Name, ...] = ()
            for column in table.columns:
                ref = CatalogColumnRef(
                    table_id=table.table_id, column_id=column.column_id
                )
                if ref not in allowed:
                    continue
                metadata_allowed.add(ref)
                if not column.writable or column.generated:
                    continue
                if not table_names:
                    table_names = tuple(
                        normalize_name(
                            name,
                            max_bytes=options.max_name_bytes,
                            max_tokens=options.max_tokens,
                        )
                        for name in (table.name, *(entry.aliases if entry else ()))
                    )
                annotation = columns.get(column.name)
                result.append(
                    Target(
                        table,
                        column,
                        ref,
                        normalize_name(
                            column.name,
                            max_bytes=options.max_name_bytes,
                            max_tokens=options.max_tokens,
                        ),
                        tuple(
                            normalize_name(
                                a,
                                max_bytes=options.max_name_bytes,
                                max_tokens=options.max_tokens,
                            )
                            for a in (annotation.aliases if annotation else ())
                        ),
                        table_names,
                        expected_patterns(
                            column, annotation.semantic_type if annotation else None
                        ),
                        entry,
                    )
                )
                if len(result) > options.max_columns:
                    raise failure("MAPPING_LIMIT_EXCEEDED", "target_columns")
    return tuple(sorted(result, key=lambda t: t.key)), frozenset(metadata_allowed)


class DeterministicMapper:
    """Ранжировать semantic fields по локальному evidence; I/O отсутствует.

    Args:
        options: Immutable weights, thresholds и ceilings одного вызова.

    Raises:
        MappingError: Некорректные options или snapshots, binding или budget.

    Один экземпляр пригоден для concurrent calls; retained state локален.
    """

    def __init__(self, options: DeterministicMappingOptions | None = None) -> None:
        try:
            if options is None:
                options = DeterministicMappingOptions()
            bounded_size(options, 65536)
            self._options = DeterministicMappingOptions.model_validate(
                options.model_dump(mode="python")
            )
        except (ValidationError, TypeError, ValueError, AttributeError):
            raise failure("MAPPING_INPUT_INVALID", "options") from None

    @property
    def options(self) -> DeterministicMappingOptions:
        """Вернуть проверенные immutable options без чтения env или файлов.

        Конфигурация одинакова для вызовов экземпляра; budgets и retained state
        создаются заново внутри каждого rank. Для других weights нужен новый mapper.
        """
        return self._options

    async def rank(
        self,
        profile: NormalizedDataProfile,
        catalog: DatabaseCatalog,
        *,
        scope: MappingScope,
        semantic_catalog: DatabaseSemanticCatalog | dict[str, object] | None = None,
    ) -> DeterministicMappingResult:
        """Вернуть top-k и ambiguity; не генерировать MappingPlan/SQL.

        Args:
            profile: Завершённый M8 profile schema 1.0.0 с lineage и агрегатами.
                Raw samples не участвуют в scoring.
            catalog: M7 catalog schema 1.1.0 / catalog-v1 для SQLite/PostgreSQL.
                FK graph должен быть согласован с metadata.
            scope: Явный trusted allowlist/denylist точных catalog refs.
                Пустой allow запрещает все targets; deny имеет приоритет.
            semantic_catalog: Локальные hints как DTO либо dict того же wire
                формата. None означает отсутствие пользовательских annotations.

        Returns:
            Immutable результат для каждого SemanticFieldRef: до top_k candidates,
            SDK scores/breakdown, bindings, явные ambiguity и recommendation status.
            Несовместимые targets исключаются; blockers запрещают auto recommendation.

        Raises:
            MappingError: Некорректный snapshot/options, неподдержанная schema,
                несогласованный binding/semantic catalog либо превышенный budget.
                Коды MAPPING_INPUT_INVALID, MAPPING_UNSUPPORTED_SCHEMA,
                MAPPING_BINDING_MISMATCH, MAPPING_SEMANTIC_CATALOG_INVALID,
                MAPPING_LIMIT_EXCEEDED обозначают полный отказ без partial result.
            DatabaseInspectionError: DATABASE_SCHEMA_DRIFT при неверном hash каталога.
            asyncio.CancelledError: Отмена на checkpoint между блоками работы.

        Side effects:
            Файлы, сеть, БД и LLM не используются; входные DTO не меняются.
            Вызов отдаёт управление event loop, состояние и счётчики локальны.

        Security:
            Полный result чувствителен; для logs используйте safe_summary().
            Hash не удостоверяет происхождение snapshot. Grants, свежесть живой
            БД, identity/FK strategy и разрешение на загрузку проверяет caller
            на последующих этапах; auto_candidate такого разрешения не выдаёт.
        """
        await asyncio.sleep(0)
        options = self._options
        profile, catalog, scope, semantic, input_size = validate_inputs(
            profile, catalog, scope, semantic_catalog, options
        )
        budget = Budget(options)
        budget.retain(input_size)
        sources = _sources(profile, options)
        targets, allowed = _targets(catalog, scope, semantic, options)
        if 2 * len(sources) * len(targets) > options.max_pairs:
            raise failure("MAPPING_LIMIT_EXCEEDED", "candidate_pairs")
        budget.retain(
            len(sources) * 8192
            + len(targets) * 4096
            + sum(len(t.aliases) * 2048 for t in targets)
        )
        anchors: dict[SemanticFieldRef, CatalogColumnRef] = {}
        operations = 0
        for source in sources:
            best: list[
                tuple[Decimal, tuple[str, str, str, str, str], CatalogColumnRef]
            ] = []
            for target in targets:
                budget.work(len(source.names) * (1 + len(target.aliases)))
                evidence = base_evidence(source, target, catalog.dialect)
                lexical = max(evidence.names.score, evidence.alias)
                if (
                    evidence.compatibility.status == "compatible"
                    and not evidence.compatibility.blockers
                    and not evidence.pattern.blockers
                    and not target.name.confusable
                ):
                    best.append((lexical, target.key, target.ref))
                    best.sort(key=lambda p: (p[0].copy_negate(), p[1]))
                    del best[2:]
                operations += 1
                if operations % 128 == 0:
                    await asyncio.sleep(0)
            if (
                best
                and best[0][0] >= Decimal("0.95")
                and not all(n.generic for n in source.names)
                and not any(n.confusable for n in source.names)
                and (
                    len(best) == 1
                    or difference(best[0][0], best[1][0]) >= Decimal("0.10")
                )
            ):
                anchors[source.profile.field] = best[0][2]
        graph = catalog.dependency_graph
        assert graph is not None
        context = MappingContext(
            profile.fields, profile.relationships, graph, anchors, allowed, budget
        )
        retained: list[tuple[Source, TopCandidates, set[str]]] = []
        for source in sources:
            top = TopCandidates(max(2, options.top_k))
            reasons: set[str] = set()
            for target in targets:
                budget.work(len(source.names) * (1 + len(target.aliases)))
                evidence = base_evidence(source, target, catalog.dialect)
                if evidence.compatibility.status == "incompatible":
                    reasons.add("TYPE_INCOMPATIBLE")
                    reasons.update(evidence.compatibility.blockers)
                else:
                    contextual = context.score(source, target)
                    if any(
                        (
                            evidence.names.score,
                            evidence.alias,
                            evidence.pattern.score,
                            contextual.structure,
                            contextual.graph,
                        )
                    ):
                        blockers = (
                            set(evidence.compatibility.blockers)
                            | set(evidence.pattern.blockers)
                            | set(contextual.blockers)
                        )
                        if (
                            any(n.confusable for n in source.names)
                            or target.name.confusable
                        ):
                            blockers.add("CONFUSABLE_NAME")
                        if (
                            all(n.generic for n in source.names)
                            and not evidence.alias
                            and not contextual.structure
                            and not contextual.graph
                        ):
                            blockers.add("GENERIC_NAME_ONLY")
                        if (contextual.structure or contextual.graph) and any(
                            r in profile.reasons
                            for r in ("pair_limit", "context_limit")
                        ):
                            blockers.add("CONTEXT_EVIDENCE_INCOMPLETE")
                        signals = score_signals(
                            evidence,
                            options.weights,
                            structure=contextual.structure,
                            graph=contextual.graph,
                            structure_available=contextual.structure_available,
                            graph_available=contextual.graph_available,
                        )
                        top.add(
                            Ranked(
                                target,
                                evidence,
                                signals,
                                tuple(sorted(blockers)),
                                contextual.foreign_keys,
                                context.identity_evidence(source, target),
                            )
                        )
                operations += 1
                if operations % 128 == 0:
                    await asyncio.sleep(0)
            budget.retain(len(top.heap) * 8192)
            retained.append((source, top, reasons))
        result = build_result(
            profile, catalog, scope, semantic, options, retained, budget
        )
        await asyncio.sleep(0)
        return result
