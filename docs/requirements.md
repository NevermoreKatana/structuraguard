# Требования StructuraGuard SDK

Статус: нормативный baseline milestone M0. Документ задаёт целевое поведение,
но не подтверждает наличие реализации. Подтверждением требования считается
только evidence, полученное в указанном milestone.

## Нормативные обозначения

- `MUST` и `MUST NOT` обозначают обязательное требование.
- `SHOULD` обозначает рекомендуемое поведение, отклонение от которого требует
  документированного обоснования.
- `MAY` обозначает необязательное расширение.

Этот файл является владельцем истины для scope, форматов, support tiers СУБД,
режимов, NFR и критериев приёмки. Границы компонентов определены в
[архитектуре](architecture.md), угрозы и controls — в
[модели угроз](threat-model.md), а пользовательские сценарии и signatures — в
[public API](public-api.md). Основание baseline —
[план M0](plans/M00_requirements.md) и выбранные разделы
[технического задания](../StructuraGuard_SDK_Technical_Specification.md).

## Назначение и граница продукта

`PRD-001` StructuraGuard `MUST` быть встраиваемой Python-библиотекой (SDK), а
не web-service. Ядро `MUST NOT` зависеть от FastAPI, Django, Flask, Celery,
Redis, UI или transport конкретного приложения. Demo API, worker и UI могут
использовать SDK только как внешние consumers.

`PRD-002` SDK `MUST` поставляться как устанавливаемый пакет `structuraguard`
для Python 3.12+ и допускать использование в API, ETL, CLI, background task и
Notebook без обязательного application framework.

`PRD-003` SDK `MUST` преобразовывать поддерживаемый источник в проверяемый
декларативный `MappingPlan`, безопасно загружать разрешённые данные в
существующую реляционную БД и возвращать типизированные reports с provenance.

`PRD-004` Core `MUST NOT` быть связан с конкретным LLM provider, file store или
расширяемой СУБД. Интеграции подключаются через явно переданные
protocols/adapters.

## Источники и форматы

Формат содержимого и способ передачи источника являются разными контрактами.
Поддержка транспорта не означает поддержку произвольного бинарного формата.

### Входные transports

`SRC-001` Public API `MUST` принимать следующие виды источников.

| Вид | Обязательные значения |
| --- | --- |
| Файловый | path, разрешённый immutable `SourcePathPolicy` |
| Бинарный | `bytes`, `bytearray`, `BinaryIO` |
| Текстовый | `TextIO`, явно типизированный raw text |
| Объектный | `dict`, `list`, JSON body |
| Поток записей | `Iterable[dict]`, `AsyncIterable[dict]` |

`SRC-002` Строка в path-oriented API `MUST` означать path. Raw text `MUST`
передаваться через явный source type, чтобы исключить неоднозначное чтение
filesystem. Path transport разрешён только если trusted composition owner
задал immutable `SourcePathPolicy`; per-run input `MUST NOT` расширять её
`allowed_roots`. Без такой policy path отклоняется. SDK `MUST` открыть внутри
разрешённого root обычный файл без symlink traversal и затем проверять тип,
identity, limits и fingerprint по тому же открытому handle. Device, directory,
FIFO, socket и symlink `MUST` отклоняться до parsing.

`SRC-003` `inspect_source` `MUST` возвращать async-closeable `SourceAnalysis`,
связанный с bounded snapshot lease и `source_fingerprint`. Lease остаётся живой
между пошаговыми вызовами до `aclose()`, выхода из async context либо истечения
настроенного срока. `execute` с закрытым или истёкшим lease `MUST` завершаться
`SOURCE_SNAPSHOT_EXPIRED` без записи. `ingest` владеет lease внутри вызова и
освобождает его при success, failure, timeout или cancellation. Reuse
сохранённого plan требует нового inspection исходного source и повторной сверки
fingerprint; незаметное повторное чтение изменившегося path запрещено.

### Встроенные форматы

`FMT-001` Базовая поставка `MUST` поддерживать следующие форматы.

| Категория | Форматы |
| --- | --- |
| Текстовые | TXT, LOG, MD |
| Табличные | CSV, TSV, XLSX |
| Структурированные | JSON, JSONL, NDJSON, XML, YAML |
| Web-документы | HTML, XHTML |
| Документные | DOCX, PDF только с текстовым слоем |

