"""Read-only SQLite inspection adapter; исполнение загрузки запрещено."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import AsyncIterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from structuraguard.contracts.database import (
    ColumnCatalog,
    ColumnInspectionMetadata,
    DatabaseCatalog,
    DatabaseInspectionRequest,
    DatabaseMetadataSnapshot,
    DatabaseType,
    ForeignKeyCatalog,
    ForeignKeyInspectionMetadata,
    IndexCatalog,
    IndexKeyCatalog,
    LoadContext,
    SchemaCatalog,
    TableCatalog,
    TableInspectionMetadata,
)
from structuraguard.contracts.mapping import ValidatedMappingPlan
from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.reports import LoadReport
from structuraguard.exceptions import (
    DatabaseInspectionError,
    OperationNotImplementedError,
)

from ._catalog import inspect_catalog
from ._inspection import InspectionControl, failure, inspection_slot, run_inspection
from ._sqlite_sql import (
    TableDefinition,
    index_definition,
    table_definition,
    tokenize,
    unsupported,
)
from .normalization import catalog_identifier, normalize_type
from .target import SQLiteTarget

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


class SQLiteDatabaseAdapter:
    """Получать каталог существующей SQLite-БД через отдельное соединение для чтения.

    Args:
        target: Доверенная конфигурация пути, allowlist/denylist и limits.
            При создании адаптера повторно проверяется и сохраняется локально.

    Raises:
        pydantic.ValidationError: Конфигурация target некорректна.

    Side effects:
        Конструктор не выполняет I/O. Соединение открывают только ``inspect``
        и ``inspect_metadata``; один экземпляр допускает один активный вызов.

    Security:
        Нет чтения пользовательских строк, выполнения metadata expressions
        или записи. Произвольные SQL/connection не принимаются. Каталог
        остаётся недоверенными данными и не подтверждает права writer.
    """

    def __init__(self, target: SQLiteTarget) -> None:
        self._target = SQLiteTarget.model_validate(target.model_dump())
        self._active = threading.Lock()
        self._unavailable = False

    async def inspect(self, request: DatabaseInspectionRequest) -> DatabaseCatalog:
        """Получить каталог с fingerprint и FK graph после закрытия соединения.

        Args:
            request: Target ID, ожидаемый policy fingerprint и сужение schemas.

        Returns:
            DatabaseCatalog schema 1.1.0, ``catalog-v1``; при циклах load order
            отсутствует. Fingerprint покрывает только представимые metadata.

        Raises:
            DatabaseInspectionError: Нарушение scope, missing/unsupported
                metadata, limits, timeout или ошибка соединения/cleanup.
                Частичный каталог не возвращается; причина задана error_code.
            asyncio.CancelledError: Отмена после ограниченного cleanup worker.

        Side effects:
            Открывает существующий файл в режиме read-only и читает metadata.
            Общий deadline учитывает reflection, cleanup, fingerprint и graph.

        Security:
            Allowlist/denylist проверяются до I/O. Нет DDL/DML, user rows или
            LLM calls. При неуспешном cleanup экземпляр блокирует новые вызовы.
        """
        with inspection_slot(self._active, unavailable=self._unavailable):
            try:
                return await inspect_catalog(
                    lambda: self._inspect_metadata(request), self._target.limits
                )
            except DatabaseInspectionError as error:
                if error.error_code == "DATABASE_INSPECTION_CLEANUP_FAILED":
                    self._unavailable = True
                raise

    async def inspect_metadata(
        self, request: DatabaseInspectionRequest
    ) -> DatabaseMetadataSnapshot:
        """Получить metadata snapshot без fingerprint и dependency graph.

        Args:
            request: Target/policy binding и необязательное сужение schemas.

        Returns:
            DatabaseMetadataSnapshot schema 1.1.0 с замкнутыми FK-ссылками;
            ``comments_supported=False``, ``write_permissions="unknown"``.

        Raises:
            DatabaseInspectionError: Ошибки policy, metadata, limits и cleanup,
                как у ``inspect``; неполный snapshot не публикуется.
            asyncio.CancelledError: Отмена после ограниченного cleanup worker.

        Side effects:
            Новое read-only соединение закрывается до возврата snapshot.
            Читаются только metadata; expressions и пользовательские строки
            не исполняются и не выбираются. SQL/path не добавляются к ошибке.
        """
        with inspection_slot(self._active, unavailable=self._unavailable):
            return await self._inspect_metadata(request)

    async def execute(
        self,
        batches: AsyncIterable[NormalizedBatch],
        plan: ValidatedMappingPlan,
        context: LoadContext,
    ) -> LoadReport:
        """Отклонить загрузку: SQLite inspection adapter не реализует writer.

        Args:
            batches: Поток для совместимости с port; не читается.
            plan: План загрузки; не исполняется.
            context: Контекст загрузки; не используется для открытия БД.

        Raises:
            OperationNotImplementedError: Всегда, ``SDK_OPERATION_NOT_IMPLEMENTED``.

        Side effects:
            Нет I/O, DDL/DML или потребления batches; LoadReport не возвращается.
        """
        raise OperationNotImplementedError("database.execute")

    async def _inspect_metadata(
        self, request: DatabaseInspectionRequest
    ) -> DatabaseMetadataSnapshot:
        """Вернуть полный metadata snapshot после cleanup read-only connection.

        Raises:
            DatabaseInspectionError: Нарушение policy, неполный scope, limits,
                неподдержанные metadata или ошибка reflection без raw SQL/DSN.
            asyncio.CancelledError: Отмена caller после остановки DB worker.

        Side effects:
            Открывает файл только для чтения, не выбирает пользовательские строки.
        """
        try:
            checked = DatabaseInspectionRequest.model_validate(request.model_dump())
        except ValidationError:
            checked = None
        if checked is None:
            raise failure("TARGET_NOT_ALLOWED")
        target = self._target
        if checked.target_id != target.target_id:
            raise failure("DATABASE_TARGET_MISMATCH")
        if checked.target_policy_fingerprint != target.policy_fingerprint:
            raise failure("DATABASE_POLICY_MISMATCH")
        if (
            target.include_schemas != ("main",)
            or any(_sqlite_name(name) == "main" for name in target.deny_schemas)
            or any(name != "main" for name in checked.schema_names)
        ):
            raise failure("TARGET_NOT_ALLOWED")
        if target.include_columns or target.deny_columns:
            raise unsupported()
        denied = {_sqlite_name(name) for name in target.deny_tables}
        names = tuple(
            sorted(
                name
                for name in target.include_tables
                if _sqlite_name(name) not in denied
            )
        )
        if not names or any(name.lower().startswith("sqlite_") for name in names):
            raise failure("TARGET_NOT_ALLOWED")
        if len(names) > target.limits.max_tables:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        try:
            return await run_inspection(
                lambda control: self._inspect_file(names, control), target.limits
            )
        except DatabaseInspectionError as error:
            if error.error_code == "DATABASE_INSPECTION_CLEANUP_FAILED":
                self._unavailable = True
            raise

    def _inspect_file(
        self, names: tuple[str, ...], control: InspectionControl
    ) -> DatabaseMetadataSnapshot:
        try:
            from sqlalchemy import create_engine
            from sqlalchemy.exc import SQLAlchemyError
            from sqlalchemy.pool import NullPool
        except ImportError:
            raise failure("DATABASE_DEPENDENCY_UNAVAILABLE") from None

        def connect() -> sqlite3.Connection:
            control.check()
            raw = sqlite3.connect(
                self._target.path.as_uri() + "?mode=ro",
                uri=True,
                isolation_level=None,
                timeout=min(
                    control.limits.lock_timeout_seconds, control.limits.timeout_seconds
                ),
            )
            try:
                raw.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, control.limits.max_sql_bytes)
                raw.setlimit(
                    sqlite3.SQLITE_LIMIT_SQL_LENGTH, control.limits.max_sql_bytes
                )
                raw.execute("PRAGMA query_only = ON")
                if raw.execute("PRAGMA query_only").fetchone() != (1,):
                    raise failure("DATABASE_READ_ONLY_REQUIRED")
                raw.set_progress_handler(lambda: int(control.expired()), 100)
                raw.set_authorizer(_authorize)
                control.attach(raw)
            except (sqlite3.Error, DatabaseInspectionError):
                raw.close()
                raise
            return raw

        engine = create_engine(
            "sqlite+pysqlite://",
            creator=connect,
            poolclass=NullPool,
            echo=False,
            hide_parameters=True,
        )
        # echo=False не перекрывает application logging DEBUG, включая result rows.
        # Изолированный logger принадлежит только этому engine, не registry logging.
        logger = logging.Logger(
            "structuraguard.sqlite.inspection", logging.CRITICAL + 1
        )
        logger.propagate = False
        engine.logger = logger
        engine.pool.logger = logger
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("BEGIN")
                reader = _SQLiteReader(connection, control)
                tables = tuple(reader.table(name) for name in names)
                tables = _resolve_foreign_keys(tables, reader.references)
                result = DatabaseMetadataSnapshot(
                    dialect="sqlite",
                    target_id=self._target.target_id,
                    target_policy_fingerprint=self._target.policy_fingerprint,
                    comments_supported=False,
                    schemas=(
                        SchemaCatalog(
                            schema_id=catalog_identifier("schema", "main"),
                            name="main",
                            tables=tables,
                        ),
                    ),
                )
                control.check()
                connection.rollback()
            control.check()
            return result
        except (SQLAlchemyError, sqlite3.Error, OSError, ValidationError, ValueError):
            code = (
                "PROCESSING_TIMEOUT"
                if control.expired()
                else "DATABASE_INSPECTION_FAILED"
            )
        finally:
            # Синхронное закрытие выполняется в том же worker, что и connection.
            control.close()
            engine.dispose()
        # from None скрывает traceback, но оставляет sensitive driver __context__.
        raise failure(code)


def _authorize(
    action: int,
    arg1: str | None,
    arg2: str | None,
    database: str | None,
    source: str | None,
) -> int:
    del source
    if action == sqlite3.SQLITE_READ:
        return (
            sqlite3.SQLITE_OK
            if database == "main" and arg1 in {"sqlite_master", "sqlite_schema"}
            else sqlite3.SQLITE_DENY
        )
    if action == sqlite3.SQLITE_PRAGMA:
        return (
            sqlite3.SQLITE_OK
            if arg1
            in {
                "read_uncommitted",
                "table_xinfo",
                "index_list",
                "index_xinfo",
                "foreign_key_list",
            }
            else sqlite3.SQLITE_DENY
        )
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_OK if arg2 in {"length"} else sqlite3.SQLITE_DENY
    if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_TRANSACTION}:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise unsupported()
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise unsupported()
    return value


class _SQLiteReader:
    def __init__(self, connection: Connection, control: InspectionControl) -> None:
        self.connection = connection
        self.control = control
        self.references: dict[str, tuple[_ForeignKey, ...]] = {}

    def rows(
        self, sql: str, parameters: tuple[object, ...] = (), *, limit: int
    ) -> tuple[tuple[object, ...], ...]:
        self.control.check()
        result: list[tuple[object, ...]] = []
        self.control.statement_deadline = min(
            self.control.deadline,
            time.monotonic() + self.control.limits.statement_timeout_seconds,
        )
        try:
            with self.connection.exec_driver_sql(sql, parameters) as cursor:
                for row in cursor:
                    values: tuple[object, ...] = tuple(row)
                    self.control.account(values)
                    if len(result) >= limit:
                        raise failure("SECURITY_LIMIT_EXCEEDED")
                    result.append(values)
            self.control.check()
        finally:
            if self.control.expired():
                self.control.cancelled.set()
            self.control.statement_deadline = None
        return tuple(result)

    def text(self, value: object) -> str:
        text = _text(value)
        if len(text) > self.control.limits.max_text_chars:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        return text

    def identifier(self, value: object) -> str:
        name = self.text(value)
        if name != name.strip():
            raise unsupported()
        return name

    def pragma(
        self, pragma: str, name: str, limit: int
    ) -> tuple[tuple[object, ...], ...]:
        identifier = self.connection.dialect.identifier_preparer.quote_identifier(name)
        return self.rows(f"PRAGMA main.{pragma}({identifier})", limit=limit)

    def definition(self, name: str, kind: str) -> str | None:
        records = self.rows(
            "SELECT length(CAST(sql AS BLOB)) FROM main.sqlite_schema WHERE name=? AND type=?",
            (name, kind),
            limit=1,
        )
        if not records:
            raise failure("DATABASE_OBJECT_NOT_FOUND")
        length = records[0][0]
        if length is None:
            return None
        if _integer(length) > self.control.limits.max_sql_bytes:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        rows = self.rows(
            "SELECT sql FROM main.sqlite_schema WHERE name=? AND type=?",
            (name, kind),
            limit=1,
        )
        return _text(rows[0][0])

    def table(self, name: str) -> TableCatalog:
        rows = self.rows(
            "SELECT type FROM main.sqlite_schema WHERE name=? AND type IN ('table','view')",
            (name,),
            limit=1,
        )
        if not rows:
            raise failure("DATABASE_OBJECT_NOT_FOUND")
        kind = _text(rows[0][0])
        sql = self.definition(name, kind)
        if sql is None:
            raise unsupported()
        if any(token.keyword("VIRTUAL") for token in tokenize(sql)[:3]):
            raise unsupported()
        raw_columns = self.pragma("table_xinfo", name, self.control.limits.max_columns)
        definition = (
            table_definition(sql, frozenset(_text(row[1]) for row in raw_columns))
            if kind == "table"
            else TableDefinition((), (), False, False, False)
        )
        if len(definition.checks) > self.control.limits.max_constraints:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        for check in definition.checks:
            self.text(check)
        indexes = self.indexes(name) if kind == "table" else ()
        pk_rows = sorted(
            (row for row in raw_columns if _integer(row[5])),
            key=lambda row: _integer(row[5]),
        )
        has_pk_index = any(index.origin == "primary_key" for index in indexes)
        rowid_alias = (
            len(pk_rows) == 1
            and _text(pk_rows[0][2]).upper() == "INTEGER"
            and not definition.without_rowid
            and not has_pk_index
        )
        unique_constraints = tuple(
            sorted(
                tuple(key.column_id for key in index.keys if key.column_id is not None)
                for index in indexes
                if index.origin == "unique_constraint"
            )
        )
        unique_columns = {
            index.keys[0].column_id
            for index in indexes
            if index.unique
            and index.origin != "primary_key"
            and index.predicate is None
            and len(index.keys) == 1
            and index.keys[0].column_id is not None
        }
        columns = tuple(
            sorted(
                (
                    self.column(
                        name, row, definition, rowid_alias, unique_columns, kind
                    )
                    for row in raw_columns
                ),
                key=lambda column: column.name,
            )
        )
        self.references[name] = self.foreign_keys(name) if kind == "table" else ()
        if (
            len(definition.checks) + len(indexes) + len(self.references[name])
            > self.control.limits.max_constraints
        ):
            raise failure("SECURITY_LIMIT_EXCEEDED")
        return TableCatalog(
            table_id=catalog_identifier("table", "main", name),
            schema_name="main",
            name=name,
            columns=columns,
            primary_key=tuple(
                catalog_identifier("column", "main", name, _text(row[1]))
                for row in pk_rows
            ),
            unique_constraints=unique_constraints,
            check_constraints=definition.checks,
            writable=kind == "table",
            inspection=TableInspectionMetadata(
                kind="view" if kind == "view" else "table",
                indexes=indexes,
                strict=definition.strict,
                without_rowid=definition.without_rowid,
            ),
        )

    def column(
        self,
        table: str,
        row: tuple[object, ...],
        definition: TableDefinition,
        rowid_alias: bool,
        unique_columns: set[str],
        kind: str,
    ) -> ColumnCatalog:
        name = self.identifier(row[1])
        primary = bool(_integer(row[5]))
        nullable = not bool(_integer(row[3]))
        if primary and (rowid_alias or definition.without_rowid or definition.strict):
            nullable = False
        if primary and nullable:
            raise unsupported()
        hidden = _integer(row[6])
        if hidden not in {0, 2, 3}:
            raise unsupported()
        generated = dict(definition.generated).get(name)
        if (hidden in {2, 3}) != (generated is not None):
            raise unsupported()
        if generated is not None:
            self.text(generated)
        data_type = normalize_type(self.text(row[2]), dialect="sqlite")
        if definition.strict and data_type.native_type.upper() == "ANY":
            data_type = DatabaseType.model_validate(
                {**data_type.model_dump(), "affinity": "none"}
            )
        column_id = catalog_identifier("column", "main", table, name)
        return ColumnCatalog(
            column_id=column_id,
            name=name,
            type_name=data_type.canonical_type,
            nullable=nullable,
            primary_key=primary,
            unique=column_id in unique_columns,
            generated=generated is not None,
            writable=generated is None and kind == "table",
            inspection=ColumnInspectionMetadata(
                data_type=data_type,
                ordinal_position=_integer(row[0]),
                default=None if row[4] is None else self.text(row[4]),
                generation_expression=generated,
                generation_storage=("stored" if hidden == 3 else "virtual")
                if generated is not None
                else None,
                autoincrement=primary and definition.autoincrement,
                rowid_alias=primary and rowid_alias,
            ),
        )

    def indexes(self, table: str) -> tuple[IndexCatalog, ...]:
        result: list[IndexCatalog] = []
        for row in self.pragma(
            "index_list", table, self.control.limits.max_constraints
        ):
            name = self.identifier(row[1])
            raw_keys = tuple(
                key
                for key in self.pragma(
                    "index_xinfo", name, self.control.limits.max_columns * 2 + 1
                )
                if _integer(key[5])
            )
            # WITHOUT ROWID PK имеет index_list entry без строки sqlite_schema.
            sql = self.definition(name, "index") if row[3] == "c" else None
            expressions, predicate = index_definition(sql) if sql else ((), None)
            for item in expressions:
                self.text(item)
            if predicate is not None:
                self.text(predicate)
            if sql and len(expressions) != len(raw_keys):
                raise unsupported()
            keys: list[IndexKeyCatalog] = []
            for offset, key in enumerate(raw_keys):
                column_name = key[2]
                if column_name is None and _integer(key[1]) != -2:
                    raise unsupported()
                keys.append(
                    IndexKeyCatalog(
                        column_id=catalog_identifier(
                            "column", "main", table, self.identifier(column_name)
                        )
                        if column_name is not None
                        else None,
                        expression=expressions[offset] if column_name is None else None,
                        descending=bool(_integer(key[3])),
                        collation=None if key[4] is None else self.identifier(key[4]),
                    )
                )
            origin = _text(row[3])
            if origin not in {"c", "u", "pk"}:
                raise unsupported()
            result.append(
                IndexCatalog(
                    index_id=catalog_identifier("index", "main", table, name),
                    name=name,
                    keys=tuple(keys),
                    unique=bool(_integer(row[2])),
                    predicate=predicate,
                    origin="index"
                    if origin == "c"
                    else "unique_constraint"
                    if origin == "u"
                    else "primary_key",
                )
            )
        return tuple(sorted(result, key=lambda index: index.name))

    def foreign_keys(self, table: str) -> tuple[_ForeignKey, ...]:
        groups: dict[int, list[tuple[object, ...]]] = {}
        for row in self.pragma(
            "foreign_key_list", table, self.control.limits.max_constraints
        ):
            groups.setdefault(_integer(row[0]), []).append(row)
        keys: list[_ForeignKey] = []
        for rows in groups.values():
            rows.sort(key=lambda row: _integer(row[1]))
            target = self.identifier(rows[0][2])
            local = tuple(self.identifier(row[3]) for row in rows)
            implicit = all(row[4] is None for row in rows)
            remote = (
                None if implicit else tuple(self.identifier(row[4]) for row in rows)
            )
            metadata = ForeignKeyInspectionMetadata.model_validate(
                {
                    "on_update": rows[0][5],
                    "on_delete": rows[0][6],
                    "match": rows[0][7],
                }
            )
            keys.append(_ForeignKey(target, local, remote, metadata))
        return tuple(keys)


@dataclass(frozen=True, slots=True)
class _ForeignKey:
    target_name: str
    column_names: tuple[str, ...]
    referenced_columns: tuple[str, ...] | None
    inspection: ForeignKeyInspectionMetadata


def _sqlite_name(name: str) -> str:
    # SQLite сравнивает identifiers без ASCII case, но не через Unicode casefold.
    return name.translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )


def _column_references(table: TableCatalog, names: tuple[str, ...]) -> tuple[str, ...]:
    columns = {_sqlite_name(column.name): column.column_id for column in table.columns}
    if any(_sqlite_name(name) not in columns for name in names):
        raise failure("DATABASE_CATALOG_SCOPE_INCOMPLETE")
    return tuple(columns[_sqlite_name(name)] for name in names)


def _resolve_foreign_keys(
    tables: Sequence[TableCatalog],
    references: dict[str, tuple[_ForeignKey, ...]],
) -> tuple[TableCatalog, ...]:
    table_by_name = {_sqlite_name(table.name): table for table in tables}
    resolved: list[TableCatalog] = []
    for table in tables:
        keys: list[ForeignKeyCatalog] = []
        for key in references[table.name]:
            target = table_by_name.get(_sqlite_name(key.target_name))
            if target is None:
                raise failure("DATABASE_CATALOG_SCOPE_INCOMPLETE")
            local = _column_references(table, key.column_names)
            remote = (
                target.primary_key
                if key.referenced_columns is None
                else _column_references(target, key.referenced_columns)
            )
            if len(remote) != len(local):
                raise unsupported()
            keys.append(
                ForeignKeyCatalog(
                    foreign_key_id=catalog_identifier(
                        "fk",
                        table.table_id,
                        target.table_id,
                        *local,
                        *remote,
                        key.inspection.canonical_json(),
                    ),
                    column_ids=local,
                    referenced_table_id=target.table_id,
                    referenced_column_ids=remote,
                    inspection=key.inspection,
                )
            )
        totals = Counter(key.foreign_key_id for key in keys)
        occurrences: Counter[str] = Counter()
        distinct_keys: list[ForeignKeyCatalog] = []
        for resolved_key in keys:
            identity = resolved_key.foreign_key_id
            if totals[identity] > 1:
                occurrences[identity] += 1
                resolved_key = resolved_key.model_copy(
                    update={
                        "foreign_key_id": catalog_identifier(
                            "fk",
                            identity,
                            str(occurrences[identity]),
                        )
                    }
                )
            distinct_keys.append(resolved_key)
        resolved.append(
            TableCatalog.model_validate(
                {
                    **table.model_dump(),
                    "foreign_keys": tuple(
                        sorted(distinct_keys, key=lambda key: key.foreign_key_id)
                    ),
                }
            )
        )
    return tuple(resolved)
