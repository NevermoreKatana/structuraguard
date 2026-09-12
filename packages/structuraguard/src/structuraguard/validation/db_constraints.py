"""Catalog constraints и closed read-only prechecks; никакого ремонта значений."""

from dataclasses import dataclass

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.business_rules import (
    ComparisonRule,
    PresenceRule,
    RequiredIfRule,
)
from structuraguard.contracts.common import NormalizedScalar, NullScalar
from structuraguard.contracts.constraint_validation import (
    CheckRuleBinding,
    ConstraintLookup,
    ConstraintReadRequest,
    ConstraintReadResult,
    ConstraintTablePolicy,
    ConstraintValidationPolicy,
)
from structuraguard.contracts.database import DatabaseCatalog, TableCatalog
from structuraguard.contracts.record_validation import (
    RecordValidationLimits,
    RecordValidationResult,
    ValidationDataset,
    ValidationRecord,
)
from structuraguard.domain.constraint_semantics import UniqueKey, unique_keys
from structuraguard.domain.constraint_values import field_codes
from structuraguard.domain.database_fingerprint import verify_database_fingerprint
from structuraguard.ports.validation import ConstraintReader

from ._db_values import validate_fields
from ._rule_input import Collector, checked, failure
from .business_rules import BusinessRuleValidator, scalar_key


@dataclass(frozen=True)
class _Pending:
    lookup: ConstraintLookup
    row: ValidationRecord
    fields: tuple[str, ...]
    kind: str


class DatabaseConstraintValidator:
    """Собрать local DB issues и UNIQUE/FK относительно полного входа и reader.

    Args:
        policy: Доверенные catalog fingerprints, allowlists, CHECK bindings и limits.
        reader: Необязательный read-only adapter для prechecks состояния БД.

    Raises:
        ValidationError: Невалидная policy либо превышение budget её проверки.

    Policy trusted, catalog и records повторно проверяются. Без reader состояние
    БД unverified. Отчёт advisory; final constraints и TOCTOU принадлежат loader.
    """

    def __init__(
        self,
        policy: ConstraintValidationPolicy,
        *,
        reader: ConstraintReader | None = None,
    ) -> None:
        self._policy = checked(
            policy,
            ConstraintValidationPolicy,
            RecordValidationLimits(),
            code="DB_VALIDATION_INPUT_INVALID",
        )
        self._reader = reader

    async def validate(
        self, data: ValidationDataset, *, catalog: DatabaseCatalog
    ) -> RecordValidationResult:
        """Проверить завершённый EOF scope без изменения source, raw, traces или БД.

        Args:
            data: Полный dataset с catalog table/column IDs, подготовленный caller.
            catalog: Catalog того же target, policy и database fingerprint.

        Returns:
            Immutable RecordValidationResult со всеми независимыми issues.
            Непроверяемые ограничения и prechecks без reader дают unverified issue.

        Raises:
            ValidationError: Невалидные DTO, policy bindings, ответ reader или budget.
            DatabaseInspectionError: Schema drift, отказ доступа либо сбой
                поддерживаемого DB adapter; частичный успешный отчёт не выдаётся.

        Только reader выполняет read-only I/O. Результат advisory: гарантии
        UNIQUE/FK во время записи требуют транзакции loader и ограничений самой БД.
        """
        policy = self._policy
        data = checked(
            data, ValidationDataset, policy.limits, code="DB_VALIDATION_INPUT_INVALID"
        )
        catalog = checked(
            catalog, DatabaseCatalog, policy.limits, code="DB_VALIDATION_INPUT_INVALID"
        )
        if len(data.records) > policy.limits.max_records:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        if (
            catalog.target_id != policy.target_id
            or catalog.target_policy_fingerprint != policy.target_policy_fingerprint
            or catalog.database_fingerprint != policy.database_fingerprint
        ):
            raise failure("DB_CATALOG_BINDING_MISMATCH")
        verify_database_fingerprint(catalog, policy.database_fingerprint)
        state = _State(data, catalog, policy)
        state.local()
        await state.checks()
        state.keys()
        snapshot = None
        if state.pending and self._reader is not None:
            request = ConstraintReadRequest(
                target_id=policy.target_id,
                target_policy_fingerprint=policy.target_policy_fingerprint,
                database_fingerprint=policy.database_fingerprint,
                lookups=tuple(p.lookup for p in state.pending),
            )
            result = checked(
                await self._reader.read(request, catalog=catalog),
                ConstraintReadResult,
                policy.limits,
                code="DB_READER_RESULT_INVALID",
            )
            if (
                result.request_fingerprint != request.fingerprint
                or tuple(m.lookup_id for m in result.matches)
                != tuple(k.lookup_id for k in request.lookups)
                or result.snapshot_fingerprint
                != canonical_sha256_value(
                    (
                        request.fingerprint,
                        tuple(m.canonical_json() for m in result.matches),
                    )
                )
            ):
                raise failure("DB_READER_RESULT_INVALID")
            snapshot = result.snapshot_fingerprint
            for pending, match in zip(state.pending, result.matches, strict=True):
                if pending.kind == "unique" and match.conflicts:
                    state.out.add("DB_UNIQUE", pending.row, pending.fields)
                if pending.kind == "fk" and not match.exists:
                    state.out.add("DB_FOREIGN_KEY", pending.row, pending.fields)
        else:
            for pending in state.pending:
                state.out.add("DB_CONSTRAINT_UNVERIFIED", pending.row, pending.fields)
        return state.out.result(
            data, canonical_sha256_value(policy.canonical_json()), snapshot
        )