Формат `MUST` определяться по содержимому. Расхождение signature/MIME и
расширения `MUST` порождать security event и обрабатываться policy, а не
скрываться эвристическим fallback.

### Форматы только через adapters

`FMT-002` XLS, ODS, RTF, EML, EPUB, DBF, корпоративные XML, выгрузки 1С,
специальные журналы, разрешённые архивы, Apache Tika и пользовательские форматы
`MAY` поддерживаться только через явно зарегистрированный parser adapter.
Добавление adapter `MUST NOT` требовать изменения основного pipeline.

### Исключения

`FMT-003` Базовая версия `MUST NOT` заявлять поддержку фотографий и иных
изображений, видео, аудио, OCR или распознавания рукописного текста.
Сканированный PDF без текстового слоя `MUST` завершаться контролируемой ошибкой
`PARSER_NO_TEXT_LAYER` и `MUST NOT` неявно отправляться во внешний OCR.

`FMT-004` SDK `MUST NOT` выполнять source code, SQL-файлы, JavaScript, макросы
или бинарные файлы. Архивы `MUST` быть выключены без отдельного adapter и
policy, ограничивающей суммарный размер, число файлов, вложенность, compression
ratio и пути, а также запрещающей symlinks и executable entries.

## Целевые СУБД и полномочия

`DB-001` PostgreSQL является единственным обязательным production target и
основным стендом. SQLite является только test target для unit/integration
tests; успешная проверка на SQLite `MUST NOT` считаться доказательством
production parity. MySQL, MariaDB, Microsoft SQL Server, Oracle и иные СУБД
`MAY` подключаться отдельными adapters.

`DB-002` PostgreSQL inspector `MUST` получать tables, columns, types, primary и
foreign keys, unique, nullable и check constraints; строить FK graph и stable
`database fingerprint`. Инспекция `MUST NOT` изменять данные или schema.

`DB-003` Inspection и load `MUST` использовать разные DB principals:

- `schema_inspector` имеет только доступ на чтение разрешённых metadata;
- `data_importer` имеет только необходимые `SELECT`, `INSERT` и `UPDATE` для
  allowlisted staging, target и transactional audit/outbox tables;
- `migration_admin` `MUST NOT` использоваться обычным pipeline.

System schemas и объекты denylist `MUST` быть недоступны как mapping/load
targets независимо от данных LLM или источника. Значения SQL `MUST`
передаваться параметрами, а identifiers `MUST` выбираться из отражённого и
разрешённого catalog.

`DB-004` DDL является deny-by-default. Обычный ingest и `auto_safe` `MUST NOT`
исполнять `CREATE`, `ALTER`, `DROP`, `TRUNCATE`, `GRANT` или `REVOKE`.
`propose_schema(source)` `MAY` вернуть только декларативное предложение для
ручного рассмотрения. Administrative DDL apply API не входит в MVP.

`DB-005` `MappingPlan` `MUST` быть связан с `source fingerprint`,
`database fingerprint` и immutable `target_identity`, включающим
`target_policy_fingerprint`. Trusted composition owner `MUST` заранее
зарегистрировать instance-level target policy с endpoints, dialect,
database/cluster identity, максимальным allowlist и denylist. Per-run input
`MUST` только выбирать зарегистрированный target или сужать его allowlist.
Inspection и writer используют разные principals, но `MUST` указывать на один
разрешённый database target. На writer session до staging и commit SDK `MUST`
проверить identity, policy/catalog fingerprints и отсутствие расширения policy.
Mismatch endpoint/database даёт `DATABASE_TARGET_MISMATCH`; незарегистрированный
target или расширение policy — `TARGET_NOT_ALLOWED`.

Target denylist имеет приоритет над include/allowlist. Trusted composition owner
задаёт дополнительные исключения. Обязательный PostgreSQL denylist включает
`pg_catalog`, `information_schema`, `pg_toast*`, `pg_temp_*` и SDK-owned
internal objects как mapping/load targets. Pre-provisioned staging и audit/outbox
доступны только соответствующим ports, а не source mapping. Inspector `MAY`
читать необходимые system catalogs как metadata. Per-run input `MUST NOT`
ослаблять denylist.

## Режимы и политики выполнения

Safety/orchestration policy, load error policy и dry-run являются независимыми
осями public contract.

`MODE-001` Analysis-only сценарий `MUST` читать источник и разрешённые metadata
БД, но `MUST NOT` изменять target data, schema или persistent staging. Результат
`MUST` содержать format, source profile, database catalog, candidates,
confidence, issues и security decisions.

