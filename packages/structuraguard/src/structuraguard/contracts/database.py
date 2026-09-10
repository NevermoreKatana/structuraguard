"""Декларативные контракты каталога БД и контекстов будущей загрузки.

Catalog DTO неизменяемы; некорректные поля и структурные ссылки дают
``pydantic.ValidationError``. Создание/валидация не выполняют I/O и SQL.
Metadata, включая comments и expressions, остаются недоверенными данными;
валидация DTO не подтверждает происхождение, DB grants или право загрузки.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

from ._base import FrozenContract
from .common import (
    ErrorPolicy,
    FingerprintStr,
    IdentifierStr,
    ProducerMetadata,
    SchemaVersionStr,
    UtcDateTime,
    _safe_text,
)

_ShortText = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=4096,
    ),
    AfterValidator(_safe_text),
]
_PositiveInt = Annotated[StrictInt, Field(gt=0)]
_NonnegativeInt = Annotated[StrictInt, Field(ge=0)]


def _safe_comment(value: str) -> str:
    """Сохранить formatting comments, проверив остальные controls и secrets."""
    _safe_text(value.replace("\n", " ").replace("\r", " ").replace("\t", " "))
    return value


_CommentText = Annotated[
    str, StringConstraints(strict=True, max_length=4096), AfterValidator(_safe_comment)
]


def _legacy_comment(values: object) -> object:
    """Legacy wire format сохраняет прежние strip/control ограничения comments."""
    if isinstance(values, dict) and values.get("inspection") is None:
        comment = values.get("comment")
        if isinstance(comment, str):
            comment = comment.strip()
            if not comment:
                raise ValueError("legacy catalog comment cannot be empty")
            _safe_text(comment)
            return {**values, "comment": comment}
    return values


_MetadataText = Annotated[
    str,
    StringConstraints(strict=True, max_length=4096),
    AfterValidator(_safe_text),
]


class DatabaseType(FrozenContract):
    """Представить объявленный тип без inference по пользовательским значениям.

    ``native_type`` сохраняет текст СУБД; ``canonical_type`` задаёт переносимую
    семью, включая ``unknown``. Length/precision/scale/timezone описывают известные
    параметры. ``affinity`` — отдельное правило SQLite, не проверка значений.
    ``type_kind`` различает builtin/enum/domain/array/unknown; enum_labels ordered,
    base_type и domain_* описывают domain, element_type — элементы array.
    ``domain_constraints`` дополняет domain_checks именами, comments и validity;
    None означает отсутствие расширенных metadata, пустой tuple — отсутствие CHECK.
    При наличии descriptors их выражения должны совпадать с domain_checks.
    Expressions не исполняются. Внешний consumer ограничивает размер/глубину DTO.
    """

    native_type: _MetadataText
    canonical_type: Literal[
        "integer",
        "decimal",
        "float",
        "text",
        "binary",
        "boolean",
        "date",
        "time",
        "datetime",
        "json",
        "uuid",
        "unknown",
    ]
    length: _PositiveInt | None = None
    precision: _PositiveInt | None = None
    scale: StrictInt | None = None
    timezone: StrictBool | None = None
    affinity: (
        Literal["integer", "decimal", "float", "text", "binary", "none"] | None
    ) = None
    type_kind: Literal["builtin", "enum", "domain", "array", "unknown"] | None = None
    enum_labels: tuple[_MetadataText, ...] = ()
    base_type: DatabaseType | None = None
    element_type: DatabaseType | None = None
    domain_checks: tuple[_MetadataText, ...] = ()
    domain_not_null: StrictBool = False
    domain_default: _MetadataText | None = None
    domain_constraints: tuple[ConstraintInspectionMetadata, ...] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def validate_domain_constraints(self) -> Self:
        """Проверить согласованность domain CHECK descriptors без I/O."""
        if self.domain_constraints is None:
            return self
        if self.type_kind != "domain" or any(
            constraint.kind != "check"
            or constraint.column_ids
            or constraint.expression is None
            for constraint in self.domain_constraints
        ):
            raise ValueError(
                "domain constraints require CHECK expressions without columns"
            )
        names = [constraint.name for constraint in self.domain_constraints]
        expressions = [
            constraint.expression
            for constraint in self.domain_constraints
            if constraint.expression is not None
        ]
        if len(names) != len(set(names)) or sorted(expressions) != sorted(
            self.domain_checks
        ):
            raise ValueError(
                "domain constraint descriptors disagree with domain checks"
            )
        return self


class ColumnInspectionMetadata(FrozenContract):
    """Хранить свойства столбца для catalog schema 1.1.0.

    ``data_type`` обязателен; ``ordinal_position`` — последовательная позиция
    видимой колонки с нуля, без пропусков от удалённых колонок. Default — исходное
    выражение, которое не вычисляется. Generation expression/storage задаются вместе;
    ``autoincrement`` требует ``rowid_alias``. ``identity`` различает always
    и by_default; writable eligibility проверяет содержащий ColumnCatalog.
    """

    data_type: DatabaseType
    ordinal_position: _NonnegativeInt
    default: _MetadataText | None = None
    generation_expression: _MetadataText | None = None
    generation_storage: Literal["stored", "virtual"] | None = None
    autoincrement: StrictBool = False
    rowid_alias: StrictBool = False
    identity: Literal["always", "by_default"] | None = None

    @model_validator(mode="after")
    def validate_generation(self) -> Self:
        if (self.generation_expression is None) != (self.generation_storage is None):
            raise ValueError("generation expression and storage must occur together")
        if self.autoincrement and not self.rowid_alias:
            raise ValueError("SQLite autoincrement requires a rowid alias")
        return self


class IndexKeyCatalog(FrozenContract):
    """Описать одну упорядоченную компоненту индекса.

    Требуется ровно одно из ``column_id`` и ``expression``. Остальные поля
    сохраняют descending/collation/null order/operator class. Expression
    остаётся текстом; consumer не должен превращать его в исполняемый SQL.
    """

    column_id: IdentifierStr | None = None
    expression: _MetadataText | None = None
    descending: StrictBool = False
    collation: IdentifierStr | None = None
    nulls_first: StrictBool | None = None
    operator_class: IdentifierStr | None = None

    @model_validator(mode="after")
    def validate_key(self) -> Self:
        if (self.column_id is None) == (self.expression is None):
            raise ValueError("index key requires exactly one column or expression")
        return self


class IndexCatalog(FrozenContract):
    """Представить обычный, partial или expression index без вывода natural key.

    ``index_id``/``name`` идентифицируют индекс; непустой ``keys`` сохраняет
    порядок компонентов. ``origin`` отличает отдельный индекс от PK/UNIQUE.
    Predicate, INCLUDE, method, nulls_not_distinct, valid и comment хранят
    доступные свойства. ``unique=True`` у partial/expression index само по себе
    не даёт безусловный ключ таблицы. Predicate/comment не исполняются.
    """

    index_id: IdentifierStr
    name: IdentifierStr
    keys: tuple[IndexKeyCatalog, ...]
    unique: StrictBool
    predicate: _MetadataText | None = None
    origin: Literal["index", "unique_constraint", "primary_key"]
    include_column_ids: tuple[IdentifierStr, ...] = ()
    method: IdentifierStr | None = None
    nulls_not_distinct: StrictBool = False
    valid: StrictBool = True
    comment: _CommentText | None = None

    @model_validator(mode="after")
    def validate_keys(self) -> Self:
        if not self.keys:
            raise ValueError("index must contain keys")
        return self


class ConstraintInspectionMetadata(FrozenContract):
    """Сохранить именованное ограничение PostgreSQL и доступные признаки СУБД.

    ``name``/``kind`` задают ограничение, ``column_ids`` — ordered колонки.
    Expression/comment — недоверенный текст; deferrable/initially_deferred,
    validated/enforced/no_inherit отражают metadata. DTO не меняет режим проверки
    constraints и не проверяет данные таблицы или достаточность прав writer.
    """

    name: IdentifierStr
    kind: Literal["primary_key", "foreign_key", "unique", "check", "not_null"]
    column_ids: tuple[IdentifierStr, ...] = ()
    expression: _MetadataText | None = None
    comment: _CommentText | None = None
    deferrable: StrictBool = False
    initially_deferred: StrictBool = False
    validated: StrictBool = True
    enforced: StrictBool = True
    no_inherit: StrictBool = False


class TableInspectionMetadata(FrozenContract):
    """Сохранить свойства объекта, отсутствующие в legacy catalog.

    ``kind`` различает table/view/materialized_view; ``indexes`` и
    ``constraints`` содержат metadata. ``partitioned`` — признак PG-объекта,
    strict/without_rowid — свойства SQLite. SQL definition view и partition
    bounds здесь не хранятся; DTO не разрешает исполнение view или DDL.
    """

    kind: Literal["table", "view", "materialized_view"] = "table"
    indexes: tuple[IndexCatalog, ...] = ()
    constraints: tuple[ConstraintInspectionMetadata, ...] = ()
    partitioned: StrictBool = False
    strict: StrictBool = False
    without_rowid: StrictBool = False


class ForeignKeyInspectionMetadata(FrozenContract):
    """Представить действия FK и deferred semantics из metadata СУБД.

    Обязательны ``on_update``, ``on_delete`` и ``match``. ``name`` необязателен;
    deferrable/initially_deferred не разрешают отключение FK. PG
    ``on_delete_column_ids`` сохраняет подмножество колонок SET NULL/DEFAULT;
    его ссылки проверяет содержащий TableCatalog. SQL не выполняется.
    """

    on_update: Literal["NO ACTION", "RESTRICT", "SET NULL", "SET DEFAULT", "CASCADE"]
    on_delete: Literal["NO ACTION", "RESTRICT", "SET NULL", "SET DEFAULT", "CASCADE"]
    match: IdentifierStr
    deferrable: StrictBool = False
    initially_deferred: StrictBool = False
    on_delete_column_ids: tuple[IdentifierStr, ...] = ()
    name: IdentifierStr | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ColumnCatalog(FrozenContract):
    """Описать столбец, его ограничения и структурную пригодность к записи.

    ``column_id``/``name`` идентифицируют столбец; в schema 1.1.0 ``type_name``
    согласован с canonical type внутри ``inspection``. PK не может быть nullable;
    generated и identity ALWAYS требуют ``writable=False``. Это не проверка grants.
    При inspection=None сохраняется legacy wire format; comments с inspection
    сохраняют форматирование и могут содержать sensitive metadata.
    """

    column_id: IdentifierStr
    name: IdentifierStr
    type_name: IdentifierStr
    nullable: StrictBool
    primary_key: StrictBool = False
    unique: StrictBool = False
    generated: StrictBool = False
    writable: StrictBool = True
    comment: _CommentText | None = None
    inspection: ColumnInspectionMetadata | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="before")
    @classmethod
    def preserve_legacy_comment(cls, value: object) -> object:
        return _legacy_comment(value)

    @model_validator(mode="after")
    def validate_column_flags(self) -> Self:
        if self.primary_key and self.nullable:
            raise ValueError("primary key column cannot be nullable")
        if self.generated and self.writable:
            raise ValueError("generated column cannot be marked writable")
        if self.inspection is not None:
            if self.inspection.identity == "always" and self.writable:
                raise ValueError("identity always column cannot be writable")
            if self.type_name != self.inspection.data_type.canonical_type:
                raise ValueError("column canonical types disagree")
            if self.generated != (self.inspection.generation_expression is not None):
                raise ValueError("column generation metadata disagree")
        return self


class ForeignKeyCatalog(FrozenContract):
    """Проверяемая ссылка внешнего ключа на catalog identifiers."""

    foreign_key_id: IdentifierStr
    column_ids: tuple[IdentifierStr, ...]
    referenced_table_id: IdentifierStr
    referenced_column_ids: tuple[IdentifierStr, ...]
    inspection: ForeignKeyInspectionMetadata | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def validate_column_pairs(self) -> Self:
        if not self.column_ids:
            raise ValueError("foreign key must contain at least one column")
        if len(self.column_ids) != len(self.referenced_column_ids):
            raise ValueError("foreign key column counts must match")
        if len(set(self.column_ids)) != len(self.column_ids):
            raise ValueError("foreign key contains duplicate local columns")
        if len(set(self.referenced_column_ids)) != len(self.referenced_column_ids):
            raise ValueError("foreign key contains duplicate referenced columns")
        if self.inspection is not None and not set(
            self.inspection.on_delete_column_ids
        ) <= set(self.column_ids):
            raise ValueError("foreign key delete action references an unknown column")
        return self


class TableCatalog(FrozenContract):
    """Представить именованный объект с колонками, PK/FK/unique/checks.

    ``columns`` непустой, IDs/names уникальны; ссылки ограничений должны быть
    согласованы с колонками. ``inspection`` дополняет legacy поля schema 1.1.0.
    View/materialized view и их колонки не writable. Checks/comments остаются
    недоверенным текстом; ``writable`` не подтверждает права или готовность load.
    """

    table_id: IdentifierStr
    schema_name: IdentifierStr
    name: IdentifierStr
    columns: tuple[ColumnCatalog, ...]
    primary_key: tuple[IdentifierStr, ...] = ()
    foreign_keys: tuple[ForeignKeyCatalog, ...] = ()
    unique_constraints: tuple[tuple[IdentifierStr, ...], ...] = ()
    check_constraints: tuple[_ShortText, ...] = ()
    writable: StrictBool = True
    comment: _CommentText | None = None
    inspection: TableInspectionMetadata | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="before")
    @classmethod
    def preserve_legacy_comment(cls, value: object) -> object:
        return _legacy_comment(value)

    @model_validator(mode="after")
    def validate_table_references(self) -> Self:
        if not self.columns:
            raise ValueError("table catalog must contain at least one column")

        column_ids = tuple(column.column_id for column in self.columns)
        column_names = tuple(column.name for column in self.columns)
        if len(set(column_ids)) != len(column_ids):
            raise ValueError("table catalog contains duplicate column identifiers")
        if len(set(column_names)) != len(column_names):
            raise ValueError("table catalog contains duplicate column names")

        known_columns = set(column_ids)
        if self.inspection is not None:
            if self.inspection.kind in {"view", "materialized_view"} and (
                self.writable or any(column.writable for column in self.columns)
            ):
                raise ValueError("view cannot be writable")
            indexes = self.inspection.indexes
            if len({item.index_id for item in indexes}) != len(indexes):
                raise ValueError("duplicate index identifiers")
            for index in indexes:
                if not set(index.include_column_ids) <= known_columns:
                    raise ValueError("index includes an unknown column")
                if any(
                    key.column_id is not None and key.column_id not in known_columns
                    for key in index.keys
                ):
                    raise ValueError("index references an unknown column")
            if any(
                not set(item.column_ids) <= known_columns
                for item in self.inspection.constraints
            ):
                raise ValueError("constraint references an unknown column")
        if not set(self.primary_key) <= known_columns:
            raise ValueError("primary key references an unknown column")
        if len(set(self.primary_key)) != len(self.primary_key):
            raise ValueError("primary key contains duplicate columns")
        flagged_primary_key = {
            column.column_id for column in self.columns if column.primary_key
        }
        if flagged_primary_key != set(self.primary_key):
            raise ValueError("column and table primary key declarations disagree")

        foreign_key_ids = tuple(item.foreign_key_id for item in self.foreign_keys)
        if len(set(foreign_key_ids)) != len(foreign_key_ids):
            raise ValueError("table catalog contains duplicate foreign key identifiers")
        for foreign_key in self.foreign_keys:
            if not set(foreign_key.column_ids) <= known_columns:
                raise ValueError("foreign key references an unknown local column")

        seen_unique: set[tuple[str, ...]] = set()
        for constraint in self.unique_constraints:
            if not constraint:
                raise ValueError("unique constraint cannot be empty")
            if len(set(constraint)) != len(constraint):
                raise ValueError("unique constraint contains duplicate columns")
            if not set(constraint) <= known_columns:
                raise ValueError("unique constraint references an unknown column")
            if constraint in seen_unique:
                raise ValueError("table catalog contains duplicate unique constraints")
            seen_unique.add(constraint)
        return self


class SchemaCatalog(FrozenContract):
    """Схема БД с уникальными stable table identifiers."""

    schema_id: IdentifierStr
    name: IdentifierStr
    tables: tuple[TableCatalog, ...] = ()
    comment: _CommentText | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def validate_tables(self) -> Self:
        table_ids = tuple(table.table_id for table in self.tables)
        table_names = tuple(table.name for table in self.tables)
        if len(set(table_ids)) != len(table_ids):
            raise ValueError("schema catalog contains duplicate table identifiers")
        if len(set(table_names)) != len(table_names):
            raise ValueError("schema catalog contains duplicate table names")
        if any(table.schema_name != self.name for table in self.tables):
            raise ValueError("table schema name does not match its parent schema")
        return self


def _validate_catalog_schemas(schemas: tuple[SchemaCatalog, ...]) -> None:
    schema_ids = tuple(schema.schema_id for schema in schemas)
    schema_names = tuple(schema.name for schema in schemas)
    if len(set(schema_ids)) != len(schema_ids):
        raise ValueError("database catalog contains duplicate schema identifiers")
    if len(set(schema_names)) != len(schema_names):
        raise ValueError("database catalog contains duplicate schema names")
    tables = tuple(table for schema in schemas for table in schema.tables)
    table_by_id = {table.table_id: table for table in tables}
    if len(table_by_id) != len(tables):
        raise ValueError("database catalog contains duplicate table identifiers")
    for table in tables:
        for foreign_key in table.foreign_keys:
            referenced_table = table_by_id.get(foreign_key.referenced_table_id)
            if referenced_table is None:
                raise ValueError("foreign key references an unknown table")
            referenced_columns = {
                column.column_id for column in referenced_table.columns
            }
            if not set(foreign_key.referenced_column_ids) <= referenced_columns:
                raise ValueError("foreign key references an unknown target column")


class DatabaseMetadataSnapshot(FrozenContract):
    """Сохранить metadata schema 1.1.0 без вычисленного fingerprint и graph.

    Attributes:
        dialect: Диалект источника; concrete adapters возвращают sqlite/postgresql.
        target_id: Непрозрачный ID trusted binding, без connection credentials.
        target_policy_fingerprint: Отпечаток применённой policy.
        schemas: Каталог с уникальными IDs и замкнутыми FK-ссылками;
            у каждой таблицы/колонки обязательны inspection metadata.
        comments_supported: Возможность отражения comments в данном adapter.
        write_permissions: Всегда ``unknown``; grants не проверяются.

    DTO сам не открывает БД и не подтверждает происхождение данных. Полный
    DatabaseCatalog создаёт ``adapter.inspect`` после reflection и cleanup.
    """

    schema_version: Literal["1.1.0"] = "1.1.0"
    dialect: IdentifierStr
    target_id: IdentifierStr
    target_policy_fingerprint: FingerprintStr
    schemas: tuple[SchemaCatalog, ...]
    comments_supported: StrictBool
    write_permissions: Literal["unknown"] = "unknown"

    @model_validator(mode="after")
    def validate_metadata(self) -> Self:
        _validate_catalog_schemas(self.schemas)
        for schema in self.schemas:
            for table in schema.tables:
                if table.inspection is None:
                    raise ValueError("metadata snapshot requires table inspection")
                if any(column.inspection is None for column in table.columns):
                    raise ValueError("metadata snapshot requires column inspection")
        return self


class ForeignKeyDependency(FrozenContract):
    """Связать parent/child table IDs с evidence конкретного FK.

    ``foreign_key_id`` идентифицирует связь; parent_column_ids и child_column_ids
    непусты, равной длины и сохраняют попарный порядок composite key. Ребро
    описывает структуру, не команду соединения данных или выполнения SQL.
    """

    parent_table_id: IdentifierStr
    child_table_id: IdentifierStr
    foreign_key_id: IdentifierStr
    parent_column_ids: tuple[IdentifierStr, ...]
    child_column_ids: tuple[IdentifierStr, ...]

    @model_validator(mode="after")
    def validate_pairs(self) -> Self:
        if not self.parent_column_ids or len(self.parent_column_ids) != len(
            self.child_column_ids
        ):
            raise ValueError("dependency requires matching nonempty column pairs")
        return self


class DatabaseDependencyCycle(FrozenContract):
    """Представить компоненту сильной связности, включая self-reference.

    ``table_ids`` — уникальные участники; ``foreign_keys`` — внутренние рёбра.
    Проверяются охват участников и strong connectivity; полноту SCC относительно
    всего графа проверяет DatabaseDependencyGraph. Простые циклы не перечисляются,
    стратегия разрешения цикла не выбирается и constraints не отключаются.
    """

    table_ids: tuple[IdentifierStr, ...]
    foreign_keys: tuple[ForeignKeyDependency, ...]

    @model_validator(mode="after")
    def validate_component(self) -> Self:
        members = set(self.table_ids)
        if not members or len(members) != len(self.table_ids) or not self.foreign_keys:
            raise ValueError("cycle requires unique members and FK evidence")
        if {edge.parent_table_id for edge in self.foreign_keys} != members or {
            edge.child_table_id for edge in self.foreign_keys
        } != members:
            raise ValueError("cycle evidence must cover only component members")
        if len(set(self.foreign_keys)) != len(self.foreign_keys):
            raise ValueError("duplicate cycle evidence")
        forward: dict[str, set[str]] = {node: set() for node in members}
        reverse: dict[str, set[str]] = {node: set() for node in members}
        for edge in self.foreign_keys:
            forward[edge.parent_table_id].add(edge.child_table_id)
            reverse[edge.child_table_id].add(edge.parent_table_id)
        for adjacency in (forward, reverse):
            visited = {self.table_ids[0]}
            pending = [self.table_ids[0]]
            while pending:
                for node in adjacency[pending.pop()]:
                    if node not in visited:
                        visited.add(node)
                        pending.append(node)
            if visited != members:
                raise ValueError("cycle evidence is not strongly connected")
        return self


class JoinTableCandidate(FrozenContract):
    """Сохранить структурное предположение о связующей таблице.

    ``table_id`` и два ``foreign_key_ids`` связывают hint с graph evidence;
    ``key_column_ids``/``key_kind`` указывают PK либо unique constraint.
    ``build_dependency_graph`` выдаёт hint только при полном structural evidence.
    Сам DTO не определяет бизнес-сущность и не разрешает mapping/load.
    """

    table_id: IdentifierStr
    foreign_key_ids: tuple[IdentifierStr, IdentifierStr]
    key_column_ids: tuple[IdentifierStr, ...]
    key_kind: Literal["primary_key", "unique_constraint"]
    evidence: Literal["TWO_DISJOINT_FOREIGN_KEYS_COVER_ALL_COLUMNS_AND_UNIQUE_KEY"] = (
        "TWO_DISJOINT_FOREIGN_KEYS_COVER_ALL_COLUMNS_AND_UNIQUE_KEY"
    )


class DatabaseDependencyGraph(FrozenContract):
    """Сохранить FK graph с детерминированным порядком либо diagnostics циклов.

    Attributes:
        table_ids: Все узлы, включая isolated tables и views.
        edges: Parent→child рёбра с ordered FK pairs.
        topological_order: Все узлы DAG; при циклах None.
        load_order: Только структурно writable таблицы DAG; при циклах None.
        cycles: Полные SCC с внутренними FK.
        self_references: Все self-FK, также представленные в cycles.
        join_table_candidates: Hints со структурным evidence.
        requires_explicit_strategy: True при любом цикле.

    Проверяется согласованность orders/cycles/evidence. Граф не подтверждает
    grants, бизнес-смысл или возможность безопасной загрузки; I/O отсутствует.
    """

    table_ids: tuple[IdentifierStr, ...]
    edges: tuple[ForeignKeyDependency, ...]
    topological_order: tuple[IdentifierStr, ...] | None
    load_order: tuple[IdentifierStr, ...] | None
    cycles: tuple[DatabaseDependencyCycle, ...] = ()
    self_references: tuple[ForeignKeyDependency, ...] = ()
    join_table_candidates: tuple[JoinTableCandidate, ...] = ()
    requires_explicit_strategy: StrictBool = False

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        nodes = set(self.table_ids)
        if len(nodes) != len(self.table_ids):
            raise ValueError("duplicate graph nodes")
        edge_keys = {(edge.child_table_id, edge.foreign_key_id) for edge in self.edges}
        if len(edge_keys) != len(self.edges):
            raise ValueError("duplicate graph edges")
        if any(
            edge.parent_table_id not in nodes or edge.child_table_id not in nodes
            for edge in self.edges
        ):
            raise ValueError("graph edge references unknown node")
        if set(self.self_references) != {
            edge for edge in self.edges if edge.parent_table_id == edge.child_table_id
        }:
            raise ValueError("self-reference evidence disagrees with edges")
        if self.requires_explicit_strategy != bool(self.cycles):
            raise ValueError("cycle strategy flag disagrees with evidence")
        if self.cycles:
            if self.topological_order is not None or self.load_order is not None:
                raise ValueError("cyclic graph cannot publish an executable order")
            seen: set[str] = set()
            known_edges = set(self.edges)
            for component in self.cycles:
                members = set(component.table_ids)
                if (
                    not members <= nodes
                    or members & seen
                    or not set(component.foreign_keys) <= known_edges
                ):
                    raise ValueError("invalid component evidence")
                seen.update(members)
            _validate_cycle_coverage(self.table_ids, self.edges, self.cycles)
        else:
            if self.topological_order is None or self.load_order is None:
                raise ValueError("acyclic graph requires deterministic orders")
            order = {
                node: position for position, node in enumerate(self.topological_order)
            }
            if set(order) != nodes or len(order) != len(self.topological_order):
                raise ValueError("topological order must cover all graph nodes once")
            if any(
                order[e.parent_table_id] >= order[e.child_table_id] for e in self.edges
            ):
                raise ValueError("topological order violates a foreign key")
            if (
                len(set(self.load_order)) != len(self.load_order)
                or not set(self.load_order) <= nodes
            ):
                raise ValueError("load order references duplicate or unknown nodes")
            if tuple(sorted(self.load_order, key=order.__getitem__)) != self.load_order:
                raise ValueError("load order disagrees with topological order")
        if any(
            candidate.table_id not in nodes for candidate in self.join_table_candidates
        ):
            raise ValueError("join-table candidate references unknown node")
        return self


def _validate_cycle_coverage(
    nodes: tuple[str, ...],
    edges: tuple[ForeignKeyDependency, ...],
    cycles: tuple[DatabaseDependencyCycle, ...],
) -> None:
    """Проверить полноту SCC evidence через ацикличность condensed graph."""
    group = {node: node for node in nodes}
    evidence: set[ForeignKeyDependency] = set()
    for component in cycles:
        for node in component.table_ids:
            group[node] = component.table_ids[0]
        evidence.update(component.foreign_keys)
    children: dict[str, set[str]] = {
        representative: set() for representative in group.values()
    }
    indegrees = dict.fromkeys(children, 0)
    for edge in edges:
        parent, child = group[edge.parent_table_id], group[edge.child_table_id]
        if parent == child:
            if edge not in evidence:
                raise ValueError("cycle evidence omits an internal foreign key")
        elif child not in children[parent]:
            children[parent].add(child)
            indegrees[child] += 1
    pending = [node for node, count in indegrees.items() if not count]
    visited = 0
    while pending:
        node = pending.pop()
        visited += 1
        for child in children[node]:
            indegrees[child] -= 1
            if not indegrees[child]:
                pending.append(child)
    if visited != len(children):
        raise ValueError("cycle evidence omits a strongly connected component")


class DatabaseCatalog(FrozenContract):
    """Представить каталог с заявленным schema fingerprint без credentials.

    Attributes:
        schema_version: Legacy 1.0.0 либо 1.1.0 для M7 catalog-v1.
        database_fingerprint: Сохранённый fingerprint; DTO его не пересчитывает.
        target_id: Непрозрачный trusted binding, отдельный от fingerprint схемы.
        target_policy_fingerprint: Отпечаток policy, отдельный от схемы БД.
        producer: Источник результата; не участвует в schema fingerprint.
        schemas: Замкнутый по FK каталог schemas/tables/columns.
        fingerprint_version: ``catalog-v1`` требует schema 1.1.0 и metadata.
        comments_supported: Обязательная capability для catalog-v1.
        dependency_graph: Согласованный с FK graph, обязателен для catalog-v1.

    При отсутствии расширений legacy JSON сохраняется. DTO не открывает БД,
    не удостоверяет автора и не проверяет drift: для этого нужны новый inspection
    и ``verify_database_fingerprint``. Comments не являются доверенными инструкциями.
    """

    schema_version: SchemaVersionStr = "1.0.0"
    dialect: IdentifierStr
    target_id: IdentifierStr
    database_fingerprint: FingerprintStr
    target_policy_fingerprint: FingerprintStr
    producer: ProducerMetadata
    schemas: tuple[SchemaCatalog, ...] = ()
    database_name: IdentifierStr | None = None
    fingerprint_version: Literal["catalog-v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    comments_supported: StrictBool | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    dependency_graph: DatabaseDependencyGraph | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def validate_catalog_graph(self) -> Self:
        _validate_catalog_schemas(self.schemas)
        if self.fingerprint_version is not None:
            if (
                self.schema_version != "1.1.0"
                or self.comments_supported is None
                or self.dependency_graph is None
            ):
                raise ValueError(
                    "catalog-v1 requires versioned metadata and dependency graph"
                )
            DatabaseMetadataSnapshot(
                dialect=self.dialect,
                target_id=self.target_id,
                target_policy_fingerprint=self.target_policy_fingerprint,
                schemas=self.schemas,
                comments_supported=self.comments_supported,
            )
            tables = tuple(table for schema in self.schemas for table in schema.tables)
            graph = self.dependency_graph
            if set(graph.table_ids) != {table.table_id for table in tables}:
                raise ValueError("dependency graph nodes disagree with catalog")
            expected = {
                ForeignKeyDependency(
                    parent_table_id=key.referenced_table_id,
                    child_table_id=table.table_id,
                    foreign_key_id=key.foreign_key_id,
                    parent_column_ids=key.referenced_column_ids,
                    child_column_ids=key.column_ids,
                )
                for table in tables
                for key in table.foreign_keys
            }
            if set(graph.edges) != expected:
                raise ValueError("dependency graph edges disagree with catalog")
            if graph.load_order is not None and set(graph.load_order) != {
                table.table_id for table in tables if table.writable
            }:
                raise ValueError("load order disagrees with writable catalog objects")
            return self
        if self.dependency_graph is not None or self.comments_supported is not None:
            raise ValueError("extended catalog requires fingerprint version")
        if any(schema.comment is not None for schema in self.schemas) or any(
            table.inspection is not None
            or any(column.inspection is not None for column in table.columns)
            or any(key.inspection is not None for key in table.foreign_keys)
            for schema in self.schemas
            for table in schema.tables
        ):
            raise ValueError("inspection metadata requires DatabaseMetadataSnapshot")
        return self

    def has_column(self, table_id: str, column_id: str) -> bool:
        """Проверить catalog reference без обращения к БД.

        Args:
            table_id: Стабильный идентификатор таблицы.
            column_id: Стабильный идентификатор столбца этой таблицы.

        Returns:
            ``True``, если каталог содержит указанный столбец.
        """

        return any(
            table.table_id == table_id
            and any(column.column_id == column_id for column in table.columns)
            for schema in self.schemas
            for table in schema.tables
        )


class CatalogColumnRef(FrozenContract):
    """Стабильная ссылка на столбец конкретной таблицы каталога."""

    table_id: IdentifierStr
    column_id: IdentifierStr


class DatabaseInspectionRequest(FrozenContract):
    """Запросить metadata по target/policy binding без DSN, path или SQL.

    ``target_id`` и ``target_policy_fingerprint`` должны совпадать с trusted
    adapter target. ``read_only`` допускает только True. Уникальные schema_names
    могут лишь сузить allowlist; пустой tuple означает разрешённый scope target.
    Права и фактический scope проверяет adapter до I/O, создание DTO БД не открывает.
    """

    target_id: IdentifierStr
    target_policy_fingerprint: FingerprintStr
    read_only: Literal[True] = True
    schema_names: tuple[IdentifierStr, ...] = ()

    @model_validator(mode="after")
    def validate_schema_names(self) -> Self:
        if len(set(self.schema_names)) != len(self.schema_names):
            raise ValueError("inspection request contains duplicate schema names")
        return self


class MappingPolicyRef(FrozenContract):
    """Ссылка на versioned policy, применённую к DB mapping."""

    policy_id: IdentifierStr
    policy_fingerprint: FingerprintStr


class LoadPolicy(FrozenContract):
    """Явные safety guarantees, обязательные для исполнения MappingPlan."""

    dry_run: StrictBool
    error_policy: ErrorPolicy
    target_policy_fingerprint: FingerprintStr
    allowlist_ref: IdentifierStr
    denylist_ref: IdentifierStr | None
    staging_required: StrictBool
    transactional_audit_required: StrictBool
    rollback_required: StrictBool

    @model_validator(mode="after")
    def validate_write_safety(self) -> Self:
        if self.dry_run:
            return self
        if not self.staging_required:
            raise ValueError("non-dry-run load requires staging")
        if not self.transactional_audit_required:
            raise ValueError("non-dry-run load requires transactional audit")
        if not self.rollback_required:
            raise ValueError("non-dry-run load requires rollback")
        return self


class LoadContext(FrozenContract):
    """Fingerprint-bound execution context без connection handle."""

    run_id: IdentifierStr
    target_id: IdentifierStr
    database_fingerprint: FingerprintStr
    policy: LoadPolicy
    idempotency_key: IdentifierStr
    deadline: UtcDateTime | None
    cancellation_token_id: IdentifierStr
    staging_capability_evidence: IdentifierStr
    audit_capability_evidence: IdentifierStr


class StagingContext(FrozenContract):
    """Контекст staging, связанный с конкретным normalized dataset и target."""

    run_id: IdentifierStr
    staging_id: IdentifierStr
    target_id: IdentifierStr
    database_fingerprint: FingerprintStr
    target_policy_fingerprint: FingerprintStr
    normalized_fingerprint: FingerprintStr
    expires_at: UtcDateTime
    max_records: _PositiveInt
