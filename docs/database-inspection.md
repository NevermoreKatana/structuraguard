# Database Inspector — M7

Получите metadata существующей БД, SHA-256 схемы и порядок зависимостей по FK.
Ниже показаны отдельный metadata snapshot и [полный каталог](#fingerprint-dependency-graph-m7-c).
Для inspection существующего SQLite-файла установите extra
`structuraguard[sqlite]`. Укажите точные имена разрешённых объектов и доверенный
target ID. Конструктор не открывает БД; I/O начинается при `inspect` или `inspect_metadata`.

<!-- example:m07-inspection:start -->
```python
from pathlib import Path

from structuraguard.contracts import DatabaseInspectionRequest, DatabaseMetadataSnapshot
from structuraguard.database import SQLiteDatabaseAdapter, SQLiteTarget


async def inspect_schema(database_path: Path) -> DatabaseMetadataSnapshot:
    target = SQLiteTarget(
        path=database_path,
        target_id="application",
        include_tables=("customers", "orders", "private_notes"),
        deny_tables=("private_notes",),
    )
    request = DatabaseInspectionRequest(
        target_id=target.target_id,
        target_policy_fingerprint=target.policy_fingerprint,
    )
    return await SQLiteDatabaseAdapter(target).inspect_metadata(request)
```
<!-- example:m07-inspection:end -->

`database_path` должен быть абсолютным и указывать на существующую БД. Политика
принадлежит composition owner; request не содержит DSN, file path или SQL.
Пустая allowlist запрещает inspection, пустой schema selector означает trusted
scope `main`. Deny применяется до reflection и учитывает ASCII case equivalence
SQLite. Allowlist содержит точные имена из `sqlite_schema`.

## Результат

`DatabaseMetadataSnapshot` schema `1.1.0` содержит sorted schemas/tables/columns,
target ID и policy fingerprint. Новый optional `inspection` атрибут существующих
catalog DTO хранит дополнительные metadata:

| Объект | Данные |
| --- | --- |
| Column | `DatabaseType` с native/canonical type, length/precision/scale/timezone и SQLite affinity; ordinal position, default, generated expression/storage, rowid alias и explicit autoincrement. |
| Table | Object kind, indexes, SQLite STRICT/WITHOUT ROWID. PK/FK/unique/checks находятся в прежних полях `TableCatalog`. |
| FK | Ordered local/remote column pairs и фактические on-update/on-delete/match metadata. Implicit reference разрешается в объявленный PK внутри scope. |
| Index | Ordered column/expression keys, collation/direction, uniqueness, partial predicate, origin (`index`, `unique_constraint`, `primary_key`). |

Порядок объектов стабилен по именам; ordinal position сохраняется отдельно.
Composite key order не сортируется. Quoted names сохраняют case и разделители;
references разрешаются по правилам SQLite в реальные catalog IDs.
Index expression хранит исходный index term, включая записанные modifiers.
Partial/expression index не становится unique constraint или natural key.

SQLite clauses `DEFERRABLE` (включая `NOT DEFERRABLE`), `ON CONFLICT` и
column `COLLATE` дают `DATABASE_METADATA_UNSUPPORTED`: PRAGMA не возвращает
все эти свойства, а их потеря скрывала бы schema drift. Слова внутри quoted
identifiers, literals и comments не считаются clauses. `COLLATE` внутри
CHECK/generated/index expressions сохраняется вместе с выражением.

Generated columns и views помечены `writable=False`. Для остальных колонок
`writable` отражает structural eligibility, не grants writer и не разрешение
загрузки. `write_permissions="unknown"`. SQLite comments не поддержаны явно:
`comments_supported=False`, comments в catalog равны `None`.

`ANY` в STRICT table имеет `affinity="none"`; в обычной таблице для этого
объявления действует numeric affinity. Это отдельное SQLite правило, а не
вывод о значениях строк. [SQLite STRICT tables](https://sqlite.org/stricttables.html).

Совместимый API `inspect_metadata` возвращает snapshot без fingerprint. Для
полного каталога используйте `inspect`: он возвращает `DatabaseCatalog` schema
`1.1.0` с fingerprint и dependency graph. SDK facade пока не подключена к adapters;
`execute` возвращает `SDK_OPERATION_NOT_IMPLEMENTED` до I/O и чтения batches.
SDK orchestration, mapping, sampling значений, staging, dry-run и загрузка
не реализованы этим milestone. Канонические требования:
[ТЗ §9 — анализ целевой БД][spec-database] и [M7 — Database Inspector][spec-m7].

## Read-only и limits

Соединение открывается через `mode=ro`, использует `query_only`, read transaction
и metadata-only authorizer. Нет SELECT пользовательских строк, COUNT/sampling,
DDL, DML, ATTACH или исполнения default/check/index/generated/view expressions.
Каждый вызов получает новое соединение и закрывает его до публикации snapshot.

`InspectionLimits` задаётся через `SQLiteTarget.limits`. Defaults: 256 объектов,
500 колонок на объект, 1024 constraints/indexes на объект, 20 000 полученных
metadata rows, 8 MiB полученных metadata, 256 KiB SQL definition и 4096 символов
на публикуемое текстовое поле. Время: 60 s на весь inspection, 10 s на statement,
2 s на ожидание lock и 5 s на cleanup. Одновременный второй вызов на том же
instance отклоняется. Caller не может увеличить policy limits через request.

Cancellation сохраняется как `asyncio.CancelledError` после прерывания worker.
При недоступном cleanup возвращается отдельная ошибка, и instance блокируется
для новых вызовов. Thread interruption не является process sandbox; зависший
OS/filesystem call может пережить cleanup deadline. Snapshot в этом случае нет.

## Контролируемые отказы

| Код | Причина |
| --- | --- |
| `TARGET_NOT_ALLOWED` | Пустой/запрещённый scope, system/temp/attached schema либо недопустимый request. |
| `DATABASE_TARGET_MISMATCH`, `DATABASE_POLICY_MISMATCH` | Request не соответствует trusted target/policy. |
| `DATABASE_OBJECT_NOT_FOUND` | Разрешённый объект отсутствует; adapter не создаёт его. |
| `DATABASE_CATALOG_SCOPE_INCOMPLETE` | FK выходит за разрешённый snapshot либо указывает на отсутствующий столбец. |
| `DATABASE_METADATA_UNSUPPORTED` | Column selectors/denylist, nullable PK, virtual table или metadata, которые нельзя представить полностью. |
| `SECURITY_LIMIT_EXCEEDED`, `PROCESSING_TIMEOUT` | Превышен budget, statement/общий deadline. Частичный snapshot не публикуется. |
| `DATABASE_INSPECTION_BUSY`, `DATABASE_INSPECTION_CLEANUP_FAILED` | Instance занят либо worker не завершил cleanup вовремя. |
| `DATABASE_READ_ONLY_REQUIRED`, `DATABASE_DEPENDENCY_UNAVAILABLE` | Не обеспечен read-only режим либо отсутствует SQLite extra. |
| `DATABASE_SCHEMA_DRIFT` | Только `verify_database_fingerprint`: пересчитанный fingerprint переданного каталога отличается от ожидаемого. Адаптер сам не сравнивает прежнюю схему. |
| `DATABASE_INSPECTION_FAILED` | Ошибка DBAPI/metadata validation, включая unsafe text; raw SQL, path и driver message не публикуются. |

Control metadata и credential canaries отклоняются validators. Comments с inspection metadata
сохраняют CR/LF/TAB, пробелы и пустые строки. Чувствительные данные без canary могут находиться в metadata:
snapshot остаётся локальным; перед отправкой куда-либо нужна отдельная privacy
policy. SDK не настраивает application logging и не пишет metadata в engine logs.

### Совместимость и переход на полный каталог {#m07-catalog-migration}

Legacy `DatabaseCatalog` schema `1.0.0` без `inspection` сохраняет прежний JSON.
Он не принимается pure fingerprint/graph functions как полный snapshot.
`inspect_metadata` сохраняет API A/B и возвращает `DatabaseMetadataSnapshot`;
для нового кода, которому нужны hash/graph, вызывайте `inspect` и принимайте
`DatabaseCatalog` schema `1.1.0` с `fingerprint_version="catalog-v1"`.
Проверяйте версию при чтении сохранённого JSON. DTO проверяет согласованность
структуры, но не пересчитывает заявленный `database_fingerprint` автоматически.

После security review SQLite `DEFERRABLE`/`ON CONFLICT`/column `COLLATE` и
нестандартная PostgreSQL column/domain collation дают explicit unsupported
вместо неполного успешного каталога. Старые результаты повторно инспектируйте;
переписывание строки версии или hash не делает их совместимыми. Целевые
данные/схема не мигрируют. Решения: [ADR 0015](adr/0015-bounded-sqlite-inspection.md)
и [ADR 0017](adr/0017-canonical-catalog-and-dependency-graph.md).

## PostgreSQL — M7-B

Установите `structuraguard[postgres]`. Передайте отдельный DSN inspector в формате
`postgresql+asyncpg://...` с явными host, user и database. Пароль храните в secret
store приложения; adapter не ищет DSN в окружении. Из query options разрешён `ssl`.
Target не принимает готовый writer engine/connection или произвольный SQL.

<!-- example:m07-postgresql:start -->
```python
from pydantic import SecretStr

from structuraguard.contracts import DatabaseInspectionRequest, DatabaseMetadataSnapshot
from structuraguard.database import (
    InspectionLimits,
    PostgreSQLDatabaseAdapter,
    PostgreSQLTarget,
)


async def inspect_postgresql(inspector_dsn: SecretStr) -> DatabaseMetadataSnapshot:
    target = PostgreSQLTarget(
        dsn=inspector_dsn,
        target_id="application",
        include_schemas=("app", "ref"),
        include_tables=(("app", "items"), ("ref", "parents"), ("app", "hidden")),
        deny_tables=(("app", "hidden"),),
        limits=InspectionLimits(statement_timeout_seconds=5),
    )
    request = DatabaseInspectionRequest(
        target_id=target.target_id,
        target_policy_fingerprint=target.policy_fingerprint,
    )
    return await PostgreSQLDatabaseAdapter(target).inspect_metadata(request)
```
<!-- example:m07-postgresql:end -->

Пример предполагает существующие `app.items` и `ref.parents`; все referenced
tables должны входить в allowlist. `inspector_dsn` приложение передаёт из своего
secret store. Для полного каталога замените тип результата на `DatabaseCatalog`
и вызов `inspect_metadata` на `inspect`; остальные policy controls сохраняются.

Имена точные, с сохранением регистра; точка внутри имени не разделяет schema/table.
Deny имеет приоритет. Request может только сузить schemas. System schemas
запрещены. Пустой scope и column-level selectors отклоняются до подключения.
FK должен целиком попадать в scope; adapter не добавляет referenced tables.
Пользовательские enum/domain/array types читаются только для разрешённых columns
и в пределах разрешённых schemas. OID используется лишь внутри transaction,
публичные IDs зависят от структурного пути объекта.

Inspector нужны CONNECT к БД и USAGE к разрешённым schemas; SELECT на rows
не нужен. Provisioning пользователя и GRANT/REVOKE выполняет owner вне SDK.
Inspector и writer должны быть разными principals. Runtime использует новый
`NullPool` connection и `SERIALIZABLE READ ONLY`; read-only, statement/lock timeouts
и `search_path=pg_catalog` задаются при подключении. После завершения, ошибки или
отмены выполняются rollback/close/dispose; snapshot публикуется после cleanup.

Отражаются table/view/materialized view, native/canonical types, nullable/default,
PK/FK/unique/checks и comments schemas/tables/columns/constraints/indexes.
PostgreSQL metadata расширяют общие DTO:

- Enum labels сохраняют объявленный порядок; domain содержит base type,
  default/not-null/CHECK; array содержит element type. Неизвестные types остаются
  canonical `unknown`, сохраняя native name.
- `DatabaseType.domain_constraints` дополняет прежний `domain_checks` именами,
  comments и validity каждого domain CHECK. PostgreSQL adapter возвращает tuple
  descriptors; `None` в старом DTO означает отсутствие расширенных metadata.
  Выражения двух полей должны совпадать, имена CHECK — быть уникальными.
- Stored/virtual generated columns и identity `ALWAYS` явно non-writable.
  Identity `BY DEFAULT` допускает структурную запись. Serial хранит default
  expression; sequence values не выбираются.
- Views/materialized views всегда non-writable, их SQL не исполняется.
- Constraints сохраняют имена, comments, deferrability, initial mode и validity.
  Indexes сохраняют key order, expressions, INCLUDE, predicate, method,
  collation/operator class, sort/null order, NULLS NOT DISTINCT и validity.

PostgreSQL column collation, отличная от `pg_catalog."default"`, пока даёт
`DATABASE_METADATA_UNSUPPORTED`, включая collation, унаследованную от domain.
Текущий DTO отражает collation index keys; column collation отдельно не хранится,
поэтому такой каталог нельзя публиковать с неполным fingerprint.

Defaults общих `InspectionLimits` те же, что для SQLite. В PostgreSQL `max_sql_bytes`
ограничивает запрос adapter, row/text/byte budgets — получаемые metadata.
Строки обрезаются до sentinel на сервере; превышение даёт ошибку, не усечённый
snapshot. Повторно используемые type descriptors учитываются в byte budget.
Глубина nested type descriptors ограничена восемью. Общий deadline включает
подключение; cleanup имеет отдельный budget. При cleanup failure snapshot не
публикуется и instance больше не используется. Caller cancellation сохраняется.

Дополнительные typed errors: `DATABASE_PERMISSION_DENIED` (USAGE/authentication),
`DATABASE_TARGET_INVALID` (DSN/driver/options), `DATABASE_METADATA_UNSUPPORTED`
(EXCLUDE/temporal constraint, foreign table или непредставимые metadata). Raw driver errors,
password и DSN не входят в exceptions/logs. Comments остаются недоверенными
данными. Включение DEBUG SQLAlchemy в приложении не публикует inspection rows.

`make test-database` запускает реальный PostgreSQL 16.15 и 18.6 через Testcontainers;
недоступный Docker даёт ошибку, не skip. Обычный `make test` и общий
`make test-integration` исключают DB containers. PostgreSQL-specific behavior
не проверяется эмуляцией SQLite. Версии 15–18 проходят feature gate, но текущая
проверенная matrix — 16.15/18.6. Server-side memory limit требует настройки PostgreSQL;
statement timeout не заменяет этот контроль. Архитектура:
[ADR 0016](adr/0016-scoped-postgresql-inspection.md).


## Fingerprint и dependency graph — M7-C

<!-- example:m07-catalog:start -->
```python
from pathlib import Path

from structuraguard.contracts import DatabaseCatalog, DatabaseInspectionRequest
from structuraguard.database import SQLiteDatabaseAdapter, SQLiteTarget
from structuraguard.domain import verify_database_fingerprint


async def inspect_catalog(database_path: Path) -> DatabaseCatalog:
    target = SQLiteTarget(
        path=database_path,
        target_id="application",
        include_tables=("customers", "orders"),
    )
    adapter = SQLiteDatabaseAdapter(target)
    request = DatabaseInspectionRequest(
        target_id=target.target_id,
        target_policy_fingerprint=target.policy_fingerprint,
    )
    catalog = await adapter.inspect(request)
    fresh = await adapter.inspect(request)
    verify_database_fingerprint(fresh, catalog.database_fingerprint)
    return fresh
```
<!-- example:m07-catalog:end -->

Оба adapters выполняют одинаковый contract. `catalog.database_fingerprint` —
`sha256:` и 64 hex-символа, `fingerprint_version="catalog-v1"`. Хеш строится из
явной canonical UTF-8 projection. Schemas, tables и columns разрешаются по
точным именам, включая schema qualification; opaque IDs не участвуют в хеше.
Неупорядоченные коллекции сортируются по содержимому. Порядок составных ключей,
index keys, enum labels и ordinal position сохраняется.
Ordinal position — последовательная позиция видимой колонки с нуля. Пропуски
физических PostgreSQL attnum после DROP COLUMN не меняют hash; изменение порядка
видимых колонок меняет. Физические номера используются только для разрешения keys.

В fingerprint входят native/canonical types и nested type descriptors,
nullable/default/generated/identity, PK/FK с действиями, constraints, checks,
indexes, comments и признак поддержки comments. Имена внутренних SQLite
`sqlite_autoindex` исключаются; их содержимое сохраняется. Не входят target,
policy fingerprint, producer, database display name, permissions, `writable`,
производный graph, DSN, timestamps, statistics и пользовательские строки.
Изменение данных поэтому не меняет hash. Комментарий меняет hash, поскольку
может влиять на mapping. Эквивалентность произвольного SQL с разным текстом и
схем разных dialects не гарантируется. Хеш покрывает только отражённый scope и
представимые metadata; например, SQL definition view в DTO не хранится.

После исправлений M7 hash каталога с domains либо историей DROP COLUMN может
отличаться от ранее рассчитанного. Domain CHECK name/comment/validity теперь
участвуют в projection, включая nested domain/array types. Получите свежий
inspection и повторно проверьте binding; сохранённые hash не заменяйте автоматически.
Legacy DTO без нового поля читаются, wire format SQLite без domain metadata сохранён.

`structuraguard.domain` экспортирует чистые функции `canonical_database_catalog`,
`database_fingerprint`, `verify_database_fingerprint`, `build_dependency_graph`.
Они принимают полный `DatabaseMetadataSnapshot` либо `DatabaseCatalog` catalog-v1.
Legacy catalog без inspection metadata отклоняется явно. Functions не выполняют
I/O и не получают права на DB operations. Для данных вне adapters caller задаёт
свои пределы размера; async adapters используют общий deadline и metadata budget
также для вычислений и итогового DTO.

`catalog.dependency_graph` содержит:

- `table_ids` всех объектов, включая изолированные tables/views, и FK `edges`
  parent→child с точными ordered local/remote column pairs.
- `topological_order` для всех узлов DAG и `load_order` только для структурно
  writable tables. Stable tie-break: schema name, table name, table ID.
- `cycles`: strongly connected components с `table_ids` и всеми внутренними FK;
  `self_references` перечисляет self-FK отдельно. При любом цикле оба порядка
  равны `None`, а `requires_explicit_strategy=True`. Частичный порядок не выдаётся.
- `join_table_candidates`: только structural hints с evidence. Нужны ровно два
  FK к разным внешним таблицам, непересекающиеся non-null/non-generated колонки,
  покрывающие все колонки таблицы, и PK или безусловный unique key по их объединению.
  Payload, surrogate, nullable endpoints, self-links, invalid constraints либо
  одно лишь имя таблицы не дают hint. Partial/expression index не заменяет key.

SCC находится итеративно за O(V+E), отдельно выполняется сортировка; все простые
циклы не перечисляются. Graph не определяет бизнес-смысл и не разрешает запись.
Перед будущей загрузкой нужен новый inspection и проверка fingerprint; hash не
заменяет target/policy binding, grants или защиту TOCTOU. Автоматического deferral,
отключения FK и двухфазной загрузки нет. Решение: [ADR 0017](adr/0017-canonical-catalog-and-dependency-graph.md).

## Проверка примеров и границы подтверждения

Оба SQLite Python-блока исполняются из этого Markdown на отдельной fixture;
network audit hook запрещает сетевые обращения. PostgreSQL-блок исполняется
без подмены adapter на реальных Testcontainers 16.15/18.6 с отдельным inspector.
Fixtures создаёт только test admin. Команды из корня репозитория:

```bash
uv run --locked --no-sync pytest -q packages/structuraguard/tests/docs/test_m07_examples.py
uv run --locked --no-sync pytest -q -m database_integration packages/structuraguard/tests/integration/database/test_m07_documented_postgresql_example.py
make docs
```

Критерии связаны с наблюдаемыми tests в [матрице приёмки](plans/M07_acceptance.md).
Последующие исправления и непроверенные угрозы:
[security review M7](plans/M07_security_review.md). Native fuzzing, hard memory
isolation, TLS/MITM, PostgreSQL 15/17 и remote CI этими примерами не подтверждаются.
Сборка MkDocs проверяет внутренние ссылки/anchors; отдельный внешний link checker
не настроен.

[spec-database]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#9-анализ-целевой-базы-данных
[spec-m7]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m7-database-inspector