`MODE-002` Plan-only сценарий `MUST` создавать версионируемый декларативный
`MappingPlan` без записи в target DB. Редактирование plan `MUST` создавать новую
версию, а не менять fingerprinted artifact на месте.

`MODE-003` `dry_run: bool` `MUST` быть независим от других policies. При
`dry_run=True` persistent mutations source, target tables и persistent staging
запрещены. Реализация `MAY` использовать bounded transient или rollback-only
staging для проверки DB constraints, если после success, failure, timeout и
cancellation гарантированы rollback и cleanup без остаточных data-plane
записей. Redacted audit/events являются разрешёнными control-plane side effects
и `MUST` помечать run как dry run.

`MODE-004` `safety_policy="auto_safe"` задаёт run-level gates. Успешные
detection/parsing, достаточный mapping confidence, отсутствие conflicts,
валидный `MappingPlan`, актуальные source/database fingerprints, target
identity и composition-owned policy, allowlist/denylist, DDL prohibition,
security limits и доступная обязательная audit durability `MUST` выполняться
всегда. Нарушение plan, dataset structure, security, identity или infrastructure
`MUST NOT` смягчаться `error_policy` и завершает run без записи в основные
таблицы.

`LOAD-001` Любая реальная загрузка `MUST` пройти staging, повторную проверку и
transaction. `error_policy="atomic"` является default: любая record-level или
критическая ошибка `MUST` приводить к отсутствию commit либо полному rollback
целевой транзакции. Staging и transactional audit/outbox objects `MUST` быть
заранее подготовлены deployment/admin или предоставлены adapter; обычный run
`MUST NOT` создавать или изменять их через DDL.

`LOAD-002` Loader `MUST` поддерживать `insert_only` и `upsert`, явные правила
идентификации существующей записи и idempotency. Повтор того же подтверждённого
run `MUST NOT` создавать неконтролируемые дубликаты.

`LOAD-003` После прохождения всех run-level gates
`error_policy="quarantine_invalid"` `MAY` загрузить корректные записи, сохранив
record-local ошибки values/types/business rules/constraints в изолированном
quarantine согласно retention policy. `error_policy="best_effort"` `MAY`
пропустить только явно классифицированные recoverable record-level failures и
применяться лишь при явном выборе с предупреждением о частичном результате.
Plan/dataset structural, security, source/target identity, transaction и system
failures `MUST NOT` понижаться до record-level warning. Partial success `MUST`
завершаться `COMPLETED_WITH_WARNINGS` и отражать каждый skip/quarantine.

## Pipeline и состояния

`PIPE-001` Нормативный порядок обработки:

```text
source → limits/detection → parser → normalized source/profile
       → PII scan → DB inspection/catalog/fingerprint
       → deterministic candidates → optional LLM mapping
       → MappingPlan → independent plan validation
       → normalization/record validation → staging/recheck
       → transactional load or rollback → reports/audit
```

Пропуск security, plan validation, staging или pre-commit recheck ради
convenience API `MUST NOT` допускаться.

`PIPE-002` Pipeline `MUST` использовать следующий словарь состояний:

```text
CREATED
SOURCE_PROBING
SOURCE_PARSING
SOURCE_PROFILING
DATABASE_INSPECTING
MAPPING
PLAN_VALIDATING
NORMALIZING
VALIDATING
STAGING
LOADING
COMPLETED
COMPLETED_WITH_WARNINGS
NEEDS_REVIEW
REJECTED_SECURITY
ROLLED_BACK
FAILED
CANCELLED
```

Переходы `MUST` быть явными и проверяемыми; недопустимый transition `MUST` быть
отклонён. Timeout и cancellation после начала обратимых data-plane side effects,
но до подтверждённого commit, `MUST` инициировать rollback и cleanup перед
выдачей итогового состояния.

## Mapping, LLM и результаты

`MAP-001` Candidate generation `MUST` быть детерминированным до обращения к
LLM. `MappingPlan` `MUST` содержать только декларативные ссылки на разрешённые
catalog objects и преобразования; произвольный исполняемый SQL запрещён.

`MAP-002` Независимый validator `MUST` проверить plan, permissions, types,
constraints, relationships, fingerprints, target identity/policy и security
policy до execution.
Невалидный или устаревший plan `MUST NOT` достигать loader.

