"""Ordered execution plan на свежем catalog и одном read-only ConstraintReader."""

from typing import Literal

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import (
    LoadOperation,
    NullScalar,
    ValidationDecision,
)
from structuraguard.contracts.constraint_validation import (
    ConstraintLookup,
    ConstraintReadRequest,
    ConstraintTablePolicy,
    ConstraintValidationPolicy,
)
from structuraguard.contracts.database import (
    CatalogColumnRef,
    ColumnCatalog,
    DatabaseCatalog,
    TableCatalog,
)
from structuraguard.contracts.loading import (
    DryRunExecutionPlan,
    DryRunPolicy,
    ExecutionStep,
)
from structuraguard.contracts.record_validation import ValidationRecord
from structuraguard.domain.constraint_semantics import unique_keys
from structuraguard.mapping import MappingPlanValidator
from structuraguard.ports.validation import ConstraintReader
from structuraguard.validation import DatabaseConstraintValidator
from structuraguard.validation.business_rules import scalar_key

from .projection import Prepared, failure
from .relations import verify_mapped_parents


def _needs_evaluation(column: ColumnCatalog) -> bool:
    info = column.inspection
    if info is None:
        return True
    if info.default is not None or info.identity is not None or column.generated:
        return True
    data_type = info.data_type
    while True:
        if data_type.domain_default is not None:
            return True
        if data_type.base_type is None:
            return False
        data_type = data_type.base_type


def _dependencies(
    rows: tuple[ValidationRecord, ...], tables: dict[str, TableCatalog], max_work: int
) -> dict[str, set[str]]:
    dependencies: dict[str, set[str]] = {r.record_id: set() for r in rows}
    values = {r.record_id: {c.field_id: c.value for c in r.values} for r in rows}
    specifications = {
        (fk.referenced_table_id, fk.referenced_column_ids)
        for t in tables.values()
        for fk in t.foreign_keys
    }
    incoming: dict[
        tuple[str, tuple[str, ...], tuple[tuple[str, object], ...]], list[str]
    ] = {}
    work = 0
    for tid, ids in sorted(specifications):
        for row in rows:
            work += 1
            if work > max_work:
                raise failure("SECURITY_LIMIT_EXCEEDED")
            cells = values[row.record_id]
            if row.collection_id == tid and all(
                cid in cells and not isinstance(cells[cid], NullScalar) for cid in ids
            ):
                key = (tid, ids, tuple(scalar_key(cells[cid]) for cid in ids))
                incoming.setdefault(key, []).append(row.record_id)
    for row in rows:
        cells = values[row.record_id]
        for fk in tables[row.collection_id].foreign_keys:
            work += 1
            if work > max_work:
                raise failure("SECURITY_LIMIT_EXCEEDED")
            if all(
                cid in cells and not isinstance(cells[cid], NullScalar)
                for cid in fk.column_ids
            ):
                key = (
                    fk.referenced_table_id,
                    fk.referenced_column_ids,
                    tuple(scalar_key(cells[cid]) for cid in fk.column_ids),
                )
                dependencies[row.record_id].update(incoming.get(key, ()))
                work += len(dependencies[row.record_id])
    if work > max_work:
        raise failure("SECURITY_LIMIT_EXCEEDED")
    return dependencies


def _quarantine_groups(
    prepared: Prepared, codes: dict[str, set[str]], dependencies: dict[str, set[str]]
) -> None:
    # Один исходный record и его связи неделимы. FK-зависимости также не должны
    # оставить дочернюю запись после исключения нового parent.
    peers: dict[str, set[str]] = {}
    for uid, origin in prepared.origins.items():
        peers.setdefault(origin.record_id, set()).add(uid)
    graph = {uid: set(deps) for uid, deps in dependencies.items()}
    for group in peers.values():
        anchor = min(group)
        for uid in group:
            graph[uid].add(anchor)
            graph[anchor].add(uid)
    for batch in prepared.request.batches:
        for record in batch.records:
            for related in (
                *record.related_record_ids,
                *((record.parent_record_id,) if record.parent_record_id else ()),
            ):
                if related not in peers:
                    raise failure("DRY_RUN_PROJECTION_AMBIGUOUS")
                a, b = min(peers[record.record_id]), min(peers[related])
                graph[a].add(b)
                graph[b].add(a)
    for uid, deps in dependencies.items():
        for parent in deps:
            graph[parent].add(uid)
    pending = [uid for uid, issues in codes.items() if issues]
    rejected = set(pending)
    while pending:
        for peer in graph[pending.pop()]:
            if peer not in rejected:
                rejected.add(peer)
                codes[peer].add("DRY_RUN_DEPENDENCY_QUARANTINED")
                pending.append(peer)