class _State:
    def __init__(
        self,
        data: ValidationDataset,
        catalog: DatabaseCatalog,
        policy: ConstraintValidationPolicy,
    ) -> None:
        self.data, self.catalog, self.policy = data, catalog, policy
        self.out = Collector(policy.limits)
        self.tables = {t.table_id: t for s in catalog.schemas for t in s.tables}
        self.operations = {t.table_id: t for t in policy.tables}
        self.allow = {(c.table_id, c.column_id) for c in policy.allow_columns}
        self.identities = {
            (c.table_id, c.column_id) for c in policy.source_identity_allow
        }
        self.rows: dict[str, list[ValidationRecord]] = {}
        self.values = {
            r.record_id: {c.field_id: c.value for c in r.values} for r in data.records
        }
        self.invalid: dict[str, set[str]] = {}
        self.pending: list[_Pending] = []
        if sum(len(c.rules.rules) for c in policy.checks) > policy.limits.max_rules:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        self.key_specs = {
            t.table_id: unique_keys(t, catalog.dialect) for t in self.tables.values()
        }
        self.incoming: dict[
            tuple[str, tuple[str, ...]], set[tuple[tuple[str, object], ...]]
        ] = {}
        if any(t.table_id not in self.tables for t in policy.tables) or any(
            tid not in self.tables
            or cid not in {c.column_id for c in self.tables[tid].columns}
            for tid, cid in self.allow
        ):
            raise failure("DB_POLICY_SCOPE_INVALID")
        for operation in policy.tables:
            if operation.operation == "upsert" and not any(
                k.supported and k.column_ids == operation.identity_column_ids
                for k in self.key_specs[operation.table_id]
            ):
                raise failure("DB_UPSERT_IDENTITY_INVALID")

    def local(self) -> None:
        for row in self.data.records:
            self.out.tick()
            table = self.tables.get(row.collection_id)
            if table is None or table.table_id not in self.operations:
                self.out.add("DB_TARGET_NOT_ALLOWED", row)
                continue
            if (
                not table.writable
                or table.inspection is None
                or table.inspection.kind != "table"
            ):
                self.out.add("DB_TABLE_NOT_WRITABLE", row)
                continue
            invalid = validate_fields(
                row, table.columns, self.catalog.dialect, self.out
            )
            for cell in row.values:
                if (table.table_id, cell.field_id) not in self.allow:
                    self.out.add("DB_COLUMN_NOT_ALLOWED", row, (cell.field_id,))
                    invalid.add(cell.field_id)
                column = next(
                    (c for c in table.columns if c.column_id == cell.field_id), None
                )
                if (
                    column is not None
                    and (
                        column.primary_key
                        or (
                            column.inspection is not None
                            and column.inspection.identity is not None
                        )
                    )
                    and (table.table_id, cell.field_id) not in self.identities
                ):
                    self.out.add(
                        "DB_SOURCE_IDENTITY_NOT_ALLOWED", row, (cell.field_id,)
                    )
                    invalid.add(cell.field_id)
            self.invalid[row.record_id] = invalid
            self.rows.setdefault(row.collection_id, []).append(row)

    async def checks(self) -> None:
        configured = {
            (c.table_id, c.column_id, c.expression_fingerprint): c
            for c in self.policy.checks
        }
        used: set[tuple[str, str | None, str]] = set()
        for tid, rows in self.rows.items():
            table = self.tables[tid]
            expressions: list[tuple[str | None, str]] = [
                (None, expression) for expression in table.check_constraints
            ]
            for column in table.columns:
                dt = column.inspection.data_type if column.inspection else None
                while dt is not None:
                    expressions.extend(
                        (column.column_id, expr) for expr in dt.domain_checks
                    )
                    dt = dt.base_type
            for cid, expression in expressions:
                binding_id = (tid, cid, canonical_sha256_value(expression))
                binding = configured.get(binding_id)
                if binding is None:
                    for row in rows:
                        self.out.add(
                            "DB_CONSTRAINT_UNVERIFIED", row, (cid,) if cid else ()
                        )
                    continue
                used.add(binding_id)
                self.check_binding(binding, table)
                # Верхняя оценка local rule work учитывает все bindings в одном
                # budget, включая field/schema checks и presence operands.
                self.out.tick(
                    len(rows)
                    * sum(
                        4
                        + (len(rule.field_ids) if isinstance(rule, PresenceRule) else 0)
                        for rule in binding.rules.rules
                    )
                    + 2 * len(binding.rules.rules)
                    + len(binding.rules.fields)
                )
                report = await BusinessRuleValidator(
                    limits=self.policy.limits
                ).validate(
                    ValidationDataset(
                        records=tuple(
                            r.model_copy(update={"parent_id": None}) for r in rows
                        )
                    ),
                    rules=binding.rules,
                )
                for issue in report.issues:
                    issue_row = next(
                        (r for r in rows if r.record_id == issue.record_id), None
                    )
                    self.out.add(
                        "DB_CHECK" if issue.code == "RULE_VIOLATION" else issue.code,
                        issue_row,
                        issue.field_ids,
                        issue.rule_id,
                        collection_id=tid,
                    )
            if table.inspection is not None and (
                table.inspection.partitioned
                or any(
                    not c.enforced or not c.validated
                    for c in table.inspection.constraints
                )
            ):
                for row in rows:
                    self.out.add("DB_CONSTRAINT_UNVERIFIED", row)
        if set(configured) - used:
            # Даже пустой scope не позволяет незаметно принять stale attestation.
            all_expressions: set[tuple[str, str | None, str]] = {
                (t.table_id, None, canonical_sha256_value(e))
                for t in self.tables.values()
                for e in t.check_constraints
            }
            for t in self.tables.values():
                for c in t.columns:
                    dt = c.inspection.data_type if c.inspection else None
                    while dt is not None:
                        all_expressions.update(
                            (t.table_id, c.column_id, canonical_sha256_value(e))
                            for e in dt.domain_checks
                        )
                        dt = dt.base_type
            if set(configured) - all_expressions:
                raise failure("DB_CHECK_BINDING_INVALID")

    def check_binding(self, binding: CheckRuleBinding, table: TableCatalog) -> None:
        columns = {c.column_id for c in table.columns}
        if any(
            r.collection_id != table.table_id
            or not isinstance(r, ComparisonRule | PresenceRule | RequiredIfRule)
            for r in binding.rules.rules
        ) or any(
            f.collection_id != table.table_id or f.field_id not in columns
            for f in binding.rules.fields
        ):
            raise failure("DB_CHECK_BINDING_INVALID")

    def key_values(
        self, row: ValidationRecord, columns: tuple[str, ...]
    ) -> tuple[NormalizedScalar, ...] | None:
        if any(c in self.invalid[row.record_id] for c in columns):
            return None
        values = self.values[row.record_id]
        if any(c not in values for c in columns):
            self.out.add("DB_CONSTRAINT_UNVERIFIED", row, columns)
            return None
        return tuple(values[c] for c in columns)

    def add_lookup(
        self,
        row: ValidationRecord,
        tid: str,
        columns: tuple[str, ...],
        values: tuple[NormalizedScalar, ...],
        kind: str,
        fields: tuple[str, ...],
        operation: ConstraintTablePolicy | None = None,
    ) -> None:
        if any((tid, cid) not in self.allow for cid in columns):
            self.out.add("DB_CONSTRAINT_UNVERIFIED", row, fields)
            return
        identity: tuple[NormalizedScalar, ...] = ()
        ids: tuple[str, ...] = ()
        if operation is not None and operation.operation == "upsert":
            candidate = self.key_values(row, operation.identity_column_ids)
            if candidate is None or any(isinstance(v, NullScalar) for v in candidate):
                self.out.add(
                    "DB_UPSERT_IDENTITY_INVALID", row, operation.identity_column_ids
                )
                return
            ids, identity = operation.identity_column_ids, candidate
        self.out.key()
        self.pending.append(
            _Pending(
                ConstraintLookup(
                    lookup_id=f"lookup-{len(self.pending)}",
                    table_id=tid,
                    column_ids=columns,
                    values=values,
                    identity_column_ids=ids,
                    identity_values=identity,
                ),
                row,
                fields,
                kind,
            )
        )

    def keys(self) -> None:
        for tid, rows in self.rows.items():
            for key in self.key_specs[tid]:
                self.unique(tid, rows, key)
        # Только после построения всех parent keys: порядок batches не влияет.
        for tid, rows in self.rows.items():
            for fk in self.tables[tid].foreign_keys:
                parent_keys = self.key_specs.get(fk.referenced_table_id, ())
                supported = any(
                    k.supported and k.column_ids == fk.referenced_column_ids
                    for k in parent_keys
                )
                info = fk.inspection
                match = info.match.upper() if info else "UNKNOWN"
                supported = (
                    supported
                    and info is not None
                    and not info.deferrable
                    and match in ("SIMPLE", "NONE", "FULL")
                )
                for row in rows:
                    self.out.tick()
                    if not supported:
                        self.out.add("DB_CONSTRAINT_UNVERIFIED", row, fk.column_ids)
                        continue
                    values = self.key_values(row, fk.column_ids)
                    if values is None:
                        continue
                    null_count = sum(isinstance(v, NullScalar) for v in values)
                    if null_count:
                        if match == "FULL" and null_count != len(values):
                            self.out.add("DB_FOREIGN_KEY", row, fk.column_ids)
                        continue
                    parent_columns = {
                        c.column_id: c
                        for c in self.tables[fk.referenced_table_id].columns
                    }
                    mismatch = False
                    for source_id, parent_id, value in zip(
                        fk.column_ids, fk.referenced_column_ids, values, strict=True
                    ):
                        codes = field_codes(
                            parent_columns[parent_id], value, self.catalog.dialect
                        )
                        for code in codes:
                            self.out.add(code, row, (source_id,))
                            mismatch = True
                    if mismatch:
                        continue
                    exact_key = tuple(scalar_key(v) for v in values)
                    if exact_key in self.incoming.get(
                        (fk.referenced_table_id, fk.referenced_column_ids), set()
                    ):
                        continue
                    self.add_lookup(
                        row,
                        fk.referenced_table_id,
                        fk.referenced_column_ids,
                        values,
                        "fk",
                        fk.column_ids,
                    )

    def unique(self, tid: str, rows: list[ValidationRecord], key: UniqueKey) -> None:
        seen: dict[tuple[tuple[str, object], ...], ValidationRecord] = {}
        reported: set[str] = set()
        for row in rows:
            self.out.tick()
            if not key.supported:
                self.out.add("DB_CONSTRAINT_UNVERIFIED", row, key.column_ids)
                continue
            values = self.key_values(row, key.column_ids)
            if values is None or (
                not key.nulls_equal and any(isinstance(v, NullScalar) for v in values)
            ):
                continue
            exact = tuple(scalar_key(v) for v in values)
            self.out.key()
            previous = seen.get(exact)
            if previous is not None:
                self.out.add("DB_UNIQUE", row, key.column_ids)
                if previous.record_id not in reported:
                    self.out.add("DB_UNIQUE", previous, key.column_ids)
                    reported.add(previous.record_id)
            else:
                seen[exact] = row
            self.add_lookup(
                row,
                tid,
                key.column_ids,
                values,
                "unique",
                key.column_ids,
                self.operations[tid],
            )
        self.incoming[(tid, key.column_ids)] = set(seen)