`LLM-001` SDK `MUST` полностью поддерживать режим без LLM. LLM provider является
optional adapter; provider contract `MUST` позволять как минимум две
взаимозаменяемые конфигурации без изменения domain/pipeline.

`LLM-002` LLM `MAY` только ранжировать или выбирать из переданных mapping
candidates и возвращать schema-bound mapping proposal, ссылающийся на них. Она
не является источником истины, не получает tools, DB connection, credentials
или произвольный catalog access и не создаёт исполняемый SQL. Любой output LLM
считается недоверенным и `MUST` пройти deterministic schema и `MappingPlan`
validation.

`VAL-001` Validation `MUST` покрывать syntax, types, JSON Schema, DB
constraints, business rules и provenance. Результат `MUST` включать
машиночитаемый `ValidationReport`.

`DATA-001` Каждый итоговый report `MUST` позволять связать решение и загруженное
значение с source fragment. Для воспроизводимости `MUST` сохраняться версии SDK,
parser, mapping algorithm, prompt, provider/model, generation parameters,
normalizers и rules, а также source/DB/plan/target-policy fingerprints.

`DATA-002` Все встроенные parsers `MUST` преобразовывать данные в единую
типизированную normalized source model с records, fields, metadata, issues и
provenance. Формат источника `MUST NOT` менять contract downstream stages.

`REPORT-001` Run `MUST` возвращать типизированные processing, validation и
security outcomes, включая machine-readable `ValidationReport` и
`SecurityReport`. Audit `MUST` фиксировать hashes, decisions, статусы и counts,
но `MUST NOT` содержать credentials, secrets или restricted raw values.

`REPORT-002` Для реальной загрузки durable core audit/outbox record `MUST`
участвовать в той же transaction, что и target mutations, либо adapter `MUST`
предоставлять эквивалентную атомарную гарантию. Capability и доступность
проверяются до `LOADING`; иначе run завершается без data-plane mutation.
Post-commit listeners не являются частью решения о commit: их сбой сохраняет
outbox/retry evidence и `MAY` дать `COMPLETED_WITH_WARNINGS`, но не вызывает
ложный rollback. Если после подтверждённого rollback не удалось сохранить
отдельный terminal audit event, run `MUST` завершиться `FAILED`;
`LoadReport` при этом явно указывает transaction outcome `rolled_back`, а
`SecurityReport` — audit gap.

## Требования безопасности

Детальная привязка угроз к controls и residual risks находится в
[модели угроз](threat-model.md). Здесь перечислены обязательные свойства
продукта.

`SEC-001` Source content, filenames, DB metadata/values, parser/plugin output,
LLM output, aliases, declarative rules и mapping templates `MUST` считаться
недоверенными. Ни один из этих потоков `MUST NOT` получать authority выполнять
code, SQL, shell, network operation или ослаблять policy. Executable adapters и
business-rule objects, уже imported/constructed в host process, считаются
trusted code composition owner; SDK не заявляет их sandbox isolation.

`SEC-002` Parser boundary `MUST` применять content-based detection, bounded
resources и explicit failure. Parser `MUST NOT` выполнять source-directed
network access. XML external entities и DTD;
HTML JavaScript, iframe и external fetch; YAML object construction и unsafe
loader `MUST` быть запрещены. Output для HTML, template, shell и CSV/XLSX
formula contexts `MUST` экранироваться соответствующей policy.

`SEC-003` Для явно trusted development input и adapter `MAY` применяться
`InProcessParserRunner`. Любой strict/untrusted run `MUST` использовать
`SandboxParserRunner`; без него run `MUST` завершаться
`SECURITY_SANDBOX_REQUIRED`. Sandbox `MUST` быть non-root, без network, secrets
и Docker socket, с read-only filesystem, isolated temp, resource limits,
timeout и cleanup.

Источник считается untrusted по умолчанию. Trust decision задаёт trusted
composition owner до run; per-run caller `MUST NOT` повышать доверие.

Untrusted parser plugin `MUST` регистрироваться только декларативным descriptor:
разрешение artifact/module, import и constructor выполняются внутри sandbox, а
не в host process. Регистрация уже созданного parser object разрешена только
для trusted code. Untrusted executable extensions других типов отклоняются,
пока для них нет отдельного sandbox protocol; untrusted aliases, rules и
templates остаются неисполняемыми данными.

