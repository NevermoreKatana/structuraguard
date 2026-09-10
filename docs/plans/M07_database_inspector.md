# M07 — безопасный Database Inspector

Статус: M7 A/B/C завершён в описанном inspection scope, commit `1027781`.
Исправление cancellation для Python 3.14 проверено локально и оставлено
uncommitted. Commit/push/PR в шаге исправления не выполнялись.
Дата плана: 2026-09-10. Подготовка к передаче: 2026-09-11.

Актуальная приёмка: **2474 tests** на Python 3.12.9 и 3.14.2;
**18 integration**, **487 security**, **84 PostgreSQL tests** на Python 3.14.2
и Testcontainers 16.15/18.6. [Исправление CI и проверки](#m07-python314).
[Исторический checklist передачи](#checklist-commit-pr).
Канонический scope: [§9 — анализ целевой БД](https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#9-анализ-целевой-базы-данных)
и [M7](https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m7-database-inspector).

Повторная проверка K1–K7: [матрица приёмки, новые tests и результаты](M07_acceptance.md).

Последующий [security review текущего diff M7](M07_security_review.md) обнаружил
и исправил две Medium-проблемы: сохранение sensitive exception context и
публикацию fingerprint при потере непредставимых свойств схемы.

Финальный review дополнительно исправил потерю name/comment/validity domain
CHECK и зависимость fingerprint от физических пропусков после DROP COLUMN.
Regression evidence: [исправления review](M07_acceptance.md#m07-final-review-fixes).

## Исправление cancellation на Python 3.14 {#m07-python314}

В присланном CI log Python 3.14.2: 3 failed, 2466 passed, 84 deselected.
После отмены `asyncio.shield()` позднее исключение внутренней task передавалось
в exception handler event loop, хотя cleanup уже прочитал его. AnyIO фиксировал
ошибку вне ожидаемого error path. Связанный путь PostgreSQL cleanup мог таким же
образом передать необработанный текст driver error, включая sensitive values.

В `database/_inspection.py` и `database/postgresql.py` ожидание заменено на
`asyncio.wait()` с явным чтением результата. Worker/close не отменяются вместе
с вызывающей task; прежние cancellation, общий deadline, cleanup budget и запрет
публикации результата после отмены сохранены. Handler приложения не меняется.
Публичный API, SQL, catalog, dependencies и существующие tests не изменены.

| Критерий | Regression evidence |
| --- | --- |
| K6: отмена/deadline завершают worker без вторичного loop error; даже поздний успешный результат не публикуется | `tests/security/database/test_worker_cancellation.py::test_cancelled_worker_never_reports_handled_error_to_loop`, 4 cases |
| K6: driver error после отмены cleanup не попадает в loop/logs | `tests/security/database/test_worker_cancellation.py::test_cancelled_postgresql_cleanup_does_not_report_driver_error_to_loop`, fake credential canary, без DB connection |
| K6/K7: реальный connection lifecycle не нарушен | `tests/integration/database/test_postgresql_security.py`: timeout, повторная отмена, cleanup deadline и отсутствие оставшихся connections на PostgreSQL 16/18 |

Новые regressions на Python 3.14.2 до исправления: **3 failed, 2 passed**.
После исправления вместе с исходными SQLite/assembly tests: **18 passed**.

Фактически выполненные команды (logs вне repository):

```bash
UV_PROJECT_ENVIRONMENT=/private/tmp/structuraguard-m07-py314 UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache uv sync --python /Users/katana/.local/share/uv/python/cpython-3.14.2-macos-aarch64-none/bin/python3.14 --all-packages --locked --group dev --group docs
UV_PROJECT_ENVIRONMENT=/private/tmp/structuraguard-m07-py314 UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache uv run --locked --no-sync pytest -q --tb=short packages/structuraguard/tests/security/database/test_worker_cancellation.py
UV_PROJECT_ENVIRONMENT=/private/tmp/structuraguard-m07-py314 UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache uv run --locked --no-sync pytest -q --tb=short packages/structuraguard/tests/security/database/test_worker_cancellation.py packages/structuraguard/tests/security/database/test_sqlite_read_only.py packages/structuraguard/tests/unit/database/test_inspection_catalog.py
UV_PROJECT_ENVIRONMENT=/private/tmp/structuraguard-m07-py314 UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache make lint typecheck
UV_PROJECT_ENVIRONMENT=/private/tmp/structuraguard-m07-py314 UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache make test test-integration test-security
DOCKER_CONFIG=/private/tmp/structuraguard-docker-public DOCKER_HOST=unix:///Users/katana/.docker/run/docker.sock UV_PROJECT_ENVIRONMENT=/private/tmp/structuraguard-m07-py314 UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache make test-database
UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache uv run --locked --no-sync pytest -q --tb=short packages/structuraguard/tests/unit/database packages/structuraguard/tests/unit/contracts/test_database_catalog.py packages/structuraguard/tests/security/database
UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache make test
UV_PROJECT_ENVIRONMENT=/private/tmp/structuraguard-m07-py314 make test-build
make test-build
UV_PROJECT_ENVIRONMENT=/private/tmp/structuraguard-m07-py314 UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache make docs
UV_PROJECT_ENVIRONMENT=/private/tmp/structuraguard-m07-py314 UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache uv run --locked --no-sync pytest -q packages/structuraguard/tests/docs/test_m07_examples.py
UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache uv lock --check --offline
git diff --check
```

| Проверка | Фактический результат |
| --- | --- |
| Узкий M7 suite, Python 3.12.9 | 221 passed |
| `make lint typecheck`, Python 3.14.2 | 268 files formatted; Ruff passed; mypy: 266 source files, passed |
| `make test`, Python 3.12.9 / 3.14.2 | По 2474 passed, 84 DB cases deselected, 5 прежних PyMuPDF/SWIG warnings |
| `make test-integration`, Python 3.14.2 | 18 passed, 2540 deselected, 5 прежних warnings |
| `make test-security`, Python 3.14.2 | 487 passed |
| `make test-database`, Python 3.14.2 | 84 passed, PostgreSQL 16.15/18.6; без skips/SAWarning |
| `make test-build`, штатная Python 3.12.9 | wheel/sdist и offline installation/import/examples: passed |
| Дополнительный `make test-build`, Python 3.14.2 | wheel/sdist созданы; verification остановлена: copied Python во временном venv на macOS не находит `libpython3.14.dylib` |
| `make docs`, SQLite documentation examples | Strict build passed; 2 examples passed на Python 3.14.2 |
| `uv lock --check --offline` | 105 packages, exit 0 после повторения вне sandbox: первый вызов uv завершился panic macOS system-configuration |
| `git diff --check`, просмотр пяти изменённых файлов | Passed; generated/debug artifacts и реальные secrets не найдены; новый password literal — тестовый canary |

Проверены outputs `/private/tmp/structuraguard-m07-py31{2,4}-*.log`.
`uv --no-sync` предупреждал о различии локального pin 3.12 и отдельной среды;
pytest header подтверждает фактическую Python 3.14.2. Lock и основная `.venv`
сохранены. Paid LLM API не вызывались.

- [x] Причина CI воспроизведена и закрыта отдельными regression tests.
- [x] K6/K7 подтверждены на Python 3.14.2; обратная совместимость — на 3.12.9.
- [x] Review текущего fix: новых существенных findings не обнаружено;
      cancellation/resource lifecycle и отсутствие driver error в loop проверены.
- [x] План/PROJECT_STATE, strict docs/examples и diff/artifact checks актуальны.
- [ ] Повтор remote CI после fix: локальные изменения ещё не отправлены.
- [ ] Python 3.13 и другие OS: в этом bugfix прогоне не запускались;
      проверены затронутая 3.14 и исходная 3.12 на macOS.
- [ ] Offline installed-package verification на macOS/managed Python 3.14:
      ограничение временного venv описано выше; build tooling вне scope fix.

## История реализации A/B/C

Проверки этапов ниже исторические; актуальные counts включают последующее
[исправление Python 3.14](#m07-python314).

Фактический API части A: [SQLite metadata inspection](../database-inspection.md),
[ADR 0015](../adr/0015-bounded-sqlite-inspection.md).
`SQLiteDatabaseAdapter.inspect_metadata` возвращает `DatabaseMetadataSnapshot`
без фиктивного fingerprint. SQLAlchemy используется для bounded catalog queries;
CHECK/generated/index expressions выделяются ограниченным lexical parser.
Column restrictions, nullable SQLite PK и virtual tables дают явный отказ.
Часть B добавляет PostgreSQL; часть C связывает snapshot с fingerprint и FK graph.

Проверки части A, 2026-09-10 (Python 3.12.9, SQLite 3.49.1,
SQLAlchemy 2.0.52): 54 целевых unit/contract/security/example tests;
`make lint`, `make typecheck`, `make test` — 2287 passed,
`make test-integration` — 18 passed, `make test-security` — 454 passed,
`make docs`, `make test-build`, `uv lock --check --offline` прошли.
Полный suite содержит 5 существующих PyMuPDF/SWIG deprecation warnings.
Review/security review: исправлены FK case resolution, denylist case handling,
изолировано SQLAlchemy DEBUG logging, проверены cancellation/read-only и
граница SQLite integer limit; учтён STRICT ANY. Существенных незакрытых findings
в пределах A нет; ограничения API и worker cleanup описаны в документации.
Integration suite здесь общий, PostgreSQL/Testcontainers в него пока не входят.

Фактический API части B: `PostgreSQLDatabaseAdapter.inspect_metadata`, отдельный
`PostgreSQLTarget` с SecretStr DSN и schema/table scope. SQLAlchemy async Core
выполняет bounded queries к `pg_catalog` в отдельной SERIALIZABLE READ ONLY
transaction. Встроенный Inspector заменён, поскольку его column reflection
загружает enum/domain всех schemas. Решение, типы, ограничения и cleanup:
[ADR 0016](../adr/0016-scoped-postgresql-inspection.md).

PostgreSQL tests используют реальные Testcontainers 16.15 и 18.6, включая
stored/virtual generated columns, named constraints/comments, NOT NULL и
NOT ENFORCED metadata PostgreSQL 18. Writer и final catalog API в B не добавлены.

Проверки части B, 2026-09-10: `make test-database` — 44 passed на PostgreSQL
16.15/18.6, без skips и SQLAlchemy warnings; SQLAlchemy 2.0.52, asyncpg 0.31.0,
Testcontainers 4.15.0, Python 3.12.9. `make test` — 2299 passed (44 DB tests
выбраны отдельной командой), `make test-integration` — 18 passed,
`make test-security` — 462 passed. `make lint`, `make typecheck`, `make docs`,
`make test-build`, `uv lock --check --offline`, `git diff --check` прошли.
Пять прежних PyMuPDF/SWIG deprecation warnings сохраняются в общем suite.

Review/security review B: закрыты выход enum/domain reflection за scope,
утечка SQLAlchemy connection record при cleanup timeout и несовместимость
multiline comments с legacy security contract. Проверены SQL trace без DDL/DML,
отдельный inspector без SELECT grants, read-only даже с owner credentials,
permission/authentication errors без DSN, resource budgets, timeout/lock и
повторная cancellation. Существенных незакрытых findings в scope B нет.
Ограничения: EXCLUDE/temporal constraints, foreign tables, column projection
дают typed отказ; DB server memory требует внешней настройки. Fingerprint,
FK graph/cycles, full DatabaseAdapter port и SDK wiring не входят в приёмку B.


Фактический API части C: `inspect` обоих adapters возвращает `DatabaseCatalog`
schema 1.1.0 с `catalog-v1` SHA-256 и dependency graph после cleanup. Pure domain
functions вычисляют canonical projection/hash, проверяют drift и строят iterative
SCC/DAG. FK IDs полного каталога зависят от owner, ordered pairs и attributes.
Join hints требуют полного structural evidence. Runtime binding и права не
участвуют в schema hash. Решение и ограничения:
[ADR 0017](../adr/0017-canonical-catalog-and-dependency-graph.md).
Writer `execute` явно недоступен; SDK facade и loader остаются за пределами M7.


Проверки части C, 2026-09-10: `make lint` (256 файлов), `make typecheck`
(254 source files), `make test` — 2364 passed, `make test-integration` —
18 passed, `make test-security` — 467 passed. `make test-database` — 50 passed
на PostgreSQL 16.15/18.6, без skips и SQLAlchemy warnings. `make docs`,
`make test-build`, `uv lock --check --offline`, `git diff --check` прошли.
Wheel/sdist проверены offline с основным uv cache; временный cache не содержал
закреплённый hatchling. Пять прежних PyMuPDF/SWIG deprecation warnings остаются.

Review/security review C: проверены явная projection, сохранение composite/enum
order, metadata-only поведение, общий deadline/byte budget после reflection,
сохранение cancellation и запрет writer. Исправлены коллизии одинаковых SQLite
FK; SCC evidence проверяется на полноту и strong connectivity. Domain использует
только contracts, общий pure error contract и stdlib; boundary/import probes
проверены. Существенных незакрытых findings в scope C нет.

Ограничения C: fingerprint покрывает отражённый scope и поля catalog DTO, без
доказательства семантической эквивалентности SQL; view definition в DTO не входит.
Graph не подтверждает grants и не разрешает циклическую загрузку. Pure functions
ожидают ограниченный вызывающим кодом вход; adapters применяют trusted limits.

## Цель

Получать для SQLite и PostgreSQL полный в пределах разрешённого scope каталог
metadata без изменения БД, со стабильным SHA-256 fingerprint и FK graph,
который возвращает порядок зависимостей либо явные циклы.

## Основание и исходное поведение

По строке Database Inspector в [индексе](../codex/SPEC_INDEX.md) извлечены только
разделы 9, 10, 13 и M7 из `StructuraGuard_SDK_Technical_Specification.md`.
DB security requirements извлечены из 20.1–20.4, 20.9–20.12 и 20.14:
недоверенные metadata, разные principals, запрет DDL, limits, privacy и audit.
Основа решений — [ADR 0001](../adr/0001-public-api-and-run-policies.md) и
[ADR 0002](../adr/0002-security-boundary-defaults.md).

| До M7 | Реализовано в M7 |
| --- | --- |
| `ports/database.py`: async `inspect(DatabaseInspectionRequest) -> DatabaseCatalog` и `execute(..., ValidatedMappingPlan, LoadContext) -> LoadReport`; concrete adapter отсутствует. | SQLite и PostgreSQL реализуют inspection через этот port. `execute` явно возвращает `SDK_OPERATION_NOT_IMPLEMENTED` до чтения batches или открытия соединения. |
| `contracts/database.py`: immutable catalog DTO; FK обязан ссылаться на существующие таблицы/столбцы каталога. `read_only` допускает только `True`; request содержит только schema selector, target ID и policy fingerprint. | Trusted target configuration ограничивает scope до reflection; request может только сузить его. Сигнатура port сохраняется. |
| Есть PK/FK/unique/checks/comments, `generated`/`writable`; отсутствуют native/canonical type pair, defaults/identity, indexes, object kind и metadata capabilities. | Versioned extension существующих DTO покрывает эти свойства и различает отсутствие metadata, отсутствие поддержки и ошибку reflection. |
| Fingerprint передаёт caller; `domain/canonical.py` сортирует ключи JSON, но не catalog collections. FK graph не реализован. | Fingerprint строится по отдельной versioned projection; graph вычисляется чистой функцией из проверенного каталога. |
| `sdk.py` и `sync_sdk.py` содержат заглушки orchestration. В extra `postgres` уже есть SQLAlchemy, asyncpg и psycopg; SQLite extra и Testcontainers отсутствуют. | M7 доступен через concrete adapters. Подключение всей SDK facade остаётся отдельной работой; основной пакет импортируется без DB extras и I/O. |

Изучены M2 tests в `tests/unit/contracts/` и `tests/unit/ports/`: declarative
catalog, согласованность PK, запрет secrets/control text, runtime/static port
conformance. Ниже пути исходников считаются от
`packages/structuraguard/src/structuraguard/`, тестов — от
`packages/structuraguard/tests/`; новые пути обозначают будущую работу.

## Критерии приёмки

- **K1 — доступ:** request с чужим target/policy или расширением scope отвергается
  до reflection. Ноль reflection calls к запрещённым объектам, включая FK targets.
- **K2 — неизменность:** inspection использует отдельное read-only соединение;
  SQL trace и снимки fixture до/после подтверждают отсутствие DDL/data mutation,
  чтения пользовательских строк, исполнения views/defaults/checks и LLM calls.
- **K3 — полнота:** каталог отражает разрешённые schemas/tables/views/columns,
  types, PK/FK/unique/checks/indexes/comments/defaults/identity/generated metadata.
  Unsupported и missing не выдаются за пустые успешные значения.
- **K4 — стабильность:** перестановка результатов reflection не меняет IDs/hash;
  изменение любого поля fingerprint projection меняет hash. Изменение строк БД,
  времени, credentials или producer version не меняет schema fingerprint.
- **K5 — граф:** parent предшествует child в DAG; составные FK сохраняют пары
  столбцов; self-reference и несколько циклов дают детерминированные diagnostics.
- **K6 — ограничения:** timeout, cancellation, oversized metadata, denied access
  и незавершённый snapshot не возвращают успешный каталог; соединение закрыто,
  в logs/errors/audit нет DSN, исходного SQL и sensitive metadata.
- **K7 — совместимость:** M2 round-trip/security/port tests сохраняются;
  новые DTO имеют явную версию. Один inspection contract suite проходит на обеих
  СУБД, PostgreSQL подтверждён реальным Testcontainers run.

## Затронутые контракты

| Контракт / файл | Изменение M7 |
| --- | --- |
| `contracts/database.py`, `contracts/__init__.py` | Catalog schema `1.1.0`: optional version-gated поля для native type, canonical type и параметров (precision/scale/length/timezone/array/enum/domain), ordinal position, default, identity/autoincrement, generation expression, object kind, indexes и capabilities. Сохранить `type_name` как legacy поле; в новом output оно согласовано с canonical type. Для FK добавить доступные actions/deferrability. |
| `ColumnCatalog`, `TableCatalog` | `generated=True` всегда означает `writable=False`; views и materialized views также не writable. `writable` — консервативная пригодность с учётом target policy, а не доказательство grants writer. Отдельно фиксировать `unknown` для непроверенных прав; inspector не подключается writer credentials. |
| Index DTO | Ordered key items различают column reference и expression; учитывать unique, predicate, direction, included columns и доступные dialect options. Partial/expression unique index не превращается в обычный unique/natural key. Не терять index, дублирующий unique constraint, и не считать его вторым constraint. |
| Trusted target configuration, `database/target.py` | Instance-local target ID/identity, inspection connection factory/secret, immutable максимальная schema/table/column policy, denylist, capabilities и limits. Секреты не входят в request/catalog/repr. Writer connection не требуется для M7. |
| `DatabaseInspectionRequest` | Сохранить имеющиеся поля; при необходимости добавить optional qualified table selectors, которые только сужают trusted allowlist. Пустой selector означает trusted scope, а не «вся БД»; пустая maximum allowlist запрещает inspection. |
| `DatabaseCatalog` | Сохранить `database_fingerprint`, `target_id`, `target_policy_fingerprint`, `producer`; добавить fingerprint format version и metadata capabilities. Не переименовывать `database_fingerprint` в `fingerprint` из иллюстративного примера ТЗ. |
| `domain/database_graph.py`, graph DTO | Pure result: узлы, parent→child edges с FK evidence, topological order, strongly connected components и cycle diagnostics. Каталог и graph не содержат SQLAlchemy objects или DB connection. |
| `exceptions.py`, `contracts/common.py` | Typed inspection error и стабильные коды: существующие `TARGET_NOT_ALLOWED`, `DATABASE_TARGET_MISMATCH`, `PROCESSING_TIMEOUT`, `SECURITY_LIMIT_EXCEEDED`; новые `DATABASE_POLICY_MISMATCH`, `DATABASE_READ_ONLY_REQUIRED`, `DATABASE_OBJECT_NOT_FOUND`, `DATABASE_METADATA_UNSUPPORTED`, `DATABASE_CATALOG_SCOPE_INCOMPLETE`, `DATABASE_INSPECTION_FAILED`, `DATABASE_SCHEMA_DRIFT`. |

Legacy `1.0.0` читается и сериализуется без новых пустых полей; `1.1.0`
не интерпретируется неподдерживающим её consumer молча: capability/version gate
обязателен до использования каталога. Добавить JSON golden tests и migration
note. Не менять глобальные `IdentifierStr`/`_safe_text` ради DB adapter.
Миграций целевой БД и переписывания сохранённых plans нет. Новые fingerprints
получаются повторным inspection; прежние нельзя объявить совместимыми автоматически.

## Обязательные решения безопасности

### Соединение и scope

1. Trusted owner связывает target ID с immutable target identity и policy
   fingerprint. Сверить request до открытия соединения; проверить доступное
   evidence целевой БД до reflection. Credentials и имя БД сами по себе не
   доказывают identity; будущий writer должен использовать ту же trusted binding.
2. **PostgreSQL:** отдельный least-privilege inspector без DML/DDL grants,
   ownership, superuser и наследуемых write roles. Одна read-only transaction
   с `SERIALIZABLE` (asyncpg readonly, ADR 0016), bounded connect/statement/lock timeouts и проверкой
   `transaction_read_only` до inspection. Session setup не меняет persistent
   configuration. Read-only transaction не заменяет запрет опасных операций:
   PostgreSQL отдельно оговаривает temporary tables и записи на диск.
   [PostgreSQL: SET TRANSACTION](https://www.postgresql.org/docs/current/sql-set-transaction.html).
3. **SQLite:** отдельное соединение к существующему fixture/target file с URI
   `mode=ro`, `query_only` как дополнительной защитой и явной read transaction.
   Не использовать `immutable=1` для изменяемой БД, не выполнять `ATTACH`,
   journal/schema changes, extension loading или user functions.
   In-memory SQLite не считается доказательством OS/file read-only режима:
   обязательный contract suite работает с временным файлом, созданным fixture.
   [SQLite URI](https://www.sqlite.org/uri.html),
   [SQLite PRAGMA](https://sqlite.org/pragma.html).
4. Пересечь schema/table selectors с максимальной allowlist и применить denylist
   **до** discovery/reflection; запрос расширения scope — explicit failure.
   Использовать явно qualified schema/table, без wildcard и зависимости от
   пользовательского `search_path`. System/temp schemas не являются targets;
   чтение необходимых `pg_catalog`/`sqlite_schema` metadata разрешено только для
   inspection конкретных разрешённых объектов.
5. Не вызывать безграничный `MetaData.reflect()` и не отражать FK targets
   автоматически. Разрешённые точечные Inspector calls предпочтительнее;
   `filter_names` допустим только после проверки фактического SQL, `resolve_fks`
   должен быть отключён при использовании Table reflection.
   [SQLAlchemy reflection](https://docs.sqlalchemy.org/en/20/core/reflection.html),
   [Table.resolve_fks](https://docs.sqlalchemy.org/en/20/core/metadata.html#sqlalchemy.schema.Table.params.resolve_fks).
6. Column policy проверяется до reflection. Если разрешены все metadata таблицы,
   допустим bounded `get_columns`; при column allowlist/denylist требуется
   ограниченная проекция catalog query. Если dialect не может её обеспечить,
   вернуть `DATABASE_METADATA_UNSUPPORTED` до широкого чтения. Post-filter не
   считается достаточной защитой. Constraints с запрещёнными references не
   публиковать как полные; применить правило замкнутого scope ниже.
7. Значения параметризуются, identifiers передаются dialect quoting API только
   после policy validation. Метаданные считаются данными: никакого выполнения
   выражений, callbacks из БД, SQL от caller/LLM или восстановления grants.
   DDL для создания fixtures выполняет отдельный test admin вне adapter.

### Неполный scope и сложные metadata

| Случай | Обязательное поведение M7 |
| --- | --- |
| FK ведёт вне allowlist или к скрытому столбцу | Не расширять reflection, не удалять FK и не создавать фиктивный target. Вернуть `DATABASE_CATALOG_SCOPE_INCOMPLETE` без запрещённых имён в diagnostics. Это сохраняет closed-graph invariant текущего DTO; внешний FK может быть поддержан позднее отдельным versioned contract. |
| Views / materialized views | Только явно разрешённые объекты; kind и columns из metadata, `writable=False`, без SELECT из view и без вывода PK/FK по эвристике. Materialized view — отдельная PostgreSQL capability. |
| Generated/default/identity | Сохранить признаки и bounded декларативные выражения, не вычислять их. Computed column не writable; identity/autoincrement отличать от computed, identity ALWAYS консервативно не writable. |
| Native/canonical types | Явная versioned таблица соответствий, без inference по значениям. Неизвестный тип сохраняется как native + canonical `unknown`, без предположения о безопасном mapping. SQLite affinity не выдаётся за строгую типизацию. |
| PK / unique | Сохранить порядок composite keys. `ColumnCatalog.unique=True` означает только безусловную уникальность одного столбца. SQLite nullable non-integer PK нельзя молча сделать NOT NULL: в M7 вернуть `DATABASE_METADATA_UNSUPPORTED`, сохранив текущий DTO invariant; расширение такого контракта — последующая работа. |
| Checks / indexes | Получать структуру и bounded выражения; не использовать regex для доказательства эквивалентности SQL. Unsupported expression/index reflection не выдавать за отсутствие. Если важные metadata нельзя представить полностью, завершить inspection ошибкой. |
| Comments | SQLite не обещает catalog comments: `None` + unsupported capability. В PostgreSQL отсутствие комментария — `None` при supported capability. Не интерпретировать comments как instructions и не передавать их LLM автоматически. |
| Unsafe / oversized metadata | Existing validators остаются границей: control text, credential canary, недопустимые identifiers и превышение длины дают безопасную typed error без raw content. Не обрезать молча expressions/comments для успешного fingerprint. |
| Quoted names | Сохранить case и точное имя; разделять schema/table/column структурно. Если текущий DTO изменяет имя через strip или не вмещает его, explicit unsupported, а не обращение к другому объекту. |
| Missing / denied / reflection failure | Не создавать пустой «успешный» каталог. Missing разрешённого объекта и permission failure имеют отдельный reason; отсутствующая metadata feature отличается от denied access. |

Ограничение SQLite nullable PK следует из реального dialect behavior:
[SQLite PRIMARY KEY](https://sqlite.org/lang_createtable.html#the_primary_key).
Metadata-only — единственный режим M7: нет `COUNT`, sampling, aggregates,
profiling пользовательских строк, SQL к views или вызовов LLM. Разделы 10 и 13
задают будущих consumers каталога, но не добавляют semantic mapper в этот milestone.

### Resource/time limits

Фактические defaults `InspectionLimits`: statement — 10 s, lock wait — 2 s,
полный inspection — 60 s, cleanup — отдельные 5 s; один active inspection на
adapter instance. Scope ограничен 256 objects, 500 columns на объект,
1024 constraints/indexes на запрос metadata, 20 000 metadata items на фазу
reflection/сборки и 8 MiB metadata, отдельный текст — 4096 символов;
`max_sql_bytes` — 256 KiB. Полный catalog проверяется также по размеру serialization.

Первоначальное предложение отдельных connect/pool budgets по 5 s, `max_schemas=16`
и уменьшения limits через request не реализовано. PostgreSQL использует NullPool
без ожидания свободного connection; connect ограничен минимумом statement/total
timeouts (default 10 s). SQLite busy timeout — минимум lock/total timeout.
Число отражённых schemas ограничено выбранными objects; отдельного schema cap нет.
Limits задаёт trusted target, request сужает только schemas и не изменяет budgets.
Это уточнение фактического контракта; отдельные настройки не заявляются готовыми.

Проверять cardinality/длины bounded preflight queries до materialization,
затем budgets при каждом fetch и normalization. Не полагаться на проверку размера
готового DTO. Если Inspector предварительно буферизует неограниченные данные,
использовать bounded dialect query либо explicit unsupported path.
Общий monotonic deadline охватывает connect, retries, reflection, hash/graph;
автоматических retries в M7 нет. SQLite busy timeout ограничивает ожидание lock;
для длительного исполнения нужны deadline-aware progress callback/interrupt.
[SQLite progress handler](https://www.sqlite.org/c3ref/progress_handler.html),
[SQLite interrupt](https://sqlite.org/c3ref/interrupt.html).

Cancellation прерывает driver operation, завершает worker и закрывает/инвалидирует
connection за bounded cleanup; отмену нельзя превратить в успешный пустой catalog.
Success публикуется после завершения read transaction и cleanup. Не менять
глобальный logging: отключить SQL echo для принадлежащего adapter engine;
errors/audit содержат только коды, разрешённые IDs, counts и hashes, без raw
driver exception/statement/connection repr и sensitive comments/defaults.

## Fingerprint и FK graph

**Формат `catalog-v1`.** Использовать существующий canonical JSON/SHA-256 helper
над явной projection, а не всем `DatabaseCatalog.model_dump()`: helper сам не
исключает `database_fingerprint`. Вход содержит format version, dialect,
qualified schemas/objects, kind, columns с ordinal/types/nullability/default/
identity/generated attributes, PK/FK с actions, unique, checks, indexes,
comments и capabilities. Comments включены сознательно: они влияют на будущий
mapping. Derived graph, permissions/writable policy, target ID, DSN, database
display name, policy fingerprint, producer/version, время, statistics и строки
исключены. Policy и target identity проверяются отдельно от schema fingerprint.
Hash вычисляется по UTF-8 canonical JSON; результат — `sha256:` и 64 lowercase
hex digits, совместимые с текущим `FingerprintStr`.

Сортировать schemas/tables/columns по точным стабильным keys, сохраняя ordinal
position отдельно; unordered collections constraints/indexes — по canonical
content. Не сортировать компоненты composite PK/FK/index, enum values и пары FK:
их порядок семантически значим. Не приводить SQL identifiers к lower case и не
нормализовать Unicode так, чтобы разные объекты слились. Выражения хранить точно
в согласованном безопасном representation; равенство семантически эквивалентного
SQL с разным текстом не обещается. Между dialects hash может различаться.

IDs вычислять из versioned structured qualified tuple с type prefix и SHA-256,
а не из позиции в результате, OID или неоднозначного `schema.table.column`.
FK ID связывать с owner и ordered local/remote pairs плюс structural attributes;
для неназванных constraints не использовать позицию reflection. Полностью
идентичные FK получают ordinal только внутри группы одинакового содержимого;
их перестановка сохраняет множество IDs. Golden tests
фиксируют bytes/hash и отсутствие коллизий qualified names в выбранной схеме IDs.
DDL drift проверяется только новым inspection без устаревшего Inspector cache.
M7 предоставляет сравнение с `DATABASE_SCHEMA_DRIFT`; recheck непосредственно
перед load остаётся обязанностью будущих validator/loader. Существующий
`DATABASE_FINGERPRINT_MISMATCH` для contract binding сохраняется.

Graph строить только из FK evidence: parent→child, включая изолированные таблицы,
composite/cross-schema и несколько FK между двумя узлами. DAG получает
детерминированную топологическую сортировку с stable tie-break. Для циклов
вычислять strongly connected components, включая self-loop, и возвращать
`requires_explicit_strategy` с FK evidence; полный executable load order не
выдавать. Не перечислять все simple cycles: их число может расти экспоненциально.
SCC обход должен быть ограничен O(V + E), без зависимости от глубины Python
recursion; сортировка для стабильного порядка выполняется отдельно.
Двухфазную загрузку, отключение constraints и выбор deferral M7 не выполняет.
Join-table candidates выводить только как структурные hints с evidence, без
утверждения бизнес-смысла или назначения natural keys по статистике.

## Выполненные шаги

Последовательность ниже сохранена из рабочего плана; A/B/C реализованы.

### A. Dialect-neutral normalization и SQLite adapter tests

1. **Сначала tests:** `unit/database/test_normalization.py`,
   `unit/database/test_sqlite_inspection.py`,
   `unit/contracts/test_database_catalog.py`,
   `security/database/test_inspection_policy.py` и
   `security/database/test_sqlite_read_only.py`. Fixtures: composite keys,
   generated columns, defaults, views, quoted names, checks/indexes,
   nullable PK, missing table, FK за scope и hostile metadata.
2. **Реализация:** versioned additions в `contracts/database.py`, policy/limits
   в `database/target.py`; `database/normalization.py` преобразует только
   bounded metadata в DTO-compatible records. `database/sqlite.py` использует
   SQLAlchemy со stdlib sqlite3 в ограниченном worker, без blocking I/O в event
   loop; соединение создаётся/закрывается внутри worker, cancellation отдельно
   прерывает SQLite. `database/_inspection.py` задаёт общий lifecycle и budgets.
3. SQLite extra в `packages/structuraguard/pyproject.toml` включает SQLAlchemy,
   без нового async SQLite driver; DB test dependencies явно устанавливаются
   в dev/CI. Обновить `uv.lock`; проверить лицензии/поддержку выбранных версий.
   `database/__init__.py` не открывает соединений и не тянет optional drivers
   при импорте ядра.
4. Зафиксировать долгоживущие решения в новом ADR в `docs/adr/`: closed scope,
   DTO compatibility, stable IDs и fingerprint projection. Рассмотренные
   альтернативы: broad reflection + filtering отклонена из-за access boundary;
   внешние FK stubs отложены из-за изменения graph contract; молчаливое
   исправление nullable PK отклонено из-за искажения metadata. Здесь это план
   решения; ADR оформляется вместе с реализацией A.
5. **Проверка:** `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/database packages/structuraguard/tests/unit/contracts/test_database_catalog.py packages/structuraguard/tests/security/database`;
   затем `make lint typecheck`. SQL trace проверяет allowlist до reflection,
   file fixture остаётся неизменным после success/error/cancel.

### B. PostgreSQL reflection + Testcontainers

1. **Сначала tests:** `integration/database/conftest.py`,
   `integration/database/test_postgresql_inspection.py`,
   `integration/database/test_postgresql_security.py`.
   Test admin создаёт fixtures и inspector role без write grants, без SELECT
   на пользовательские строки. Проверить read-only state, cross-schema FK,
   search_path collisions, comments, enum/domain/numeric/timezone/array,
   identity/serial/generated, materialized views, partial/expression indexes,
   deferrable FK, timeout/lock/cancellation и отказ без достаточных прав.
2. **Реализация:** `database/postgresql.py`, общий lifecycle и normalization из
   A. Использовать asyncpg extra и SQLAlchemy async Core с ограниченными SELECT
   к `pg_catalog` внутри read-only transaction. Принятое уточнение вместо
   `run_sync(Inspector)` — [ADR 0016](../adr/0016-scoped-postgresql-inspection.md).
   Проверить отсутствие reflection вне allowlist, включая enum/domain;
   schema qualification не должна зависеть от `search_path`.
   [PostgreSQL reflection/search_path](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#remote-schema-table-introspection-and-postgresql-search-path).
3. **Среда:** Testcontainers — только dev dependency в корневом `pyproject.toml`;
   обновить `uv.lock`, marker `integration`/новый `database_integration`,
   `Makefile` и `.github/workflows/ci.yml`. Выбрать и закрепить поддерживаемый
   PostgreSQL image tag + digest; не использовать `latest`. CI DB job явно
   устанавливает `postgres`/SQLite extras и запускает PostgreSQL suite на Docker.
   Default offline suite исключает DB containers; явный DB job при отсутствии
   Docker/dependencies завершается ошибкой, а не успешным skip.
   [Testcontainers Python](https://github.com/testcontainers/testcontainers-python).
4. **Проверка:** `uv run --locked --no-sync pytest -m database_integration packages/structuraguard/tests/integration/database`;
   затем `make lint typecheck test-integration`. Fixture setup/teardown отделены
   от inspection SQL trace; schema/data snapshots сравниваются test admin.

### C. Stable fingerprint + FK dependency graph/cycles

1. **Сначала tests:** `unit/database/test_fingerprint.py`,
   `unit/database/test_dependency_graph.py`, `unit/database/test_inspection_catalog.py`,
   `contract_suites/database.py`, `integration/database/test_postgresql_catalog.py`. Property tests переставляют
   reflection collections; golden tests фиксируют формат; mutation matrix
   меняет по одному значимому полю. Negative controls меняют rows, target/policy и opaque IDs;
   producer, DSN и timestamps исключены из явного списка полей projection; composite order и quoted-name collisions проверяются
   отдельно. Pure graph fixtures: пустой каталог (без DB inspection),
   isolated nodes, chain, diamond, composite/cross-schema FK, self-loop,
   несколько SCC и потенциальная join table.
2. **Реализация:** `domain/database_fingerprint.py`,
   `domain/database_graph.py`, graph DTO в `contracts/database.py`,
   assembly в `database/_catalog.py`, lifecycle в `database/_inspection.py`. Pure domain не импортирует SQLAlchemy
   или adapters. Объединить A/B в публичный `DatabaseAdapter.inspect`,
   возвращающий каталог только после validation, hash и cleanup. В A/B до этой
   сборки проверяются внутренние snapshots; фиктивный fingerprint наружу не
   публикуется. Общий inspection contract suite из C прогнать на обоих adapters.
3. **Документация:** `docs/database-inspection.md`, `docs/public-api.md`,
   `mkdocs.yml`, ADR 0017 и executable offline SQLite example в документации:
   configuration, metadata-only defaults, limits/errors, fingerprint version,
   цикл и правила передачи metadata будущему mapper. Проверить imports без extras.
4. **Проверка:** `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/database`;
   `uv run --locked --no-sync pytest -m database_integration packages/structuraguard/tests/integration/database`.
   DDL drift создаётся только test admin между inspections; data-only update
   не меняет fingerprint. Затем review/security review и общие gates ниже.

Зависимости: A задаёт общий normalized snapshot и SQLite path; B добавляет
PostgreSQL path; C завершает публичный каталог/hash/graph поверх A и B.
Принятие отдельных внутренних шагов не означает готовность всего M7.

## Проверки перед приёмкой M7

Полный прогон в окружении с DB extras, Docker и Testcontainers:
`make lint`, `make typecheck`, `make test`, `make test-integration`,
`make test-security`, `make test-database`, `make docs`; дополнительно `uv lock --check` и
`make test-build` из-за новых extras/exports. Зафиксировать реальные команды,
версии DB/driver, counts и skips; никакой skipped PostgreSQL suite не закрывает K7.
Фактически выполненные проверки, включая подготовку к commit, перечислены ниже.

## Передача к ручному commit и PR {#checklist-commit-pr}

Историческая запись передачи до commit `1027781`. Checklist, состав 64 файлов
и команды ниже сохранены как evidence этого этапа; текущий статус и follow-up
исправление описаны [выше](#m07-python314).

Ветка `feat/m07-database-inspector`, HEAD `b976282`; локальная `main` указывает
на тот же commit. Staging area пуст. Remote refs не обновлялись. В текущем шаге
изменены только этот план и `docs/codex/PROJECT_STATE.md`; source/tests/lock/CI
совпадают с состоянием после последнего полного прогона и исправления review.

### Checklist по фактическому состоянию

- [x] A — dialect-neutral types и SQLite read-only inspection реализованы.
- [x] B — bounded PostgreSQL reflection проверена реальными Testcontainers 16/18.
- [x] C — canonical fingerprint, FK graph, DAG/SCC/self-reference и join evidence.
- [x] K1 — target/policy/allowlist/denylist проверяются до reflection, FK scope замкнут.
- [x] K2 — отдельное read-only соединение, metadata-only; нет DDL/DML/user rows/LLM.
- [x] K3 — представимые types/constraints/indexes/comments/generated metadata полны;
      missing/unsupported дают typed отказ, domain CHECK metadata сохранены.
- [x] K4 — canonical SHA-256 и IDs стабильны при перестановке reflection;
      domain CHECK drift обнаруживается, история DROP COLUMN не меняет logical hash.
- [x] K5 — parent-first DAG, composite/cross-schema FK, SCC/self-reference проверены;
      недостаточное evidence не даёт join hint.
- [x] K6 — budgets, permission/errors, timeout/cancellation/cleanup и redaction
      подтверждены tests; неполный catalog не публикуется.
- [x] K7 — legacy contracts, version gates, common port suite обоих dialects,
      отсутствие import-time I/O и offline package probes проходят.
- [x] Все критерии сопоставлены с наблюдаемыми tests в [матрице приёмки](M07_acceptance.md).
- [x] Security и финальный review findings исправлены с regression tests;
      существующие assertions, lint и typecheck не ослаблены.
- [x] Документация/русские docstring/examples и PROJECT_STATE актуальны;
      действуют ADR 0015–0017, канонический scope указан ссылками на ТЗ.
- [x] `git diff --check`, состав tracked/untracked diff и артефакты проверены.
- [x] Реальных credentials в просмотренных 64 файлах не найдено: 8 pattern hits
      относятся к локальным Testcontainers fixtures и тестовым canaries.
      Runtime AST не содержит debug prints/breakpoints/eval/exec.
- [x] `dist/`, `site/` и caches игнорируются Git; generated/debug artifacts в
      кандидатах на commit отсутствуют. Временные probes/logs находятся вне repo.
- [ ] Ручной staging и commit — оставлены пользователю по явному указанию.
- [ ] Push и Pull Request в `main` — не выполнялись по явному указанию.
- [ ] Проверка актуального remote `main` и CI будущего PR — после передачи;
      локальные refs и локальные suites не подтверждают удалённое состояние.

### Фактически выполненные команды

После последнего изменения runtime (шаг исправления review) выполнены:

```bash
UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache uv run --locked --no-sync pytest -q packages/structuraguard/tests/unit/database packages/structuraguard/tests/unit/contracts/test_database_catalog.py packages/structuraguard/tests/security/database
UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache make lint typecheck
DOCKER_CONFIG=/private/tmp/structuraguard-docker-public DOCKER_HOST=unix:///Users/katana/.docker/run/docker.sock UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache uv run --locked --no-sync pytest -q --tb=short -W error::sqlalchemy.exc.SAWarning -m database_integration packages/structuraguard/tests/integration/database/test_m07_review_regressions.py
DOCKER_CONFIG=/private/tmp/structuraguard-docker-public DOCKER_HOST=unix:///Users/katana/.docker/run/docker.sock UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache make test-database
UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache make test test-integration test-security
UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache uv run --locked --no-sync pytest -q packages/structuraguard/tests/unit/database/test_domain_constraints.py packages/structuraguard/tests/docs/test_m07_examples.py
UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache make lint typecheck docs
make test-build
```

| Проверка | Последний фактический результат |
| --- | --- |
| Узкий M7 suite | 216 passed |
| Новые PostgreSQL regressions | 14 passed; до исправления 8 ожидаемых failures |
| `make lint typecheck` | 267 files / 265 source files, passed |
| `make test` | 2469 passed, 84 DB cases deselected |
| `make test-integration` | 18 passed, 2535 deselected |
| `make test-security` | 482 passed |
| `make test-database` | 84 passed на PostgreSQL 16.15/18.6, без skips/SAWarning |
| Domain contract и SQLite examples | 19 passed |
| `make docs` | Strict build и внутренние ссылки/anchors — passed |
| `make test-build` | wheel/sdist, offline rebuild/import/examples — passed |
| `uv lock --check --offline` | 105 packages, exit 0; повторено на этапе передачи |
| `git diff --check` | Exit 0; повторено на этапе передачи |

`make test` и `make test-integration` сохраняют пять прежних PyMuPDF/SWIG warnings.
Это не skips и не ослабление gates. PostgreSQL suite выполняется отдельным target.
Локальная среда: Python 3.12.9, SQLite 3.49.1, SQLAlchemy 2.0.52, asyncpg 0.31.0,
Testcontainers 4.15.0. Outputs проверены по локальным
`/private/tmp/structuraguard-m07-review-fix-*.log`.

На этапе передачи дополнительно выполнены:

```bash
git status --short
git diff --stat
git diff --check
git diff --cached --stat
git branch --show-current
git log -1 --format='%h %s'
git show-ref --verify refs/heads/main
git log --format='%h %s' main..HEAD
git diff --name-only HEAD
git ls-files --others --exclude-standard
git check-ignore dist site .pytest_cache .mypy_cache .ruff_cache
python3 /private/tmp/structuraguard-m07-handoff-scan.py
UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache uv lock --check --offline
UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache uv run --locked --no-sync pytest -q packages/structuraguard/tests/docs/test_m07_examples.py
UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache make docs
```

Локальный scan проверил имена/расширения всех кандидатов, patterns private keys,
access tokens, credential URLs/password literals и runtime AST на debug/dynamic
execution calls. Все 8 совпадений просмотрены вручную: только тестовые значения.
`gitleaks`/`detect-secrets` не установлены; специализированного scanner target нет.
SQLite documentation examples на этапе передачи повторно прошли: **2 passed**.
Strict docs build и `git diff --check` после обновления checklist — passed.

### Состав milestone diff {#m07-commit-files}

64 файла: 15 modified и 49 untracked. Все относятся к M7; перед ручным commit
нужно включить новые файлы вместе с tracked edits. Runtime/contract/domain,
tests/fixtures, docs/ADR и dependency/CI/package changes перечислены полностью:

```text
.github/workflows/ci.yml
Makefile
docs/adr/0015-bounded-sqlite-inspection.md
docs/adr/0016-scoped-postgresql-inspection.md
docs/adr/0017-canonical-catalog-and-dependency-graph.md
docs/codex/PROJECT_STATE.md
docs/database-inspection.md
docs/plans/M07_acceptance.md
docs/plans/M07_database_inspector.md
docs/plans/M07_security_review.md
docs/public-api.md
mkdocs.yml
packages/structuraguard/pyproject.toml
packages/structuraguard/src/structuraguard/contracts/__init__.py
packages/structuraguard/src/structuraguard/contracts/database.py
packages/structuraguard/src/structuraguard/database/__init__.py
packages/structuraguard/src/structuraguard/database/_catalog.py
packages/structuraguard/src/structuraguard/database/_inspection.py
packages/structuraguard/src/structuraguard/database/_postgresql_catalog.py
packages/structuraguard/src/structuraguard/database/_postgresql_queries.py
packages/structuraguard/src/structuraguard/database/_sqlite_sql.py
packages/structuraguard/src/structuraguard/database/normalization.py
packages/structuraguard/src/structuraguard/database/postgresql.py
packages/structuraguard/src/structuraguard/database/sqlite.py
packages/structuraguard/src/structuraguard/database/target.py
packages/structuraguard/src/structuraguard/domain/__init__.py
packages/structuraguard/src/structuraguard/domain/_database_catalog.py
packages/structuraguard/src/structuraguard/domain/database_fingerprint.py
packages/structuraguard/src/structuraguard/domain/database_graph.py
packages/structuraguard/tests/contract_suites/database.py
packages/structuraguard/tests/docs/test_m07_examples.py
packages/structuraguard/tests/integration/database/conftest.py
packages/structuraguard/tests/integration/database/test_m07_acceptance.py
packages/structuraguard/tests/integration/database/test_m07_documented_postgresql_example.py
packages/structuraguard/tests/integration/database/test_m07_postgresql_security_review.py
packages/structuraguard/tests/integration/database/test_m07_review_regressions.py
packages/structuraguard/tests/integration/database/test_postgresql_catalog.py
packages/structuraguard/tests/integration/database/test_postgresql_inspection.py
packages/structuraguard/tests/integration/database/test_postgresql_security.py
packages/structuraguard/tests/packaging/test_ci_contract.py
packages/structuraguard/tests/packaging/test_metadata.py
packages/structuraguard/tests/security/database/test_dependency_evidence.py
packages/structuraguard/tests/security/database/test_inspection_policy.py
packages/structuraguard/tests/security/database/test_m07_full_inspection.py
packages/structuraguard/tests/security/database/test_m07_security_review.py
packages/structuraguard/tests/security/database/test_postgresql_policy.py
packages/structuraguard/tests/security/database/test_sqlite_read_only.py
packages/structuraguard/tests/smoke/test_m02_layer_boundaries.py
packages/structuraguard/tests/unit/contracts/test_database_catalog.py
packages/structuraguard/tests/unit/database/conftest.py
packages/structuraguard/tests/unit/database/test_dependency_graph.py
packages/structuraguard/tests/unit/database/test_domain_constraints.py
packages/structuraguard/tests/unit/database/test_fingerprint.py
packages/structuraguard/tests/unit/database/test_inspection_catalog.py
packages/structuraguard/tests/unit/database/test_m07_catalog_contract.py
packages/structuraguard/tests/unit/database/test_m07_fingerprint_acceptance.py
packages/structuraguard/tests/unit/database/test_m07_graph_acceptance.py
packages/structuraguard/tests/unit/database/test_m07_sqlite_acceptance.py
packages/structuraguard/tests/unit/database/test_normalization.py
packages/structuraguard/tests/unit/database/test_sqlite_edge_cases.py
packages/structuraguard/tests/unit/database/test_sqlite_inspection.py
pyproject.toml
scripts/verify_distribution.py
uv.lock
```

### Рекомендуемый commit и PR

Conventional Commit: `feat(database): реализовать безопасный Database Inspector M7`.

PR title: `M7: безопасный инспектор SQLite/PostgreSQL, fingerprint и FK graph`.

PR body:

> Добавлены отдельные read-only adapters SQLite/PostgreSQL с allowlist/denylist
> до reflection, ограничениями metadata/времени и typed errors. Versioned catalog
> содержит stable SHA-256, FK load order и diagnostics циклов; legacy DTO сохранены.
>
> Проверки: lint/typecheck, 2469 основных tests, 482 security, 84 PostgreSQL tests
> на реальных 16.15/18.6, strict docs и offline wheel/sdist verification.
> Writer и SDK orchestration остаются вне scope M7.

### Пропущенные проверки и причины

| Не выполнено | Причина / граница подтверждения |
| --- | --- |
| Повтор полного runtime/DB/build suite в этом docs-шаге | Source/tests/dependencies не менялись; последний полный зелёный прогон указан выше. После правок Markdown повторяются docs/examples и diff checks. |
| PostgreSQL 15/17 | Feature gate допускает 15–18, но закреплённая Testcontainers matrix содержит только 16.15/18.6. |
| Remote CI, свежий remote `main`, другие OS/Python builds | Commit/push/PR не создаются; fetch/удалённый pipeline не запускались. Проверена локальная macOS/Python 3.12.9 среда. |
| Полный advisory scan / специализированный secret scanner | Отдельных configured targets/tools нет; выполнены review dependencies/lock и scan текущего diff, но это не полный advisory/entropy audit. |
| Native fuzzing, hard OS/server memory limits, TLS/MITM | Требуют отдельной fuzz/deployment/transport среды; проверены программные budgets и контролируемая cancellation/cleanup. |
| Все concurrent DDL schedules и нагрузка при одновременном достижении всех максимумов | Есть управляемые interleavings, limit tests и глубокий SCC; exhaustive schedules и throughput/RSS benchmark не выполнялись. Значения limits — ограничения, не гарантии производительности. |
| Writer/staging/load, SDK orchestration, audit sink и LLM DB mapping | Вне inspection scope M7; эти функции не заявляются готовыми. Paid LLM API не вызывались. |

Перед будущим load нужны fresh inspection, повторная проверка target/policy/hash
и защита TOCTOU. Fingerprint не покрывает view SQL definition, произвольную
семантическую эквивалентность SQL и metadata вне отражённого scope. Сохранённые
M7 hashes domains/схем с DROP COLUMN нельзя автоматически считать совместимыми.

## Scope и риски

Обязательный scope — K1–K7, A–C и перечисленные dialect metadata. Позднее:
value profiling, semantic catalog ingestion/aliases, deterministic/LLM mapping,
natural-key selection, staging/load/upsert, фактическая двухфазная загрузка,
автоматическое применение DDL и SDK orchestration. Sections 10/13 требуют
пригодных metadata и graph evidence, а не реализации этих consumers в M7.

- **Совместимость:** ограничения текущего DTO дают explicit unsupported для
  nullable SQLite PK и некоторых unusual identifiers/expressions. Внешние FK
  требуют замкнутой allowlist; скрыто расширять scope нельзя.
- **Reflection:** SQLAlchemy/driver версии могут менять текст/полноту metadata;
  поддержку фиксируют versioned capabilities и fixtures, обновление проходит
  golden tests. Unsupported essential metadata блокирует успешный snapshot.
- **Resource exhaustion:** drivers могут буферизовать metadata раньше adapter;
  preflight/SQL bounds и driver cancellation обязательны, одного async timeout
  недостаточно. Непроверенный cleanup блокирует приёмку соответствующего dialect.
- **Schema drift:** read transaction даёт ограниченный во времени snapshot,
  а не вечную гарантию. Concurrent DDL/error не должны порождать смешанный
  успешный каталог; coherence проверяется integration tests. Перед будущей
  записью нужен новый inspection и отдельная защита TOCTOU в loader.
- **Metadata privacy:** comments/defaults могут содержать sensitive text даже
  без credential canary. В M7 они остаются локальными данными, не logs/prompts;
  внешняя передача потребует отдельной classification/redaction policy.