async def build_plan(
    prepared: Prepared,
    *,
    catalog: DatabaseCatalog,
    policy: DryRunPolicy,
    reader: ConstraintReader,
    server_values: frozenset[CatalogColumnRef] = frozenset(),
) -> DryRunExecutionPlan:
    """Повторить M11/M12 и классифицировать units без выдачи SQL/parameters.

    Reader работает в одной transaction: dry-run задаёт SERIALIZABLE READ ONLY;
    writer фиксирует write tables locks, а гонки lookup-only parent разрешает FK
    при DML. Оба adapters создают reader сами и не принимают его извне.
    """
    mapping = prepared.request.mapping
    validation = await MappingPlanValidator(policy=policy.mapping_policy).validate(
        mapping, prepared.manifest, catalog, profile=prepared.profile
    )
    if (
        validation.decision is not ValidationDecision.ACCEPTED
        or validation.evidence is None
        or validation.validated_plan is None
    ):
        raise failure("DRY_RUN_MAPPING_REJECTED")
    evidence = validation.evidence
    verify_mapped_parents(prepared, policy.read_policy.limits.max_evaluations)
    tables = {t.table_id: t for s in catalog.schemas for t in s.tables}
    identities = {i.table_id: i.column_ids for i in evidence.identities}
    limits = policy.read_policy.limits
    constraint_policy = ConstraintValidationPolicy(
        target_id=catalog.target_id,
        target_policy_fingerprint=catalog.target_policy_fingerprint,
        database_fingerprint=catalog.database_fingerprint,
        tables=tuple(
            ConstraintTablePolicy(
                table_id=tid,
                operation="upsert"
                if mapping.operation is LoadOperation.UPSERT
                else "insert",
                identity_column_ids=identities[tid]
                if mapping.operation is LoadOperation.UPSERT
                else (),
            )
            for tid in evidence.load_order
        ),
        allow_columns=policy.read_policy.allow_columns,
        source_identity_allow=policy.mapping_policy.source_identity_allow,
        checks=policy.checks,
        limits=limits,
    )
    report = await DatabaseConstraintValidator(
        constraint_policy, reader=reader
    ).validate(prepared.data, catalog=catalog)
    codes: dict[str, set[str]] = {r.record_id: set() for r in prepared.data.records}
    blockers: set[str] = set()
    recoverable = {
        "DB_NOT_NULL",
        "DB_TYPE_MISMATCH",
        "DB_LENGTH",
        "DB_ENUM",
        "DB_VALUE_INVALID",
        "DB_NUMERIC_BOUNDS",
        "DB_NUMERIC_SCALE",
        "DB_CHECK",
        "DB_UNIQUE",
        "DB_FOREIGN_KEY",
    }
    for issue in report.issues:
        if issue.record_id in codes:
            codes[issue.record_id].add(issue.code)
        if issue.code not in recoverable or issue.record_id not in codes:
            blockers.add(issue.code)
    for row in prepared.data.records:
        table = tables[row.collection_id]
        supplied = {c.field_id for c in row.values}
        if any(
            c.column_id not in supplied
            and _needs_evaluation(c)
            and CatalogColumnRef(table_id=table.table_id, column_id=c.column_id)
            not in server_values
            for c in table.columns
        ):
            codes[row.record_id].add("DRY_RUN_DEFAULT_UNVERIFIED")
            blockers.add("DRY_RUN_DEFAULT_UNVERIFIED")
        key_columns = {
            cid for key in unique_keys(table, catalog.dialect) for cid in key.column_ids
        }
        if mapping.operation is LoadOperation.UPSERT and supplied & key_columns - set(
            identities[row.collection_id]
        ):
            codes[row.record_id].add("DRY_RUN_KEY_UPDATE_UNVERIFIED")
            blockers.add("DRY_RUN_KEY_UPDATE_UNVERIFIED")
    dependencies = _dependencies(prepared.data.records, tables, limits.max_evaluations)
    _quarantine_groups(prepared, codes, dependencies)
    if any(codes.values()) and policy.error_policy == "atomic":
        blockers.add("DRY_RUN_ATOMIC_REJECTED")
    lookups: list[ConstraintLookup] = []
    if mapping.operation is LoadOperation.UPSERT:
        for row in prepared.data.records:
            if codes[row.record_id]:
                continue
            cells = {c.field_id: c.value for c in row.values}
            ids = identities[row.collection_id]
            if not all(cid in cells for cid in ids):
                raise failure("DRY_RUN_IDENTITY_UNRESOLVED")
            lookups.append(
                ConstraintLookup(
                    lookup_id=row.record_id,
                    table_id=row.collection_id,
                    column_ids=ids,
                    values=tuple(cells[cid] for cid in ids),
                )
            )
    request = ConstraintReadRequest(
        target_id=catalog.target_id,
        target_policy_fingerprint=catalog.target_policy_fingerprint,
        database_fingerprint=catalog.database_fingerprint,
        lookups=tuple(lookups),
    )
    matches = await reader.read(request, catalog=catalog)
    if matches.request_fingerprint != request.fingerprint or tuple(
        m.lookup_id for m in matches.matches
    ) != tuple(k.lookup_id for k in lookups):
        raise failure("DRY_RUN_READER_INVALID")
    exists = {m.lookup_id: m.exists for m in matches.matches}
    order = {tid: i for i, tid in enumerate(evidence.load_order)}
    steps: list[ExecutionStep] = []
    for row in sorted(
        prepared.data.records, key=lambda r: (order[r.collection_id], r.record_id)
    ):
        uid, tid = row.record_id, row.collection_id
        ids = identities[tid]
        mutable = tuple(
            c.field_id
            for c in row.values
            if c.field_id not in ids and c.field_id not in tables[tid].primary_key
        )
        action: Literal["insert", "update", "skip", "quarantine"] = (
            "quarantine"
            if codes[uid]
            else ("update" if mutable else "skip")
            if exists.get(uid, False)
            else "insert"
        )
        origin = prepared.origins[uid]
        steps.append(
            ExecutionStep(
                unit_id=uid,
                record_id=origin.record_id,
                entity_id=origin.entity_id,
                value_ids=origin.value_ids,
                table_id=tid,
                column_ids=tuple(c.field_id for c in row.values),
                identity_column_ids=ids,
                update_column_ids=mutable,
                dependencies=tuple(sorted(dependencies[uid])),
                action=action,
                codes=tuple(sorted(codes[uid])),
            )
        )
    return DryRunExecutionPlan(
        target_id=catalog.target_id,
        database_fingerprint=catalog.database_fingerprint,
        target_policy_fingerprint=catalog.target_policy_fingerprint,
        normalized_fingerprint=prepared.manifest.normalized_fingerprint,
        mapping_fingerprint=mapping.fingerprint,
        mapping_validation_fingerprint=validation.validated_plan.validation_fingerprint,
        projection_fingerprint=canonical_sha256_value(prepared.data.canonical_json()),
        policy_fingerprint=canonical_sha256_value(policy.canonical_json()),
        validation_fingerprint=canonical_sha256_value(report.canonical_json()),
        read_snapshot_fingerprint=canonical_sha256_value(
            (report.read_snapshot_fingerprint, matches.snapshot_fingerprint)
        ),
        table_order=evidence.load_order,
        steps=tuple(steps),
        blockers=tuple(sorted(blockers)),
    )