`SEC-004` Document instructions `MUST NOT` изменять system policy. Suspicious
prompt-like content `MUST` фиксироваться как security event; высокий риск
`MUST` приводить к `NEEDS_REVIEW` или `REJECTED_SECURITY`. Prompt сам по себе
`MUST NOT` считаться достаточным control.

`SEC-005` PII и secrets `MUST` классифицироваться до LLM egress как `PUBLIC`,
`INTERNAL`, `CONFIDENTIAL` или `RESTRICTED`. Raw `RESTRICTED` data `MUST NOT`
передаваться provider. Masked surrogates `MAY` передаваться только по отдельной
explicit policy. Reversible masking map `MUST` храниться отдельно, защищённо и
ограниченное retention time.
PII detection `MUST` как минимум учитывать names, email, phone, passport,
tax identifiers и payment cards; secret detection — API keys, tokens,
passwords и private keys.

`SEC-006` Errors, logs, traces, reports и audit `MUST NOT` раскрывать passwords,
DSN credentials, Authorization headers, API keys, private keys или restricted
raw values. Ошибки `MUST` содержать только safe details.

`SEC-007` SDK `MUST` применять конечные ненулевые resource limits. В strict
mode значения ниже являются hard caps и могут быть только уменьшены:

| Параметр | Default и strict cap |
| --- | ---: |
| `max_file_size_mb` | 50 |
| `max_records` | 1 000 000 |
| `max_columns` | 500 |
| `max_nested_depth` | 30 |
| `max_text_chars` | 5 000 000 |
| `max_llm_tokens` | 50 000 |
| `max_llm_calls` | 10 |
| `max_processing_seconds` | 300 |

Вне strict mode повышение `MUST` требовать явной deployment policy и оставаться
ограниченным её hard cap. Adapter-specific limits для pages, nodes, line length,
archive extraction, CPU, memory, PIDs и concurrency также `MUST` быть конечными.

## Нефункциональные требования

| ID | Нормативное требование |
| --- | --- |
| `NFR-001` | `import structuraguard` не имеет I/O или process side effects |
| `NFR-002` | Public API строго типизирован; DTO и ports имеют явные contracts |
| `NFR-003` | Canonical API async-first; sync API — отдельная facade |
| `NFR-004` | Extension points реализуются через ports/adapters |
| `NFR-005` | Dependencies instance-level; global mutable state запрещён |
| `NFR-006` | Крупные CSV, JSONL, LOG и record streams обрабатываются batches |
| `NFR-007` | Run и artifacts содержат metadata для воспроизводимости |
| `NFR-008` | SemVer; minor совместимы; removal следует deprecation |
| `NFR-009` | SDK публикует typed events; backend выбирает caller |
| `NFR-010` | Длительные операции поддерживают timeout, cancellation и cleanup |
| `NFR-011` | Streaming, batch size, concurrency и pooling остаются bounded |
| `NFR-012` | Ошибки typed и machine-readable, с `code` и safe details |

Для `NFR-001` импорт `MUST NOT` запускать server/event loop/background thread,
читать обязательные env variables, менять root logger, ставить signal handlers,
делать network/DB calls или создавать tables. Для `NFR-002` все public APIs
`MUST` иметь type hints, public DTO — Pydantic models, ports — `Protocol`.

`NFR-004` включает parser, database adapter, LLM provider, normalizer,
validator, business rule, security scanner, staging/audit store и event
listener. Для `NFR-009` минимальный event vocabulary:

```text
source.detected
source.parsed
source.profiled
database.inspected
mapping.candidates_created
mapping.created
mapping.rejected
validation.failed
staging.completed
load.started
load.completed
load.rolled_back
security.detected
```

Event consumer `MUST NOT` менять security decision. Для `NFR-011` p95 `MUST`
фиксироваться в benchmark report. Для `NFR-012` ошибка `MUST` содержать `code`,
`message`, safe `details`, `run_id`, `retryable` и redacted `cause`.

Для `NFR-010` истечение configured deadline до commit `MUST` завершать run
состоянием `FAILED` и кодом `PROCESSING_TIMEOUT`, а явная cancellation caller —
состоянием `CANCELLED`. Если transaction уже открыта, но commit не подтверждён,
rollback и cleanup предшествуют обоим terminal outcomes; unknown transaction
outcome явно отражается в report и не выдаётся за success. После подтверждённого
commit timeout/cancellation не меняет data outcome задним числом и `MUST`
отражаться как post-commit warning.

