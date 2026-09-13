# M13 — безопасные staging и loader для PostgreSQL

Дата: 2026-09-13. Статус: самостоятельные PostgreSQL API A–D подготовлены к
ручному commit и отдельному PR в `main`; полный milestone принят частично.
AC-01/02/05 остаются открытыми. Актуальные проверки и состав diff — в
[checklist подготовки](#m13-commit-readiness).

Подтверждённая поставка отдельных A–D API описана в
[staging](../staging.md), [dry-run](../dry_run.md) и [loader](../loader.md).
Полный M13 принят частично: [матрица критериев и тестов](M13_acceptance.md).
Этот план сохраняет целевой scope, включая ещё не поставленные сценарии.

## Цель

Загружать проверенный immutable набор через staging в существующие таблицы
PostgreSQL с `dry_run`, `insert_only`/`upsert`, порядком FK, атомарным commit,
quarantine и защитой от повторной записи после сбоя.

## Основания и поведение на момент планирования

Из ТЗ извлечены **только §17–18, §20.3–20.4 и M13** командой
`python3 scripts/extract_spec_sections.py '17.' '18.' '20.3' '20.4' 'M13'`.
При планировании строка staging/loader в `docs/codex/SPEC_INDEX.md` указывала M11;
в ходе документации исправлена на актуальный раздел M13.

Обязательные основания: [run policies](../adr/0001-public-api-and-run-policies.md),
[PostgreSQL inspection](../adr/0016-scoped-postgresql-inspection.md),
[catalog-v1 и граф](../adr/0017-canonical-catalog-and-dependency-graph.md),
[M11](../adr/0021-mapping-plan-validation.md),
[M12-C](../adr/0024-deterministic-record-constraints.md),
[M12-D](../adr/0025-provenance-replay-and-validation-report.md).

Ниже пути реализации и тестов указаны относительно
`packages/structuraguard/`; `src/` означает `src/structuraguard/`.
Таблицы проектирования ниже сохраняют исходный замысел; названия предполагаемых
DTO и файлов не являются обещанием поставленного API. Текущие имена и границы
описаны в руководствах и матрице приёмки выше.

| Изученная точка | Уже есть | Следствие для M13 |
|---|---|---|
| `src/ports/stores.py`, `src/contracts/database.py` | Только `StagingStore.stage(batch, context)`; `StagingContext` связывает run, target, normalized/DB/policy fingerprints, expiry и max_records. Реализации store нет. | Нужны чтение, seal, владение run, конечные состояния, retention и полная staging envelope. Одного успешного `stage` недостаточно. |
| `src/mapping/validation.py`, `_validation_identity.py`, `_validation_relations.py` | M11 перепроверяет DTO/hash/scope, возвращает wrapper только при ACCEPTED. Evidence содержит policy/options/writable hashes, identities и выбранный load_order. | Повторно вызвать M11 на свежем каталоге; legacy wrapper или самосогласованные hashes не становятся capability. |
| `src/contracts/mapping_rules.py` | `source_values`, `mapped_parent`, `lookup` проверяются структурно. `generated_key`, `deferred`, `two_phase` отклоняются. | Реализовать первые три стратегии на реальных записях; не расширять молча MappingPlan 1.1.0. |
| `src/domain/database_graph.py`, `src/contracts/database.py` | Граф parent→child, ordered composite pairs, SCC; `dependency_order` работает на выбранных узлах. Schema hash исключает grants, target и writable policy. | Переиспользовать алгоритм M7 и evidence M11. Цикл вне выбранного подграфа не блокирует загрузку; права проверять отдельно. |
| `src/contracts/reports.py`, `src/contracts/provenance.py`, `src/validation/reporting.py` | Legacy `ValidationReport`; `DetailedValidationReport` 1.1 с coverage/lineage. Builder принимает результаты trusted composition, но не доказывает target projection. ACCEPTED запрещает invalid/unverified records. | Нужен явный мост от staged values к ValidationDataset, findings и SQL parameters. Quarantine нельзя включить простым игнорированием REJECTED. |
| `src/ports/database.py`, `src/database/postgresql.py` | `DatabaseAdapter.execute` принимает batches/wrapper/LoadContext; PostgreSQL implementation всегда выдаёт `SDK_OPERATION_NOT_IMPLEMENTED` до I/O. `PostgreSQLTarget` предназначен inspector. | Отдельный writer adapter/service с явными credentials и execution request; inspector сохраняет read-only поведение. |
| `src/contracts/reports.py`, `src/contracts/database.py` | `LoadReport` различает DRY_RUN/COMMITTED/ROLLED_BACK/UNKNOWN; loaded count допустим только при COMMITTED. Write policy требует staging, transactional audit, rollback. | Сохранить wire invariants; не выдавать tentative rowcount за committed результат. Строковые capability IDs из LoadContext проверить через реальные зависимости владельца. |

Изученные опорные тесты: `tests/unit/mapping/test_m11_validation.py`,
`tests/unit/database/test_dependency_graph.py`, load/report cases в
`tests/unit/contracts/test_m02_invariants.py`, PostgreSQL tests M11/M12-C и
`tests/integration/database/conftest.py` с закреплёнными PostgreSQL 16/18.

Отдельный [аудит приёмки M13](M13_acceptance.md) сопоставляет исходные критерии
с реальными тестами и фиксирует непоставленные части. Он не изменяет критерии ниже.

## Критерии приёмки

- [ ] **AC-01.** До EOF, seal, проверки lineage/projection, актуального M11 и обязательных
  уровней M12 нет target DML. Непроверенный required layer блокирует run.
- [ ] **AC-02.** Dry-run возвращает воспроизводимый execution plan и `would_load_records`;
  `loaded_records=0`. Target, sequences, persistent staging и idempotency ledger
  остаются неизменными; память и соединения освобождены.
- [x] **AC-03.** Write выполняет только разрешённые INSERT/UPDATE с подтверждёнными ключами;
  порядок родителей/детей не зависит от разбиения входа на batches.
- [x] **AC-04.** Atomic — default: ошибка позднего batch, SQL, аудита или commit откатывает всю
  target transaction. Quarantine явно выбирается и сохраняет ошибочные единицы
  с provenance; run-level security/identity/schema blockers всегда имеют veto.
- [ ] **AC-05.** Повтор или конкурентный запуск с тем же idempotency binding не создаёт дублей,
  включая insert с DB-generated identity и потерю ответа COMMIT.
- [x] **AC-06.** Нет DDL, произвольного SQL, чтения вне scope и смешения inspector/writer user.
  Отмена, deadline, rollback failure и неизвестный commit имеют разные outcomes.
- [x] **AC-07.** M02 wire snapshots и API inspection/M11/M12 сохраняются; новые контракты
  versioned, SQLAlchemy/asyncpg остаются инфраструктурой с существующим DB extra.

Исходные формулировки сохранены. Отметки AC-03/04/06 относятся к поддержанному
bounded scope и проверенным fault paths, перечисленным в [матрице](M13_acceptance.md).
Для AC-04 применяются принятые ADR 0028/0029: потеря ответа COMMIT означает
UNKNOWN/reconciliation, а не доказанный rollback. Подтверждённый COMMIT сохраняется
при сбое staging finalize/recovery; найденный в финальном review `RuntimeError`
закрыт regression tests. Непроверенная внешняя DB семантика отклоняется.

| Открытый критерий | Подтверждённая часть | Что ещё требуется |
|---|---|---|
| AC-01 | EOF/seal, exact-copy projection, свежие M11 и M12 DB constraints | Coordinator обязательных physical/schema/business/provenance layers перед DML |
| AC-02 | Ordered plan, target-unit counters, отсутствие изменений target/staging/ledger/sequences | Связь с прежним `LoadReport.would_load_records`/`loaded_records`; измерение пиковой памяти |
| AC-05 | Durable replay, concurrent same-key binding, recovery lost COMMIT | Успешный insert/replay DB-generated PK; сейчас проверен безопасный отказ |

## Затронутые контракты и границы

| Файл / компонент | Предлагаемое изменение |
|---|---|
| `src/contracts/staging.py`, `src/ports/stores.py` | Additive `StagingRun`, `StagedRecord`, `StagingReceipt`, `StagingRunStatus`, `StagingLimits`; `RunStagingStore` расширяет прежний StagingStore операциями begin/seal/read/finalize/cleanup. Старые реализации stage-only остаются совместимыми, но loader отклоняет их до target I/O. |
| `src/contracts/loading.py`, `src/ports/loading.py` | Data-only `LoadPreparationRequest`, `LoadExecutionPlan`, `LoadExecutionRequest`, `LoadExecutionResult` и async loader protocol. Request связывает sealed receipt, manifest, M11/M12 evidence, при необходимости M8 profile, LoadContext и execution fingerprint. Result содержит существующий LoadReport и отдельные детали units/quarantine/replay. |
| `src/loading/projection.py`, `src/loading/planning.py` | Trusted composition: bounded projection immutable records/normalization sidecars → ValidationDataset → target write units. Проверить manifests, source/value refs, completeness и все bindings, используя существующие validators. |
| `src/database/writer_target.py`, `src/database/loader.py` | Отдельные `PostgreSQLWriterTarget` и `PostgreSQLLoader`; secrets доступны только constructor/config владельца. Loader сам формирует SQL. Inspector не получает writer DSN и не меняет прежнюю execute-заглушку. |
| `src/stores/memory.py`, `src/stores/postgresql.py` | Bounded in-memory store для dry-run/контрактов; durable PostgreSQL store для реального run/quarantine. Один versioned wire format и общий набор contract tests. |
| `src/contracts/reports.py`, `src/contracts/common.py`, `src/exceptions.py`, exports | Переиспользовать LoadReport/LoadPolicy/LoadContext; новые подробности вынести в sidecar. Добавить typed staging/load errors и закрытые codes без ослабления legacy invariants. |

Перед реализацией A оформить долгоживущее решение в новом
`docs/adr/0026-staging-and-loader-transactions.md`: lifecycle, same-database
commit ledger/audit, блокировки, compatibility и trust boundary projection.
Это следующий артефакт реализации; данный план не меняет статус принятых ADR.
Полный ingest/facade coordinator остаётся отдельной интеграцией. Узкий мост
M12→staging→loader входит в M13: без него безопасную запись открыть нельзя.

## Шаги

### A. StagingStore contract/implementation и run lifecycle

1. **Сначала тесты:** `tests/contract/stores/test_run_staging_store.py`,
   `tests/unit/stores/test_memory_staging.py`,
   `tests/unit/contracts/test_m13_staging.py`. Проверить повтор batch, подмену
   payload под тем же ID, запись после seal, чужой run/target, expiry, лимиты,
   отмену stage/seal и отсутствие частично опубликованного snapshot.
2. **Реализация:** `contracts/staging.py`, `ports/stores.py`, `stores/memory.py`,
   `stores/postgresql.py`. Begin фиксирует run_id, staging_id, source fingerprint
   (`source_sha256` из существующего source evidence), extraction/parse/normalized,
   DB/target/policy и mapping fingerprints, версии producer и UTC timestamps.
   Envelope хранит исходную и нормализованную запись, normalization sidecar,
   mapping, issues/warnings/status/provenance. Physical artifacts можно хранить
   отдельно по immutable refs только при гарантированном сроке владения ими;
   ссылка на уже закрытый borrowed snapshot не заменяет сохранение оригинала.
3. **Lifecycle:** `OPEN → SEALED → EXECUTING → COMMITTED | QUARANTINED |
   ROLLED_BACK | FAILED | CANCELLED | UNKNOWN`. QUARANTINED означает завершённый
   run с отклонёнными units, а не отдельный transaction outcome. До EXECUTING
   возможен FAILED/CANCELLED; expiry не создаёт ложный rollback.
   CAS по revision/attempt token допускает одного исполнителя; stale owner не
   может finalize новый attempt. Seal возможен только после EOF и успешного
   закрытия owned stream, фиксирует counts/content hash и запрещает append.
   После известного rollback повтор создаёт новый attempt на том же immutable
   seal; terminal attempt не переоткрывается для изменения данных.
4. Повтор `(run, batch ordinal, content fingerprint)` — no-op; другой content
   для того же ordinal — `STAGING_CONTENT_MISMATCH`. Read выдаёт bounded страницы
   только из sealed snapshot, сверяет content hashes и при чтении не мутирует его.
   Общие max_bytes/records/batches/issues, page/batch size, TTL и deadline задаются
   владельцем; превышение — явный отказ, без усечения и скрытого disk spill.
5. Durable schema использует рекомендованные §17 `import_batches`, `records`,
   `validation_issues`, `mapping_results`. Дополнительные `execution_commits` и
   `audit_events` нужны D для атомарного свидетельства commit. Версия схемы и
   constraints проверяются при открытии store. **Все объекты заранее создаёт
   migration_admin вне pipeline**; отсутствующая/несовместимая схема даёт
   `STAGING_SCHEMA_UNAVAILABLE`, никогда CREATE/ALTER/TRUNCATE/GRANT.
6. Успешные raw payload очищаются по явной retention policy; quarantine/failed
   сохраняются до срока разбирательства. Cleanup scoped по run/tenant, не трогает
   активный lease, UNKNOWN и idempotency tombstone. DELETE staging payload требует
   отдельного ограниченного housekeeping principal, не права DELETE target.
   При отмене закрыть iterator/connection; stage batch публикуется целиком или
   отсутствует. Persistent staging — sensitive data с ограничением доступа;
   repr/log/audit не содержат payload, DSN, paths и пользовательские сообщения.
7. **Проверка:**
   `uv run --locked --no-sync pytest packages/structuraguard/tests/contract/stores packages/structuraguard/tests/unit/stores packages/structuraguard/tests/unit/contracts/test_m13_staging.py`;
   PostgreSQL store — `test_postgresql_staging.py` из матрицы ниже.

### B. dry-run и execution plan

1. **Сначала тесты:** `tests/unit/loading/test_projection.py`,
   `tests/unit/loading/test_execution_plan.py`, `tests/integration/test_sqlite_load_planning.py`.
   Подменить sealed values, normalized manifest, M11 policy/evidence и M12 report;
   проверить EOF, missing/null, normalization sidecar и стабильность плана при
   другом batch size. Подтвердить, что stage-only store и legacy report не дают write.
2. **Реализация:** `contracts/loading.py`, `ports/loading.py`,
   `loading/projection.py`, `loading/planning.py`. Сначала bounded повторная DTO
   validation, затем hashes, trusted target/policy bindings и повтор M11.
   Projection фиксирует для каждой target cell source record/entity/value ID,
   выбранное значение, normalization evidence и CatalogColumnRef; сравнивает
   точные scalar kinds/значения, не выполняет coercion. SQL получает именно эти
   проверенные значения. Нормализация не переписывает старый manifest/MappingPlan:
   изменившиеся типы/значения требуют новой revision и upstream evidence.
3. Проверить `DetailedValidationReport.complete`, required/completed layers,
   required/verified values, unresolved records и точное соответствие findings
   staged records. Required layers задаёт trusted policy, не вызывающий клиент.
   Отдельно связать input_fingerprint, report evidence, ValidationDataset,
   catalog, MappingPlan и validation policies: ValidationLineage M12 не содержит
   собственного mapping/database binding. Hash внешнего DTO не доказывает
   projection; при отсутствии trusted composition повторить проверки через M12.
   Для replay нужны сохранённые physical/normalized snapshots, ParsePlan/execution
   context и trusted normalization registry/policies. Недоступность этих зависимостей
   блокирует подготовку; loader не открывает пути/URLs из report ради их получения.
4. Execution plan хранит versioned fingerprints входа/политик, выбранные
   identities, ordered column refs, FK strategies, target load_order, write-unit
   dependencies, допустимые INSERT/UPDATE columns, error mode, bounds и counters.
   В нём нет SQL, credentials, connections, callable и raw values. Sensitive
   identifiers доступны только в полном артефакте; safe summary — закрытые codes
   и числа. Fingerprint зависит от семантики и sealed content, но не clock/run_id
   или размера страниц чтения staging/SQL batches. Уже связанные upstream
   fingerprints не переписываются ради нового batching. Runtime budgets проверяются
   отдельно; границы исходных NormalizedBatch и их lineage сохраняются в staging.
5. Dry-run использует bounded immutable snapshot в памяти и read-only metadata/разрешённые EXISTS;
   writer engine не открывается. Никаких target DML, persistent stage/ledger writes,
   `nextval`, исполнения defaults/triggers или EXPLAIN ANALYZE. Redacted control-plane
   audit допустим по ADR 0001. INSERT с rollback отвергнут: sequence values
   не возвращаются при abort. [PostgreSQL: sequences](https://www.postgresql.org/docs/18/functions-sequence.html).
6. По уточнённому требованию M13-B dry-run различает planned inserts/updates/skips/
   quarantine по одному read-only snapshot. Счётчики относятся к target write units
   и не обещают commit. Решение и пределы реализации: [ADR 0027](../adr/0027-read-only-dry-run-planning.md). Непроверенные CHECK/default/key semantics
   отмечаются blockers, не pass. Dry-run не резервирует identity/FK/idempotency;
   реальный execute заново проверяет БД и строит сопоставимый план.
   Старый успешный dry-run не является разрешением записи.
7. **Проверка:**
   `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/loading/test_projection.py packages/structuraguard/tests/unit/loading/test_execution_plan.py packages/structuraguard/tests/integration/test_sqlite_load_planning.py`;
   PostgreSQL dry-run — `test_postgresql_dry_run.py`.

### C. insert_only/upsert/FK resolution/load order

Реализован atomic PostgreSQLLoader; конкретные границы и отклонения от исходного
плана: [ADR 0028](../adr/0028-atomic-postgresql-insert-upsert.md), [API](../loader.md).

1. **Сначала тесты:** `tests/unit/loading/test_load_order.py`,
   `tests/unit/database/test_loader_sql.py`, `tests/contract/loading/test_loader.py`;
   затем PostgreSQL insert/upsert/relations из матрицы. SQL snapshots проверяют
   реальные binds/refs, а результаты PostgreSQL — содержимое таблиц.
2. **Реализация:** `database/writer_target.py`, `database/loader.py`,
   `database/_load_queries.py`, `loading/relations.py`. Writer `data_importer`
   использует отдельный login от `schema_inspector`; совпадение effective principal
   или endpoint/target mismatch блокирует запись. Конфигурация и фактические
   session identity проверяются отдельно от schema fingerprint. Нет automatic
   role switching, owner/superuser/BYPASSRLS, migration_admin или credentials из DTO.
3. Максимальные grants: CONNECT/USAGE и SELECT/INSERT/UPDATE разрешённых target
   tables; SELECT только разрешённых lookup tables/columns; USAGE только нужных
   identity/serial sequences. DDL/DELETE/TRUNCATE и управление ролями запрещены.
   Точный schema/table/column allowlist пересекается с MappingValidationPolicy
   до reflection, lookups и каждой SQL операции; deny сильнее allow. Доступ к
   internal staging/ledger — отдельный фиксированный scope, недоступный MappingPlan.
4. SQL строит исключительно adapter средствами SQLAlchemy Core. Все scalar
   values — bind parameters; identifiers разрешаются по свежему каталогу и
   экранируются dialect compiler. Никаких source/LLM expressions, `text(payload)`,
   literal_binds или подстановки names в DML. Служебные LOCK/transaction statements
   имеют фиксированную структуру и только проверенные/quoted qualified identifiers.
   `search_path=pg_catalog`, SQL/parameters скрыты в driver diagnostics.
5. `insert_only` не обновляет существующую строку и не скрывает конфликт через
   DO NOTHING. DB defaults/identity допускаются только для missing разрешённых
   полей; явный null остаётся null. Generated columns никогда не входят в INSERT/
   UPDATE projection. Неподдержанные server-side semantics — отказ до DML.
6. `upsert` использует identity, уже выбранную M11, с повторной проверкой полного
   PK/безусловного UNIQUE: явный ключ → разрешённый source PK → unique → настроенный
   natural key. Недопустимый explicit key не заменяется другим. Nullable/partial/
   expression/invalid/deferred arbiters и statistical uniqueness запрещены.
   `ON CONFLICT DO UPDATE` обновляет только перечисленные mutable columns;
   identity/PK/generated columns не меняются. Конфликт иного unique constraint —
   ошибка, не выбор другой записи. При пустом update set существующая identity даёт явный skip по ADR 0027;
   новая identity вставляется под write-table lock.
   Дубликаты identity внутри всего staged набора, включая разные batches,
   отклоняются без last-write-wins. [PostgreSQL: INSERT](https://www.postgresql.org/docs/18/sql-insert.html).
7. FK `source_values`/`lookup` проверяют ordered composite tuple по разрешённому
   parent key; lookup означает поиск точного FK key, не перевод произвольного
   natural key в surrogate ID. `mapped_parent` проверяет конкретные record/entity
   связи и значения обоих концов; co-occurrence M8 не доказывает принадлежность.
   Missing/null учитываются по nullable и MATCH semantics каталога. Нет match —
   record failure, несколько match/неподтверждённая уникальность — run-level blocker.
   Parent EXISTS — precheck; окончательную целостность обеспечивает FK при DML.
8. Переиспользовать `dependency_order` для выбранных tables; внутри таблицы —
   стабильный порядок write units. Parent может лежать в более позднем source
   batch: поэтому порядок строится после общего seal. Lookup-only parent не
   становится целью INSERT. Цикл/self-reference в selected graph даёт
   `CYCLIC_DEPENDENCY_REQUIRES_STRATEGY`; generated-key propagation, deferred и
   two-phase остаются `MAPPING_RELATION_STRATEGY_UNSUPPORTED`. Автогенерируемый PK
   самостоятельного parent insert предусмотрен планом, но не поддержан текущим C; связь с child через RETURNING — вне M13.
9. **Проверка:**
   `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/loading/test_load_order.py packages/structuraguard/tests/unit/database/test_loader_sql.py packages/structuraguard/tests/contract/loading`;
   PostgreSQL — `test_postgresql_insert.py`, `test_postgresql_upsert.py`,
   `test_postgresql_load_relations.py`.

### D. rollback/quarantine/idempotency/schema recheck

1. **Сначала тесты:** `tests/unit/loading/test_quarantine.py`,
   `tests/unit/loading/test_idempotency.py`, `tests/security/loading/test_ledger_boundary.py`;
   PostgreSQL barriers/fault injection проверяют состояние из независимого
   соединения, не только возвращённые counters.
2. **Реализация:** `database/_load_transaction.py`, `database/_load_ledger.py`,
   `database/_load_ledger_schema.py`, `loading/groups.py`, `loading/idempotency.py`.
   Transaction owner — loader; caller не передаёт внешнюю
   transaction, где SDK не мог бы подтвердить commit/rollback.

#### Реализация M13-D

1. `contracts/loading.py`, `loading/idempotency.py`: явный ledger/key, стабильный
   binding и replay counters; pure/security tests на binding и redaction.
2. `database/ledger.py`, `_load_ledger_schema.py`: explicit bootstrap, проверка
   фиксированной схемы и SELECT/INSERT-only grants; PostgreSQL bootstrap tests.
3. `loading/groups.py`, `database/_load_transaction.py`: group SAVEPOINT,
   transactional marker/audit/quarantine, schema recheck до DML/commit; PostgreSQL
   failure/cancellation/concurrency tests.
4. `database/loader.py`: claim после ledger lock, replay/recovery и staging
   finalize без ложного rollback; затем все quality gates и review.

Архитектурные решения: [ADR 0029](../adr/0029-loader-ledger-and-quarantine.md).
Фактическая PostgreSQL матрица собрана в `test_postgresql_load_outcomes.py`;
публичный контракт — [loader](../loader.md). Legacy atomic без ledger сохранён.

#### Transaction boundaries и schema recheck

| Граница | Поведение |
|---|---|
| T-stage | Короткие отдельные staging transactions; затем immutable seal. Target DML отсутствует. Ошибочные records переживают rollback T-load. Dry-run пропускает persistent T-stage. |
| T-load | Одна writer connection и одна outer transaction на весь sealed run: locks → fresh scoped catalog/runtime checks → M11/projection gates → idempotency claim → parent lookups и DML → transactional audit и commit marker → COMMIT. Ни transport batch, ни таблица не создают промежуточный commit. |
| T-finalize | После известного результата закрыть staging attempt и сохранить подробности/quarantine. Ошибка T-finalize после commit — warning с восстановлением по ledger, не ROLLED_BACK. |

Выбран консервативный baseline M13: `READ COMMITTED`, table locks
`SHARE ROW EXCLUSIVE` на write tables, `ACCESS SHARE` на lookup-only tables;
единый порядок `(schema, table)`, strongest lock сразу и **до первого чтения
целевых данных, reflection и savepoint**. Блокировки write tables препятствуют
конкурентному DML и изменению их структуры до исхода transaction. Это осознанно
снижает параллелизм; table-level UPDATE grant нужен даже insert-only writer
для такой блокировки. При недостаточных правах — отказ, без GRANT и ослабления
lock mode. Lookup tables не получают UPDATE grants; гонка с удалением parent
разрешается реальным FK либо rollback. [PostgreSQL: LOCK](https://www.postgresql.org/docs/18/sql-lock.html),
[совместимость locks](https://www.postgresql.org/docs/18/explicit-locking.html).

Имена для первоначального lock берутся из trusted exact scope, а не из plan;
после lock разрешение объекта проверяется заново. Writer preflight переиспользует
bounded PostgreSQL reflection на **той же connection**, не отдельный inspector
snapshot. Повторно сверить catalog-v1 hash, target/policy, writable flags, grants,
identities, FK и load_order. `DATABASE_SCHEMA_DRIFT` блокирует DML; ещё раз
проверить scope/runtime metadata перед commit. Изменение grants не входит в hash
и проверяется отдельно, фактические SQL privileges остаются последней защитой.

Runtime metadata дополнительно проверяют RLS, relation kind, triggers/rules и
наследование/partition routing, которые нельзя считать покрытыми catalog-v1.
M13 отклоняет views, foreign/partitioned/inherited tables, RLS и пользовательские
triggers/rules; обычные FK triggers не отключаются. Неизвестные функции/defaults/
type semantics не получают автоматического разрешения. Административные изменения
types/functions/permissions и объектов вне table-lock coverage сериализуются
владельцем deployment с импортами; SDK не обещает изоляцию от враждебного admin.
Эту prerequisite и проверяемый runtime fingerprint зафиксировать в ADR и документации.

#### Atomic и quarantine

- Atomic: любая ошибка до commit отменяет весь T-load; confirmed rollback даёт
  `loaded_records=0`, tentative counters остаются только внутренними.
- Quarantine: global M11/security/provenance/policy/schema/system gates обязательны.
  Полный M12 report с record failures остаётся REJECTED; отдельный admission result
  классифицирует только закрытые recoverable codes и точные ordinals. Unverified,
  неполные уровни и неатрибутируемые findings нельзя отправить в quarantine ради pass.
- Единица загрузки — исходная normalized record со всеми её entities/target rows;
  разбиение одной записи по таблицам не создаёт частичного успеха. Дополнительные
  parent-child/business dependencies между records объединяются в неделимые группы.
  Группы строятся до DML; невозможность доказать closure блокирует quarantine.
  Ошибка child откатывает и новый parent той же группы; никакой «половины документа».
- SAVEPOINT охватывает всю dependency group, не один SQL statement. FK/table order
  соблюдается внутри неё. Известный record constraint failure → rollback savepoint,
  quarantine группы и продолжение независимых групп. Serialization/deadlock,
  permission/schema/connection/timeout/audit error → полный rollback, не продолжение
  aborted transaction. `best_effort` DTO сохранён, но M13 явно отклоняет этот режим.
- Summary LoadReport при частичном commit — COMPLETED_WITH_WARNINGS/COMMITTED,
  `loaded_records + rejected_records = attempted_records` в единицах исходных
  normalized records. Target row counts — отдельные counters. ERROR findings
  остаются в sensitive quarantine artifact; LoadReport содержит предупреждение
  об отклонённых units, не меняет severity исходного ValidationReport. Если все
  units quarantined, нулевой target DML и commit ledger фиксируют этот исход.

#### Идемпотентность и transactional audit

- Уникальный scope ledger: `(trusted tenant/namespace, target_id, idempotency_key)`.
  Binding включает source/extraction/parse/normalized fingerprints, mapping plan,
  database fingerprint, effective policy/error mode, validation и execution
  semantics. run_id, время, попытка и batch size не создают новую identity.
  Тот же key с другим binding — `IDEMPOTENCY_KEY_CONFLICT`, не новый импорт.
- `execution_commits` находится в той же PostgreSQL БД, что target. Unique claim
  создаётся внутри T-load и сериализует одинаковые ключи. Конкурент ждёт bounded
  deadline: после commit получает сохранённый результат, после rollback может
  выполнить весь run. Перед target DML проверять уже committed binding; повтор
  не вызывает INSERT даже для DB-generated PK. Исполненный run с quarantine
  повторяет тот же результат; исправленные данные требуют нового key и plan.
- Marker, committed unit outcomes/quarantine references и redacted append-only
  audit events пишутся в T-load вместе с target changes. Публичный
  `AuditStore.append` сам по себе не доказывает участие в transaction: нужен
  внутренний SQL-backed writer sink. Сбой sink до COMMIT откатывает target.
  Внешняя доставка аудита позже читает durable события; отдельный broker не нужен.
- Marker хранит достаточно typed evidence для восстановления результата без raw
  payload. После очистки staging сохраняется tombstone на весь заявленный срок
  idempotency policy; истечение payload TTL не разрешает дубликат. In-memory
  registry и внешняя БД не обеспечивают crash-safe commit и для write M13 запрещены.
- Replay возвращает исходный committed LoadReport с его run_id; sidecar связывает
  текущую попытку с исходным run и явно отмечает отсутствие новых записей. Нельзя
  суммировать loaded_records сохранённого результата как работу нового attempt.
- Потерян ответ COMMIT → UNKNOWN. Reconcile использует тот же key и primary БД,
  ожидает разрешения unique claim/старой transaction; отсутствие marker в одном
  SELECT ещё не доказывает rollback незавершённой transaction. Пока исход не
  установлен, нельзя присвоить FAILED/ROLLED_BACK, освободить UNKNOWN lease по TTL
  или автоматически повторить target DML с новым ключом.

#### Cancellation semantics, deadlines и ошибки

| Момент | Требуемый outcome |
|---|---|
| Отмена до target transaction | Закрыть owned streams/staging resources; CANCELLED/NOT_STARTED, target пуст. |
| Отмена до отправки COMMIT | Cancel query, bounded shielded rollback/cleanup, сохранить CANCELLED с подтверждённым outcome; затем распространить `asyncio.CancelledError`. Не возвращать success вместо отмены. |
| Configured timeout до commit | FAILED/PROCESSING_TIMEOUT после rollback/cleanup; исход transaction фиксировать отдельно. |
| Отмена/timeout во время COMMIT или разрыв соединения | Не считать отмену доказательством rollback; bounded resolution, иначе UNKNOWN и reconcile. Caller cancellation распространяется, outcome читается по run/ledger. |
| Подтверждённый commit, затем отмена/timeout/finalize failure | Committed effect сохраняется, post-commit warning; rollback и повтор загрузки запрещены. При доставленной caller cancellation исключение распространяется после фиксации evidence. |
| Не подтверждён rollback/cleanup | UNKNOWN, безопасный typed failure, connection invalidate/terminate; нельзя возвращать её в pool или считать run готовым к retry. |

Deadline общий для run; statement_timeout/lock_timeout ограничены оставшимся
временем. Cleanup имеет отдельный конечный budget, переживает повторную отмену;
не оставляет неучтённую background task. Deadlock/serialization могут разрешать
только новый полный attempt из seal после известного rollback, с тем же binding
и конечным retry budget; автоматический retry M13 по умолчанию выключен.

Rollback гарантирует отсутствие committed target rows, но не возврат потраченных
sequence numbers. Внешние эффекты пользовательских triggers не транзакционны
в общем случае, поэтому они исключены baseline. Не менять sequence обратно.
Ошибки SQLSTATE переводятся в закрытые SDK codes; driver text, parameters,
constraint names, DSN и payload не попадают в safe reports/logs.

**Проверка D:**
`uv run --locked --no-sync pytest packages/structuraguard/tests/unit/loading/test_quarantine.py packages/structuraguard/tests/unit/loading/test_idempotency.py packages/structuraguard/tests/security/loading/test_ledger_boundary.py`;
затем PostgreSQL transaction/quarantine/idempotency/schema tests из матрицы.

## Integration test matrix

Каждый PostgreSQL сценарий выполняется на **16 и 18** через существующую fixture
с закреплёнными images. DDL/roles выдаёт только test admin; tests создают отдельные
inspector, writer и staging-maintenance principals. Синхронизация гонок — events/
barriers и отдельные connections, не предположение о времени через sleep.

| Файл в `tests/integration/database/` | Матрица сценариев и проверяемый результат |
|---|---|
| `test_postgresql_staging.py` | Begin/stage/seal/reopen; повтор batch; различный payload того же ordinal; crash до/после seal; чужой tenant/run; expiry/лимиты; version mismatch и отсутствующая схема без DDL; quarantine переживает target rollback. |
| `test_postgresql_dry_run.py` | Insert/upsert/FK и invalid input; никаких target/staging/ledger DML, неизменные rows и sequence state, defaults/trigger не исполняются; would counts; cleanup при EOF error/cancel; сохранённый preview не обходит recheck. |
| `test_postgresql_insert.py` | Один/несколько batches, пустой dataset, missing vs null, Decimal/UTC, generated/default поля; existing unique conflict; поздний failure → ноль committed rows всего run; bounded parameters. |
| `test_postgresql_upsert.py` | PK/composite UNIQUE/natural key; insert+update, immutable key/columns; nullable/partial/expression/deferred/invalid key veto; другой unique conflict; duplicate identity через batch boundary; запрет расширения UPDATE scope. |
| `test_postgresql_load_relations.py` | Parent в позднем batch, diamond/join table, composite pairs; source_values/mapped_parent/lookup; отсутствующий/неоднозначный parent, wrong entity ownership, MATCH/null; selected cycle отказ и внешний cycle не мешает; generated propagation отказ; concurrent parent delete → FK/rollback. |
| `test_postgresql_transactions.py` | Fault после первой таблицы/последнего batch/audit, до COMMIT и при потере ACK; cancel во время query, lock wait, rollback, commit и после commit; повторная cancel; statement/lock/deadline timeout; deadlock; connection termination; проверка освобождения locks и UNKNOWN без ложного success. |
| `test_postgresql_quarantine.py` | Валидная и невалидная группы; child failure откатывает его parent group; shared dependencies/business groups; все invalid; global veto в quarantine; savepoint восстанавливает transaction; counters и сохранённые исходные issues; replay не загружает отклонённые rows. |
| `test_postgresql_idempotency.py` | Последовательный/конкурентный одинаковый key, разные keys, другой source/plan/policy/target binding; rollback и retry, DB-generated PK, потеря ACK, crash между commit и finalize; same-key claim wait timeout; retention payload не удаляет tombstone; audit commit совпадает с target commit. |
| `test_postgresql_load_schema.py` | Drift columns/constraints/identity до запуска; ALTER/DROP/recreate между inspection и lock; DDL во время T-load ожидает либо run отклоняется; runtime RLS/trigger/partition veto; grants changed при том же catalog hash; same schema hash на другом target не разрешает запись. |
| `test_postgresql_load_security.py` | Inspector write denied; одинаковый principal отказ; writer без grants/sequence USAGE; DDL запрещён; read/write/column deny до SQL; SQL payload в value инертен, неизвестный SQL identifier отвергнут, точное странное имя безопасно quoted; secret/PII canaries отсутствуют в repr/errors/logs/audit. |

SQLite покрывает portable planning/projection и local contract behavior через
`tests/integration/test_sqlite_load_planning.py`; in-memory store и fake loader
покрывают protocols. SQLite не заменяет PostgreSQL ON CONFLICT, locks, privileges,
sequences, commit uncertainty и cancellation. SQLite writer не входит в M13.
Security unit regressions: `tests/security/loading/test_m13_load_security.py`.

Для узкого PostgreSQL файла использовать
`uv run --locked --no-sync pytest -W error::sqlalchemy.exc.SAWarning -m database_integration packages/structuraguard/tests/integration/database/test_postgresql_transactions.py`
(заменять только имя файла). Перед завершением **реализации milestone**:
`make lint`, `make typecheck`, `make test`, `make test-integration`,
`make test-security`, `make test-database`, `make docs`.
`make test` исключает database_integration и не заменяет `make test-database`.
Без Docker PostgreSQL gate считается невыполненным, не успешным по skip.

## Документация, review и границы поставки

Фактическая документация разделена на `docs/staging.md`, `docs/dry_run.md` и
`docs/loader.md`; семь Python примеров проверяются на PostgreSQL 16/18.
Руководства добавлены в MkDocs и описывают только подтверждённое поведение.
В review применять `structuraguard-review` и `structuraguard-security`: проверять
write authority, целостность данных, transaction/cancellation paths, compatibility
и матрицу выше. Для самого изменения плана достаточно `git diff --check`,
проверки ссылок/покрытия требований и `make docs`; runtime-код не меняется.

За пределами M13: append/update_only/merge, best_effort execution, DDL API,
автоматические миграции, two-phase/deferred/cyclic strategies, generated-key
propagation, COPY/bulk optimizations, неограниченные datasets, distributed stores,
background retention scheduler и полный facade ingest coordinator.

## Риски и принятые компромиссы

- **Связь M12 с записью:** готовый report не удостоверяет projection; B — обязательный
  prerequisite C/D, его нельзя заменить флагом accepted или capability ID из DTO.
- **Блокировки и права:** простой lock baseline снижает throughput и требует
  table-level UPDATE на write tables. Более тонкая concurrency strategy — отдельное
  изменение после PostgreSQL race tests, без скрытого расширения privileges.
- **Административный drift:** catalog-v1 не покрывает все runtime свойства;
  дополнительный preflight и координация migrations обязательны. Поддержку RLS,
  triggers, partitions и нестандартных типов расширять только отдельными контрактами.
- **Crash/commit uncertainty:** атомарность target+ledger требует одной БД;
  недоступность primary может оставить UNKNOWN до восстановления связи. Это
  запрещает ложный retry, но может задержать завершение run.
- **Quarantine и объём:** dependency closure может объединить весь набор в одну
  группу; при ошибке она целиком отклоняется. Конечные M12/staging budgets ограничат
  размер импорта; large-stream validation не добавляется скрыто.
- **Чувствительные данные:** persistent staging/quarantine требуют retention и
  раздельного доступа; одни fingerprints не являются redaction или authentication.
- **Предел rollback:** sequence gaps допустимы в write mode; dry-run сохраняет
  sequence state. Никаких обещаний отката внешних эффектов пользовательского кода.

## Подготовка к ручному commit и PR — 2026-09-13 {#m13-commit-readiness}

Самостоятельные API A–D готовы к отдельному PR в `main` в описанных выше границах.
**Полный M13 не закрыт:** AC-01/02/05 остаются открытыми; утверждение о готовности
полного ingest pipeline недопустимо. Сопоставление всех семи критериев и требований
A–D с наблюдаемыми тестами сохранено в [матрице приёмки](M13_acceptance.md).
Новых существенных findings при повторном review текущего diff не выявлено.
Последний Medium о потере committed result при `RuntimeError` staging закрыт:
regressions проверяют finalize, ledger recovery и отсутствие утечки backend error.

### Checklist

- [x] План, критерии и фактический standalone scope сопоставлены; незавершённые
  сценарии явно перечислены, критерии не ослаблены.
- [x] Проверены все 73 изменённых файла, включая untracked; production dependencies
  и lockfile не меняются. Обнаруженных secrets, debug artifacts и случайных
  generated files в составе diff нет.
- [x] `git diff --check` проходит; дополнительная проверка всех 73 файлов, включая
  untracked, не выявила trailing whitespace.
- [x] После последнего runtime-исправления выполнены полные доступные gates ниже;
  после подготовки плана/state повторена strict сборка документации.
- [x] Публичные API и примеры согласованы с руководствами; примеры входят в
  PostgreSQL suite, документационные тесты — в основной suite.
- [x] Обновлён [PROJECT_STATE](../codex/PROJECT_STATE.md); для непроверенных
  сценариев сохранены причины и ограничения.
- [x] Ветка `feat/m13-staging-loader`; локальные `main` и HEAD совпадают
  (`git rev-list --left-right --count main...HEAD`: `0 0`). Индекс пуст.
- [ ] Полная приёмка M13: требуется завершить AC-01/02/05 и соответствующие тесты.
- [ ] Ручные commit и PR, проверка актуального удалённого `main` и CI: этот шаг
  их не выполняет по запросу пользователя.

### Состав изменений

Текущий diff содержит 14 изменённых tracked и 59 новых файлов. В этом шаге
подготовки изменены только данный план и `docs/codex/PROJECT_STATE.md`.

| Группа | Файлов | Состав |
|---|---:|---|
| Production Python | 31 | Контракты staging/loading, stores, planning/projection/relations, PostgreSQL planner/writer/ledger, ports, закрытые ошибки и constraint semantics |
| Tests и fixtures | 26 | Unit, contract, property, security, PostgreSQL integration, проверка Markdown примеров и import probe |
| Документация | 14 | Руководства staging/dry-run/loader, ADR 0026–0029, план/приёмка/security review, PROJECT_STATE/SPEC_INDEX, обзор и public API |
| Инструменты | 2 | `mkdocs.yml`, `scripts/verify_distribution.py` |

`dist/`, `site/` и caches остаются ignored; они не входят в 73 файла. Проверка
credentials и запрещённых debug/execution constructs выполнена по содержимому
изменённых файлов; ожидаемый вывод результатов в test/build probes не является
production logging. Это проверка текущего diff, а не всей истории Git или внешних
хранилищ секретов. Stage, commit и PR не создавались.

### Фактически выполненные проверки подготовки

Среда: Python 3.12.9, macOS, PostgreSQL 16/18 в локальных Docker fixtures.
Для команд, кроме `make test-build`, использован
`UV_CACHE_DIR=/private/tmp/structuraguard-uv-cache`.
`make lock-check` дополнительно выполнен с `UV_OFFLINE=1`; `make test-build`
использует штатный cache и offline wheel/sdist verification. Полные suites
запускались после успешных узких regression tests последнего исправления
(8 PG cases, затем 144 PG и 57 локальных tests; запись в PROJECT_STATE).

| Фактическая команда | Результат |
|---|---|
| `make lint typecheck` | PASS: Ruff — 505 файлов; mypy — 501 файл |
| `make test` | 3871 passed, 388 database cases deselected; 137.29 s |
| `make test-database` | 388 passed, PostgreSQL 16/18, SQLAlchemy SAWarning как ошибка; 185.38 s |
| `make test-integration test-security` | Integration: 30 passed, 4229 deselected, 8.42 s; security: 959 passed, 32.66 s |
| `make test-build` | PASS: offline wheel/sdist, `distribution verification OK` |
| `make lock-check` | PASS: 106 packages resolved; lockfile не изменён |
| `make docs` | PASS: strict MkDocs build после правок плана/state |
| `git diff --check` | PASS; untracked дополнительно проверены по содержимому |
| `git status --short`, `git diff --name-only`, `git ls-files --others --exclude-standard`, `git diff --cached --name-only` | Проверен состав всех 73 файлов; индекс пуст |
| `git rev-list --left-right --count main...HEAD` | `0 0`; вся поставка M13 пока uncommitted |

Основной и integration suite выдают пять сторонних SWIG/PyMuPDF
`DeprecationWarning`; они не скрыты и не относятся к M13. Первый offline
lock-check внутри sandbox завершился panic macOS SystemConfiguration в `uv`;
повтор той же команды с разрешённым доступом вне sandbox прошёл. Общие тесты,
PostgreSQL и build также выполнены с разрешениями для локального watchdog,
Docker/loopback и cache. Неуспешный probe не засчитан как успешная проверка.
Первый strict docs build отклонил новый несуществующий anchor в ссылке на матрицу
приёмки; ссылка исправлена на страницу, проверка ссылок не ослаблена.
Логи текущего шага: `/private/tmp/structuraguard-m13-commit-*.log`;
в репозиторий они не добавляются. Платные LLM API не использовались.

### Residual risks и причины непроверенных сценариев

Все доступные обязательные Makefile gates выполнены. Это не заменяет отсутствующие
сценарии из исходного плана; подробности и имеющееся смежное покрытие указаны в
[таблице непроверенных сценариев](M13_acceptance.md).

| Сценарий / проверка | Причина и практическая граница |
|---|---|
| Required M12 coordinator, legacy LoadReport и успешный generated-PK insert/replay | Соответствующие API/интеграция не поставлены; AC-01/02/05 открыты. Generated-key propagation также не поддерживается |
| Пустой public dataset; diamond/join/shared-parent quarantine; nullable/MATCH FK и concurrent lookup-parent delete | Отдельных сквозных fixtures нет; graph/unit и parent/child/composite PG tests не доказывают полную матрицу |
| Deadlock/backend termination во время DML и timeout idempotency lock | Выполненные query/table lock/overall timeout и cancelled waiter tests не заменяют эти серверные fault cases |
| SIGKILL между COMMIT/finalize, реальный lost network ACK, повторная cancel после известного COMMIT | Использована driver fault injection с реальной БД; process restart и сетевой proxy не запускались |
| Конкурентные ALTER/DROP/recreate, DDL wait и все runtime RLS/trigger/partition варианты | Проверены preflight mutations и drift перед COMMIT, но не вся параллельная административная матрица; неподдержанная семантика получает veto |
| Подлинность и доступность artifacts после retention | Store хранит opaque refs; управление внешним storage остаётся у владельца. Общие SQL credentials не изолируют tenants |
| Peak memory, production throughput, PostgreSQL 15/17 | Есть bounded функциональные тесты на 16/18; profiler, benchmark и дополнительные версии не запускались |
| Другие Python/OS, удалённый `main` и GitHub CI | Текущий прогон локальный на Python 3.12.9/macOS; fetch/CI dispatch и создание PR в этот шаг не входят |

Операционные ограничения сохраняются: writer требует отдельную роль и явный
bootstrap; table locks снижают concurrency; UNKNOWN нельзя автоматически повторять
без committed evidence. Их описание находится в руководствах и ADR 0026–0029.