## Требования к поставке

`QA-001` До финальной приёмки `MUST` существовать unit, contract, integration и
security test suites. Security suite `MUST` включать негативные сценарии parser,
LLM, credentials, SQL/identifier injection, DDL, privileges и rollback.

`DEMO-001` Отдельный FastAPI demo в M13 `MUST` использовать публичный contract
SDK без зависимости core от FastAPI.

`DOC-001` Public API `MUST` быть документирован с inputs, results, side effects,
controlled failures, ownership, timeout/cancellation и security constraints.

`EVAL-001` Evaluation report `MUST` быть воспроизводимым из versioned dataset,
configuration и измеряемых mapping, load, LLM, performance и security metrics.

## Матрица трассировки критериев приёмки

Колонка Evidence задаёт будущий проверяемый artifact, а не текущий результат.
Исходная нумерация `AC-01`–`AC-33` сохранена без повторения полного текста ТЗ.

| AC | Требования | Future evidence | Milestone |
| --- | --- | --- | --- |
| AC-01 | PRD-002 | wheel install smoke test | M1 |
| AC-02 | NFR-001 | import side-effects test | M1 |
| AC-03 | PRD-001 | dependency boundary test | M1 |
| AC-04 | SRC-001, SRC-002, SRC-003 | source contract/lease/path matrix | M3 |
| AC-05 | FMT-001, SEC-002 | detection/parser matrix | M4 |
| AC-06 | FMT-002, NFR-004 | registry contract tests | M3 |
| AC-07 | DATA-002 | normalized-model contracts | M2/M4 |
| AC-08 | NFR-006, NFR-011 | streaming benchmark | M4/M14 |
| AC-09 | DB-001, DB-002 | PostgreSQL integration test | M5 |
| AC-10 | DB-002 | catalog constraint fixtures | M5 |
| AC-11 | DB-002 | FK graph fixture | M5 |
| AC-12 | DB-002, DB-005, NFR-007 | fingerprint repeatability test | M5 |
| AC-13 | MAP-001 | mapping golden set | M7 |
| AC-14 | MAP-001 | `MappingPlan` contract test | M7 |
| AC-15 | LLM-001 | no-LLM integration test | M7 |
| AC-16 | LLM-001 | provider contract tests ×2 | M8 |
| AC-17 | MAP-001, LLM-002 | LLM SQL security test | M8/M12 |
| AC-18 | LLM-002, SEC-006 | credential canary test | M8/M12 |
| AC-19 | MAP-002 | plan validator test suite | M9 |
| AC-20 | MODE-003 | dry-run DB diff test | M11 |
| AC-21 | LOAD-002 | load strategy integration | M11 |
| AC-22 | LOAD-001 | staging lifecycle integration | M11 |
| AC-23 | LOAD-001, LOAD-003 | forced-failure rollback test | M11 |
| AC-24 | LOAD-002 | replay/idempotency test | M11 |
| AC-25 | DATA-001 | provenance round-trip test | M10/M11 |
| AC-26 | VAL-001 | `ValidationReport` contract | M10 |
| AC-27 | REPORT-001, REPORT-002 | security/audit report contract | M12 |
| AC-28 | DB-004 | DDL denial test | M11/M12 |
| AC-29 | DB-003, DB-005 | ACL/allow/denylist/target tests | M5/M12 |
| AC-30 | QA-001 | all required quality gates | M1–M12 |
| AC-31 | DEMO-001 | demo end-to-end test | M13 |
| AC-32 | DOC-001 | public API doc, Markdown/link checks | M0 |
| AC-33 | EVAL-001 | evaluation clean rerun | M14 |

## Out of scope и deferred scope

Следующее не входит в SDK MVP:

- media/OCR и executable content, перечисленные в `FMT-003` и `FMT-004`;
- автоматическое применение DDL и administrative migration API;
- autonomous agents с tools, обучение собственной LLM и fine-tuning;
- Kubernetes, Kafka, vector database и RAG как центральный компонент;
- сложная multitenancy и полноценный SaaS billing;
- production support СУБД и форматов, обозначенных только как adapters.

FastAPI demo, worker и UI не являются частью core SDK. Demo остаётся отдельным
результатом M13. M0 создаёт только нормативные документы и не включает package,
DTO, adapters, migrations, production dependencies или executable tests.
