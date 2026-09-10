# Состояние проекта StructuraGuard

Обновлено: 2026-09-11.

После commit M7 `1027781` исправлено падение cancellation tests в присланном
CI Python 3.14.2: отменённый `asyncio.shield()` сообщал позднюю ошибку worker
в event loop. Исправлены ожидание worker и связанный PostgreSQL cleanup path;
добавлены 5 security regression cases. Публичный API и dependencies сохранены.
Изменения этого исправления остаются uncommitted; commit/push/PR не выполнялись.

Последний полный прогон: по **2474 tests** на Python 3.12.9 и 3.14.2;
на Python 3.14.2 — **18 integration**, **487 security**, **84 PostgreSQL tests**
на реальных Testcontainers 16.15/18.6. Lint и mypy прошли; offline wheel/sdist
verification прошла на штатной Python 3.12.9. Дополнительный installed-package
probe на macOS/managed Python 3.14 остановлен отсутствующей `libpython3.14.dylib`
во временном copied venv. Повтор remote CI после fix ещё не подтверждён.
[Причина, regression matrix, команды и ограничения](../plans/M07_database_inspector.md#m07-python314).
Strict docs, 2 SQLite examples, offline lock check и `git diff --check` прошли.
В пяти файлах fix не найдено secrets/generated/debug artifacts; новый password
literal — regression canary. Повторный review существенных findings не выявил.

[Историческая передача M7 до commit](../plans/M07_database_inspector.md#checklist-commit-pr)
и [проверки финального review](#m07-review-fix-checks) сохранены для трассировки.
Предыдущая проверка документации и примеров фиксируется [ниже](#m07-docs-checks).
Это подтверждение ограниченного inspection scope, без SDK orchestration/load.

Исторические проверки M3–M6 ниже сохранены для трассировки; их Git/checklist
сведения не являются текущим статусом ветки. Незавершённые сценарии M6 остаются
явными в [матрице M6](../plans/M06_acceptance.md).

## Текущая версия и milestone

- Версия package: `0.3.0`.
- Текущий milestone: `M7 — Database Inspector`; реализованы SQLite adapter и
  normalization A, PostgreSQL reflection B, canonical fingerprint/FK graph C.
  Подтверждённый scope: [API и примеры](../database-inspection.md),
  [план](../plans/M07_database_inspector.md), [матрица приёмки](../plans/M07_acceptance.md).
- `M5 — Structural Profiler и deterministic ParsePlan`:
  A (`StructuralProfiler`), B (`DeterministicStructureAnalyzer`) и C
  (`ParsePlanValidator`/`ParsePlanExecutor`) реализованы в пределах закрытой policy.
- Активная ветка: `feat/m07-database-inspector`, HEAD `1027781`
  (`feat(database): реализовать безопасный Database Inspector M7`).
  Follow-up fix Python 3.14 локальный, версия package не повышалась.
  Commit/push/PR в шаге исправления не выполнялись.
- Исторические checklist, состав diff, команды и незакрытые пункты передачи M5:
  [подготовка M5](../plans/M05_parse_plan.md#checklist-commit-pr).
- [Матрица приёмки M5](../plans/M05_acceptance.md) связывает критерии с tests
  и отдельно фиксирует неподдержанные/deferred сценарии.
- [Security review M5](../plans/M05_security_review.md): исправлены 1 High и
  2 Medium, добавлены 17 regression cases. Неисправленных подтверждённых
  Critical/High/Medium findings в проверенном diff не осталось.
- M4 Groups A–E и optional F (`Tika`, default off) остаются parser boundary.
  Остаточные ограничения [приёмки M4](../plans/M04_acceptance_audit.md) сохраняются.
  Локальные gates M5 не подтверждают remote CI или реальный Tika deployment.

Канонический scope parsing: [FR-014][spec-fr-014], [FR-015][spec-fr-015],
[M5][spec-m5] и [M6][spec-m6] в техническом задании.
Фактическая policy и ограничения: [план M5](../plans/M05_parse_plan.md),
[план M6](../plans/M06_llm_semantic_parsing.md),
[semantic parsing API с копируемым offline-примером](../semantic-parsing.md).
Канонические требования текущего milestone: [§9 — анализ целевой БД][spec-database]
и [M7 — Database Inspector][spec-m7]. Общая SDK facade и DB mapping не доступны.

## Состояние milestones

- `M0` — зафиксированы требования, архитектурный baseline и модель угроз.
- `M1` — создан устанавливаемый typed package с проверяемым scaffold.
- `M2` — реализация завершена: добавлены immutable domain contracts и protocols
  двухэтапного parsing.
- `M3` — реализация завершена: добавлены instance-local parser registry,
  deterministic selection и descriptor-only opt-in plugin discovery.
- `M4` — в работе: реализованы Groups A (`TXT`, `LOG`, `MD`), B (`CSV`, `TSV`)
  C (`JSON`, `JSONL`, `NDJSON`), D (`XML`, `HTML`, `YAML`) и E (`XLSX`, `PDF`, `DOCX`).
  Optional F (`Tika`) реализована вне default factories.
- `M5` — реализованы A/B/C; подтверждён только scope, описанный в API и ADR 0008–0010.
- `M6` — реализованы A–D в bounded scope: provider contract suite и fake/no-LLM,
  HTTP adapter и отдельный policy-aware router; strict LLM ParsePlan proposal;
  Hybrid analyzer, document chunks/grounded entities и `SemanticParsingSession`.
  Три режима, default `llm_assisted`; full source validation/execution, zero-call
  deterministic/saved-plan paths, preview/review/report и usage проверены tests.
  [Provider API](../llm.md), [LLM analyzer](../llm-semantic-parsing.md),
  [Hybrid API](../semantic-parsing.md); решения ADR 0011–0014.
  Production PII scanner/redaction, безопасный aggregate report целиком,
  analyzer registry и router/session facade отсутствуют. K5/K7 исходной приёмки
  остаются частичными; DB mapping вне scope M6.
  Security review исправил 3 Medium: provider exception leaks, token/context caps
  и повторное открытие session budget после failed/cancelled analysis.
  Последующий финальный review выявил ещё 1 High и 2 Medium: снятие review при
  saved document replay, отсутствие общего deadline при его проверке и overflow
  terminal issues. Все три исправлены с 12 regression cases.
  Незакрытых подтверждённых Critical/High/Medium в проверенном diff не осталось;
  ограничения и непроверенные сценарии сохранены в отчёте review.
- `M7` — A/B/C реализованы и проверены в documented scope. Отдельные read-only
  adapters возвращают metadata snapshot либо catalog-v1 с hash/FK graph;
  SQLite не имитирует PostgreSQL-specific behavior. Security review исправил
  2 Medium: raw exception context и потерю непредставимых schema semantics.
  Финальный review исправил ещё 2 Medium: потерю domain CHECK metadata и
  зависимость fingerprint от физических пропусков после DROP COLUMN.
  Ограничения и непроверенные угрозы: [review M7](../plans/M07_security_review.md).
- `M8`–`M17` — не начаты.

## Подтверждённое поведение M7

- `structuraguard.database` экспортирует `SQLiteTarget`, `PostgreSQLTarget`,
  `InspectionLimits` и два concrete adapters. `inspect_metadata` возвращает
  `DatabaseMetadataSnapshot` schema 1.1.0, `inspect` — `DatabaseCatalog` schema
  1.1.0 с `fingerprint_version="catalog-v1"`, `database_fingerprint` и graph.
- Scope/policy проверяются до I/O; deny имеет приоритет. Нет DDL/DML, sampling,
  SELECT пользовательских строк, исполнения metadata expressions или LLM calls.
  Соединения отдельные, закрываются до результата; limits/timeout/cleanup failures
  не публикуют partial catalog. Credentials/driver context не включаются в errors.
- Native/canonical types, nullable/default, PK/FK/unique/checks/indexes и
  generated/non-writable columns отражены. PG поддерживает enum/domain/array,
  identity и comments. SQLite явно имеет `comments_supported=False`;
  `write_permissions="unknown"`, `writable` не доказывает grants.
- Domain CHECK names/comments/validity отражены в optional `domain_constraints`
  и fingerprint; прежний `domain_checks` сохранён. Колонки нумеруются с нуля по
  видимому порядку: история DROP COLUMN не влияет на hash и разрешение PK/FK.
- Pure domain functions строят versioned canonical projection и SHA-256,
  повторно проверяют drift и строят FK graph. Hash не зависит от reflection order,
  target/credentials/producer/данных. SCC/self-FK дают diagnostics и отсутствующий
  load order; join hints требуют полного структурного evidence.
- Legacy 1.0.0 JSON сохранён; pure functions требуют metadata schema 1.1.0.
  DTO не пересчитывает объявленный hash. Нужны fresh inspection и явный
  `verify_database_fingerprint`; это не проверка прав или защита TOCTOU.
- Неподдержанные column selectors, внешние FK, nullable SQLite PK, virtual tables,
  SQLite `DEFERRABLE`/`ON CONFLICT`/column `COLLATE`, PG EXCLUDE/temporal constraints,
  foreign tables и нестандартная column/domain collation дают typed отказ.
  View SQL definition не входит в fingerprint. Graph не выбирает стратегию
  циклической загрузки; `execute` всегда даёт `SDK_OPERATION_NOT_IMPLEMENTED`.
- SDK orchestration, MappingPlan validation/load, staging, dry-run и audit sink
  этим milestone не реализованы. Native fuzzing/OS memory isolation, TLS/MITM,
  PostgreSQL 15/17 и remote CI не подтверждены локальными tests.

## Исправления финального review M7 {#m07-review-fix-checks}

Два Medium finding закрыты без расширения milestone scope. Изменены только
catalog DTO, scoped PostgreSQL domain query/normalization и canonical type
projection, добавлены regression tests и уточнена документация. Дополнительный
`DatabaseType.domain_constraints` использует существующий DTO constraints;
legacy JSON без поля сохраняется, прежние SQLite golden hashes не менялись.
Catalog с domain metadata или историей DROP COLUMN требует свежего inspection
и повторной проверки сохранённого fingerprint, автоматической замены binding нет.
Подробная [матрица regressions](../plans/M07_acceptance.md#m07-final-review-fixes).

| Команда / проверка | Фактический результат |
| --- | --- |
| `pytest -q --tb=short -m database_integration .../test_m07_review_regressions.py` до исправления | **8 failed** на ожидаемых сравнениях hashes, PostgreSQL 16/18 |
| Тот же новый файл после исправления, с `-W error::sqlalchemy.exc.SAWarning` и security cases | **14 passed** |
| `pytest -q .../unit/database .../unit/contracts/test_database_catalog.py .../security/database` | **216 passed** |
| `make lint typecheck` | Ruff **267 файлов**, mypy **265**, ошибок нет |
| `make test` | **2469 passed**, 84 DB cases deselected, 5 прежних PyMuPDF/SWIG warnings |
| `make test-integration` | **18 passed**, 2535 deselected, те же 5 warnings |
| `make test-security` | **482 passed** |
| `make test-database` | **84 passed**, реальные PostgreSQL 16.15/18.6, без skips и SQLAlchemy warnings |
| `pytest -q .../unit/database/test_domain_constraints.py .../docs/test_m07_examples.py` | **19 passed**, включая обратное чтение domain DTO без нового поля и SQLite examples |
| `make docs` | Strict build, ссылки/anchors — passed |
| `make test-build` | wheel/sdist, offline rebuild/import/examples — **distribution verification OK** |
| `git diff --check` | Passed |

В таблице `.../` означает `packages/structuraguard/tests/`, новый PostgreSQL файл
расположен в `integration/database/`. Применены те же локальные uv cache и Docker
настройки, что в docs-прогоне ниже. DDL выполняют только test admin fixtures;
inspection по-прежнему использует отдельного пользователя без SELECT на rows.
Реальные платные LLM API не вызывались.

Повторный `structuraguard-review` и security review нового diff не выявили
существенных findings: projection сохраняет новые свойства, sorting не меняет
пары composite keys, row/text/byte budgets и redaction распространяются на domain
comments. Существующие tests и quality gates не ослаблялись, dependencies и
архитектурные границы не менялись. PostgreSQL 15/17, remote CI, полный advisory
scan и native fuzzing в этой работе не проверялись.

## Проверка документации M7 {#m07-docs-checks}

Публичные docstring adapters/targets, catalog DTO и pure domain API описаны
по-русски: параметры, результат, ошибки, I/O и security ограничения. Guide
содержит SQLite metadata/full-catalog примеры, PostgreSQL пример с отдельным
inspector, migration note и ссылки на канонический ТЗ. Нового архитектурного
решения не принималось; действуют ADR 0015–0017.

| Команда / проверка текущего docs-шага | Фактический результат |
| --- | --- |
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/docs/test_m07_examples.py` | **2 passed**: SQLite metadata и полный каталог, сеть запрещена |
| `uv run --locked --no-sync pytest -q -m database_integration packages/structuraguard/tests/integration/database/test_m07_documented_postgresql_example.py` | **2 passed**: исходный Markdown-пример на PostgreSQL 16.15/18.6 |
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/docs` | **111 passed** |
| Узкие `unit/database`, `unit/contracts/test_database_catalog.py`, `security/database` | **199 passed** |
| `make lint typecheck` | Ruff: 265 файлов; mypy: 263 source files; ошибок нет |
| `make docs` | Strict build, внутренние ссылки/anchors — passed |
| Сравнение AST семи изменённых runtime-модулей без docstring | Исполняемая логика не изменена |
| `git diff --check` | Passed |

Первый docs build обнаружил два неверных локальных anchor; ссылки исправлены,
strict checks сохранены. Первый PostgreSQL example run не нашёл Docker socket;
после запуска установленного Docker Desktop оба случая прошли, без skips.
Testcontainers использовал отдельного inspector и существующие admin fixtures;
DSN передавался в дочерний пример через stdin. Paid LLM API не вызывались.

Локальная среда: Python 3.12.9, SQLite 3.49.1, SQLAlchemy 2.0.52, asyncpg 0.31.0,
Testcontainers 4.15.0. Использован `UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache`;
для Docker — `DOCKER_CONFIG=/private/tmp/structuraguard-docker-public` и
`DOCKER_HOST=unix:///Users/katana/.docker/run/docker.sock`.
Новый PostgreSQL example test входит в `make test-database`; обычный docs suite
не требует Docker. Внешний link checker не настроен; remote CI не запускался
этим docs-шагом. Новый полный runtime suite не запускался: логика не менялась,
результаты предыдущего полного M7 run приведены в начале страницы.

## Реализованные публичные contracts

- Корневой `structuraguard.__all__` сохраняет ровно 12 lazy exports M1; facade
  по-прежнему fail-loud и не изображает реализованный pipeline.
- `structuraguard.contracts` экспортирует frozen DTO physical `Extracted*`,
  discriminated `ParsePlan`, semantic `Normalized*`, `DatabaseCatalog`,
  target-bound `MappingPlan`, checked-plan wrappers, reports и audit events.
- `structuraguard.ports` экспортирует десять protocols: `Parser`, `StructuralProfiler`,
  `SemanticStructureAnalyzer`, `ParsePlanValidator`, `ParsePlanExecutor`,
  `DatabaseAdapter`, `LLMProvider`, `SecurityScanner`, `StagingStore` и
  `AuditStore`.
- M7 добавляет `DatabaseMetadataSnapshot`, descriptors типов/columns/indexes и
  constraints, `DatabaseDependencyGraph`, SCC/FK evidence и join candidates.
  Эти DTO не дают DB authority и не подключаются к БД при конструировании.
- `Parser` возвращает только raw physical structure; semantic values возникают
  только после применения `ValidatedParsePlan`. DB execution принимает только
  `ValidatedMappingPlan`, связанный с catalog/target/policy fingerprints.
- `structuraguard.parsers` экспортирует registry/snapshot/session и
  discovery contracts. Facades принимают `parser_registry` через dependency
  injection и возвращают его через `parsers`; default registry instance-local.
- `structuraguard.parsers.builtin` экспортирует независимые adapters
  `PlainTextParser`, `LogParser`, `MarkdownParser`, узкий CSV/TSV family adapter
  `DelimitedTextParser`, `JsonDocumentParser`, `JsonLinesParser`,
  `XmlParser`, `HtmlParser`, `YamlParser`, `XlsxParser`, `PdfParser`, `DocxParser`.
  Регистрация явная; bounded extraction, provenance и deterministic observations
  не создают semantic schema.
- `structuraguard.parsers.tika` экспортирует отдельный выключенный по умолчанию
  `TikaParserAdapter` с config/limits/source-bound egress approval. Core ranking
  не изменён; начальный non-core scope — RTF/PostScript, response-relative XHTML.
- `DelimitedTextParser` поддерживает immutable dialect candidates/override,
  fail-closed ambiguity, streaming table segments и raw cells. Header остаётся
  только candidate metadata; missing ragged coordinates, explicit empty cells и
  blank rows различаются без business normalization.
- JSON adapters сохраняют ordered object members/array items, nested collections,
  duplicate keys и raw scalar values в physical trees с JSON Pointer provenance.
  JSONL/NDJSON records читаются и выдаются потоково без destructive flatten.
- Manual trusted parsers регистрируются явно. Selection использует strong
  content evidence, `confidence`, `priority` и canonical ID; MIME/extension
  conflicts становятся warning либо `PARSER_FORMAT_CONFLICT`.
- Entry points группы `structuraguard.parsers` обнаруживаются только явным
  вызовом с allowlist. Discovery не вызывает `EntryPoint.load()` и отражает
  ошибки отдельных distributions без прекращения обработки остальных.
- Public DTO используют strict frozen Pydantic contracts, tuple collections,
  tagged scalars, UTC datetime, `Decimal` для money и обязательную provenance.
  Persisted SHA-256 имеет единственную форму `sha256:<64 lowercase hex>`.
- Persisted aggregates/reports содержат schema и producer versions. LLM/security
  payload связан typed `SecurityApproval`, bounded canonical JSON и не принимает
  tools, credentials, handles, shell или SQL authority. `ValidationIssue`
  сохраняет `code`/`message_key` без free-form text; `LoadReport` именует target
  и всю execution fingerprint chain.

`structuraguard.structure` экспортирует profiler, deterministic analyzer,
validator/executor, LLMStructureAnalyzer и immutable options. M5 сохраняет unresolved
semantics; M6-C предлагает bounded semantic names/type/locale hints после source
validation. Hybrid/session, chunk extraction/span execution и report доступны через
[semantic parsing API](../semantic-parsing.md). DB reflection/load, production PII
scanner/redaction, staging/audit backends, state machine и общая ingest/router facade не реализованы.

M6-C API и limits: [LLM semantic parsing](../llm-semantic-parsing.md),
[ADR 0013](../adr/0013-validated-llm-structure-analysis.md). Обязательны trusted scanner,
реальный ParsePlanValidator и два полных replay. Один invocation вызывает LLM
максимум один раз; неоднозначность, low score и неполный scope дают NEEDS_REVIEW.
DB mapping не добавлялся.

## Подтверждённое поведение M5

- Bounded профиль schema 1.1.0: observations четырёх семейств, field hints,
  evidence/confidence, coverage и несколько кандидатов. Raw source целиком
  не удерживается. Пустой source даёт пустой профиль.
- Analyzer строит один plan при достаточном confidence и полном покрытии,
  ranked alternatives при неоднозначности либо NEEDS_SEMANTIC_ANALYSIS.
  Confidence — minimum boundary/regularity/coverage, default threshold 0.85.
  Другие modes явно отклоняются; LLM/network fallback отсутствует.
- Validator требует полный physical replay; executor повторно проверяет
  source/wrapper/context, сохраняет raw values, origins и selection trace.
  Schema 1.1.0 normalized hashes перепроверяются публичными validation methods.
- Record budget применяется инкрементально, в том числе к child collections.
  Source exceptions очищаются с сохранением известных codes и parser/security
  категорий. Cancellation и partial errors не становятся успешным terminal.
- Output до terminal manifest предварителен и требует downstream staging/rollback.
  Fingerprints подтверждают согласованность, но не подлинность источника.
  Profiles и normalized values могут содержать PII; это не безопасные logs.

Не реализованы semantic conversions, identity/отдельные ParseRule, executable
regex, автоматический выбор ambiguous candidates, replay store и facade wiring.
Sampling gaps запрещают automatic plan. XML element matching, optional tree
fields и неподтверждённые multi-scope regions требуют отдельной policy.
Подробные ограничения и test mapping: [приёмка](../plans/M05_acceptance.md).

## Проверки M5

После security fixes: узкие M5/DTO suites — `758 passed`, полный `make test` —
`1832 passed`, integration — `16 passed`, security — `319 passed`.
Ruff, strict mypy, strict MkDocs и `git diff --check` прошли. Команды и evidence:
[security report](../plans/M05_security_review.md).

Документация M5 добавляет [исполняемый офлайн-пример](../structure.md) и проверку
русских public docstrings в `tests/docs/test_m05_examples.py` (11 новых cases).
Новые tests запущены первыми, затем весь documentation suite и quality gates.
Все команды ниже выполнены с
`UV_CACHE_DIR=/private/tmp/structuraguard-m05-uv-cache`:

| Команда | Фактический результат после обновления документации |
| --- | --- |
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/docs/test_m05_examples.py` | `11 passed`, 0.41 s |
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/docs` | `56 passed`, 1.46 s; пример M5 проверяет raw values, provenance и unresolved semantics с запретом сети |
| `make lint typecheck docs` | Ruff: 173 файла, без замечаний; strict mypy: 171 файл, без ошибок; MkDocs strict: exit 0, внутренние ссылки/anchors проверены |
| `make test test-integration test-security` | `1843 passed` (62.11 s); `16 passed / 1827 deselected` (3.46 s); `319 passed` (15.13 s); exit 0 |
| `git diff --check` | exit 0 |

В production-коде этого документационного изменения обновлены только docstrings;
сравнение AST 20 файлов structure/contracts это подтверждает. Новых архитектурных
решений и dependencies нет; ADR 0008–0010 связаны с текущим scope без нового ADR.
После записи результатов MkDocs strict запущен повторно. Канонические anchors
FR-014, FR-012.2 и M5 сверены с локальным ТЗ; внешний сетевой link checker не настроен.
Сохранены пять upstream SWIG warnings полного/integration прогона.
Большой RSS/stress corpus, другая ОС и remote CI в этой работе не запускались.

После финального review исправлены ещё три finding в рамках прежнего scope:

| Finding | Исправление и regression test |
| --- | --- |
| High: document blocks пропадали при наличии physical lines | Исключаются только blocks с подтверждённым совпадением диапазона и полного текста lines, включая окончания строк. Независимые blocks остаются кандидатами: `test_independent_blocks_remain_candidates_beside_physical_lines`; multiline LOG и прежний Markdown error contract сохранены |
| Medium: sparse CSV накапливал пустые строки в runtime index | Lookup не вставляет отсутствующую строку. `test_sparse_rows_do_not_accumulate_outside_selected_scope` проверяет validator/executor: индекс содержит 3 непустые строки при диапазоне 5 000 строк |
| Medium: двойной знак приводил к `decimal.InvalidOperation` | Primitive hint допускает не более одного знака. `test_invalid_signed_value_does_not_crash_footer_analysis` и property `test_numeric_hint_is_always_a_valid_finite_decimal` проверяют безопасный отказ от числовой гипотезы; валидные signed totals сохранены |

Добавлены 20 regression cases и одна property. До исправлений новые cases дали
`11 failed / 9 passed`; после исправлений узкие M5 suites — `184 passed` (12.92 s).
Публичный API, утверждённый scope и production dependencies не изменены.
Повторный review исправлений, включая security, существенных findings не выявил.

| Команда после исправлений review | Фактический результат |
| --- | --- |
| `make lint typecheck` | Ruff: 175 файлов без замечаний; strict mypy: 173 файла без ошибок |
| `make test` | `1864 passed`, 5 upstream SWIG warnings, 62.73 s |
| `make test-integration` | `16 passed / 1848 deselected`, 5 upstream SWIG warnings, 3.55 s |
| `make test-security` | `321 passed`, 15.68 s |

### Подготовка ручного commit и PR M5

Локальный `main` и `HEAD` совпадают (`dfed5ce`); новых commits нет. Diff содержит
64 файла: 17 modified и 47 new, включая все модули M5. Staging/push/PR не выполнялись.
Сканирование всех файлов diff не обнаружило secrets, debugger imports или
случайных generated files. `dist/`, `site/` и caches игнорируются Git; property
`_hashseed_probe.py` и вывод проверочного CLI являются штатными test tools.

При проверке обнаружен и исправлен Medium packaging blocker:
`scripts/verify_distribution.py` отклонял 18 новых модулей как неожиданные файлы,
не учитывал новый port и не импортировал публичный `structure` в isolated smoke.
Allowlist расширен явным перечнем M5; пять новых cases проверяют inventory,
exports и отказ для посторонних файлов. До исправления — `3 failed / 7 passed`;
после него полный packaging suite — `26 passed`. Runtime API и dependencies
этот шаг не меняет. Повторный review обновлённого gate существенных findings
не выявил; проверки неизвестных файлов и optional imports сохранены.

Свежие gates на macOS/Python 3.12.9:

| Команда | Результат |
| --- | --- |
| Узкие M5 suites | `184 passed` |
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/docs` | `56 passed`, offline example включён |
| `make lock-check` | exit 0, 100 packages resolved |
| `make lint typecheck` | Ruff 175 файлов и strict mypy 173 файла без ошибок |
| `make test test-integration test-security` | `1869 passed` (62.95 s); `16 passed / 1853 deselected` (3.48 s); `321 passed` (15.62 s) |
| `make test-build` | `distribution verification OK`: wheel/sdist, byte-for-byte offline rebuild, isolated base import с M5 |
| `make docs`, `git diff --check` | exit 0 |

Для offline rebuild использован существующий `UV_CACHE_DIR=/Users/katana/.cache/uv`:
временный cache не содержал Hatchling 1.32.0. Uv/SystemConfiguration и document
watchdog checks повторены вне sandbox после системных отказов, без отключения
проверок. Пять upstream SWIG warnings полного/integration suite не подавлены.

Полная автоматизация исходного C3 и deferred operations ADR 0010 не заявлены
готовыми. Remote CI/другие окружения, большой RSS corpus, all-extras matrix,
реальный Tika и внешние audit/link services не проверялись; причина каждого
пропуска указана в [плане M5](../plans/M05_parse_plan.md#residual-risks).

## Известные ограничения M3

Lineage-aware summaries и pure stream validation позволяют обнаруживать foreign
non-terminal batches, несовпадающие counts и глобальные duplicate normalized IDs
без реализации parser/executor adapter. `ExtractedSourceIndex` намеренно bounded:
он перечисляет только разрешённые sample/evidence/selectors, а не копирует каждый
physical объект большого источника.

Discovery валидирует только metadata allowlisted distributions и не загружает
plugin code. Filesystem metadata читаются через bounded FD-reader; unsafe path,
oversized файл, ZIP/custom provider и платформа без безопасного `dir_fd`
отклоняются. Host metadata finder и подмена artifact после discovery остаются
границами доверия до isolated runtime M12. Исполнение untrusted descriptor до
M12 запрещено: `activate_plugin()` завершается `SECURITY_SANDBOX_REQUIRED`.

Публичный parser contract возвращает базовый `AsyncIterator[ExtractedBatch]`.
Если trusted adapter владеет внешним ресурсом, его custom iterator должен сам
предоставить корректный `aclose()`; без close-hook SDK может перевести wrapper
в quarantined state, но не может принудительно освободить ресурс adapter.
Обязательный close-capable protocol требует отдельного изменения публичного
contract.

## Quality gates M3

- Итоговый `make check` — успешно; включает все перечисленные ниже локальные
  gates.
- `make sync` и lock check — успешно.
- `make lint` — Ruff для 73 файлов, успешно.
- `make typecheck` — strict mypy для 71 source files, успешно.
- `make test` — `830 passed` на Python 3.12.
- `make docs` — MkDocs strict, успешно.
- `make test-build` — собраны и проверены
  `structuraguard-0.3.0.tar.gz` и
  `structuraguard-0.3.0-py3-none-any.whl`; wheel повторно собран из sdist,
  установлен изолированно и прошёл black-box/attribution import probes.
- `git diff --check`, secret/debug scan и audit untracked/generated artifacts —
  успешно; build outputs и caches исключены через `.gitignore`.

## Проверки M4-B

- Узкие unit, contract, property-based и security tests CSV/TSV — успешно.
- Проверены Unicode, custom delimiter/quote/escape, quoted newlines, ragged и
  blank rows, batch/chunk boundaries, malformed diagnostics, limits,
  cancellation и inert formula-like values.
- `DelimitedTextParser` не меняет `csv.field_size_limit` или locale. Runtime
  dependencies Group B не добавляет; Polars отсутствует.
- Итоговый `make check` успешен: Ruff, strict mypy для 91 source files,
  `1134 passed`, MkDocs strict и distribution verification wheel/sdist.

## Проверки M4-C

- Contract, unit, property-based и security tests JSON/JSONL/NDJSON — успешно.
- Проверены Unicode и nested structures, duplicate keys, exact number lexemes,
  RFC 6901 provenance, short reads, batch continuation без дубликатов/пропусков,
  strict syntax/UTF-8, malformed line coordinates, limits, cancellation и inert
  embedded content.
- Review regressions подтверждают, что слабый JSON-подобный TXT не прерывает
  selection, а trailing blank JSONL lines не создают пустой terminal batch.
- Итоговый `make check` успешен: Ruff для 100 файлов, strict mypy для 98 source
  files, `1228 passed`, MkDocs strict и distribution verification wheel/sdist.

## Открытые блокеры

Известных архитектурных блокеров внутри реализованного scope M4 Groups A–C нет.
Remote CI не запускался: он сможет независимо подтвердить результат после
публикации изменений в ветку/PR.

Cell provenance CSV/TSV публично фиксирует zero-based row/column. Exact lexical
byte span quoted token пока не входит в schema `1.1.0`; malformed diagnostics
содержат bounded one-based record/physical-line coordinates без raw source.
Плановое расширение lexical/source-span provenance отложено до отдельного schema
decision.

В расширенном плане Group A остаются deferred caller-provided LOG regex с
timeout backend и общий processing deadline. Hypothesis property suite для A
добавлен в acceptance audit. Deferred capabilities не
входят в реализованный fixed-adapter scope и не подменяются небезопасными
fallback; соответствующее поведение остаётся недоступным до отдельной задачи.

## Проверки M4-D

- Новые contract, boundary, property и security tests — `82 passed` в составе
  полного прогона. Проверены XXE/DTD/Billion Laughs, unsafe YAML tags/aliases,
  inert HTML/XSS, Unicode, provenance, cancellation и streaming boundaries.
- Итоговый `make check` — успешно: Ruff для 108 файлов, strict mypy для 106
  source files, `1310 passed`, MkDocs strict, сборка и изолированная проверка
  wheel/sdist. Core-only import не загружает `defusedxml`/`yaml`.
- `git diff --check` — успешно. Отдельных targets `test-integration` и
  `test-security` пока нет; security suites включены в `make test`.
- Review/security review завершены. Ограничения source-event HTML DOM,
  XML infoset, YAML marks и in-process limits описаны в ADR 0005.

## Проверки M4-E

Group E: независимые lazy document adapters, read-only hardened OOXML, raw
formula/cached values, physical table/cell/run/page provenance, bounded subprocess
и ZIP guards. Новые fixture/property/security/integration tests охватывают
Unicode, границы batches, XXE/ZIP/PDF actions, timeout/cancellation и cleanup.
Targets `make test-integration` и `make test-security` добавлены; их suites
также входят в `make test`.

- `make check` — успешно: Ruff для 118 файлов, strict mypy для 116 файлов,
  `1380 passed`, strict MkDocs, wheel/sdist и isolated core-only import.
- Добавлено 70 тестов E: contract/boundary, property, security и real-backend
  integration; PDF no-text, encrypted/embedded files, ZIP/XXE/actions и lifecycle.
- Review/security review завершены; существенных неисправленных findings в
  реализованном slice нет. Ограничения: Linux/macOS worker без sandbox M12,
  sampled RSS на macOS, bounded parts/pages, backend layout и PDF licensing.
- PyMuPDF fixture-writing API выдаёт пять upstream SWIG deprecation warnings;
  warnings не подавляются, все tests проходят.

## Проверки M4-F

Optional `TikaParserAdapter` использует отдельный extra HTTPX/defusedxml, явный
endpoint и source-bound PUBLIC approval после secret review caller. Upload
выполняется только после bounded snapshot/SHA/canary preflight; default factories,
специализированные adapters, DB/LLM/semantic analyzer не изменены.

- Добавлено 74 теста: contract/boundary RTF/PostScript, Unicode/chunk/batch
  properties, 47 security regressions и loopback fake HTTP integration.
- `make check` — успешно: Ruff для 125 файлов, strict mypy для 123 файлов,
  `1454 passed`, strict MkDocs, wheel/sdist и isolated import без HTTPX/defusedxml.
- `make test-integration` — `6 passed`; `make test-security` — `190 passed`.
- Review regression закрепляет `Accept: text/xml`: XML serializer сервера,
  а не HTML serialization, проверяется contract-тестами; HTML response отклоняется.
- Review/security review завершены. Network/container isolation и реальная DLP
  принадлежат caller; известные credential markers не покрывают обфускацию.
  XHTML provenance response-relative; фактический Tika/JVM не запускался.
- Сохраняются пять upstream PyMuPDF SWIG deprecation warnings; тесты проходят,
  warnings не подавляются.

## Аудит приёмки M4

Добавлено 96 cases в шести test files: aggregate content selection 13 форматов
в двух порядках регистрации, semantic boundary, strict/frozen limit overrides,
точные cells/blocks/pages boundaries, empty OOXML, properties A/PDF и worker/Tika
security regressions. Исправлены только четыре выявленных дефекта: YAML ownership
NDJSON, преждевременный decode ZIP в textual probes, потеря limit metadata и
классификация worker crash/transport/backend defect как malformed input.

- Узкие parser suites — `556 passed`.
- `make check` — успешно: Ruff 131 files, strict mypy 129 files,
  `1550 passed`, strict MkDocs, wheel/sdist и isolated base verification.
- `make test-integration` — `9 passed`; `make test-security` — `199 passed`.
- Review/security review и `git diff --check` — успешно; существующие tests и
  gates не ослаблялись. LLM/DB/semantic analyzer и dependencies не изменялись.
- Не подтверждены полный measured peak corpus, все runtime N±1 limits,
  отдельный all-extras environment и реальный Tika deployment/isolation.
  Причины и матрица каждого AC: [отчёт](../plans/M04_acceptance_audit.md).
- Пять upstream PyMuPDF SWIG warnings сохранены; внешние LLM/Tika не вызывались.

## Security review текущего M4 diff

Исправлены две подтверждённые проблемы: High — response headers/reason/protocol
errors попадали в upstream HTTP logs; Medium — format allowlist можно было обойти
сменой bytes между Tika probe и spooling при совпадающем конечном SHA.
Защита локальна Tika transport, без глобальных logger mutations и изменений
DB/LLM/semantic analyzer. Optional extras закрепляют уже установленный httpcore
1.0.9 для проверенного порядка trace callback.

- Добавлены восемь security regression cases и один packaging pin test.
- Узкие suites — `92 passed`; Ruff — 132 files, mypy — 130 files, без ошибок.
- `make check test-integration test-security` — успешно: весь pytest (`1559 passed`),
  strict docs, wheel/sdist и isolated base; integration — `11 passed`, security —
  `207 passed`. Пять существующих PyMuPDF SWIG warnings не подавлены.
- OSV query для десяти проверенных parser/HTTP package versions не вернул
  advisories; это не full SBOM/native audit и не гарантия отсутствия zero-days.
- Severity, exploit paths, точные locations/tests, dependency evidence и
  остаточные риски: [security review M4](../plans/M04_security_review.md).
  Ограничения полной приёмки AC-06/07/11/12 сохраняются.

## Документация подтверждённого scope M4

- [Копируемый офлайн-пример](../public-api.md#m4-extraction-copyable-example)
  показывает явную регистрацию TXT adapter, bounded context, raw lines с
  provenance и проверку terminal manifest. Сбор list ограничен малым примером;
  для больших sources описаны streaming и предварительный статус batches до EOF.
- Tika configuration example принимает endpoint, заявленную deployment version
  и внешний egress approval явно. Он не выполняет upload и не выдаёт DLP-допуск.
  Версия сервера не аттестуется клиентом.
- Публичные parser docstrings на русском описывают параметры, результат,
  исключения и security boundaries. Runtime-логика и dependencies не изменены.
- Новые docs checks: `tests/docs/test_m04_examples.py` — `37 passed`:
  оба примера выполняются в subprocess с запрещённой сетью; проверяется наличие
  русских docstrings у публичных builtin/Tika exports и их методов/properties.
- Все documentation tests — `45 passed`. `make lint typecheck docs` и
  `make check test-integration test-security` — успешно: Ruff 133 files,
  strict mypy 131 files, полный pytest, strict MkDocs, wheel/sdist и isolated base
  verification; integration — `11 passed`, security — `207 passed`.
  Пять прежних PyMuPDF SWIG warnings не подавлены; внешние LLM/Tika не вызывались.
- Главная страница отражает доступность M4, архитектурный baseline ссылается на
  существующие ADR и подтверждённый scope. Review документационного diff не
  выявил новых существенных проблем; `git diff --check` проходит.
- Ссылки ведут на канонические M4/NFR-006, без копирования ТЗ. Нового
  долгоживущего решения нет; используются ADR 0004–0007.
- Частичный статус AC-06/07/11/12, отсутствие общего A–D deadline, M12 sandbox,
  Windows document worker, OCR и успешного ingest не скрыты документацией.

## Исправления findings финального review M4

Закрыты четыре finding без изменения публичных signatures, dependencies,
DB/LLM/semantic scope:

- High: DOCX сохраняет `noBreakHyphen`/`softHyphen` с run provenance;
  неподдерживаемые run elements и table/row/cell containers отклоняются typed
  `PARSER_UNSUPPORTED_FEATURE`, без успешного manifest с потерянным текстом.
- Medium: JSON probe проверяет принадлежность decoded prefix JSON до передачи
  encoding error; UTF-16/UTF-32/legacy TXT не блокируется чужим adapter. Сам JSON
  parse по-прежнему принимает только strict UTF-8.
- Medium: YAML probe использует первую значимую строку и уступает mixed-root
  JSONL; двоеточия в последующих CSV cells не делают источник YAML. Unsafe tags
  и malformed подтверждённого YAML сохраняют typed отказ.
- Medium: выбранная кодировка входит в options fingerprint и extraction identity
  группы A. Повтор с теми же options стабилен; разные декодирования различимы.

Regression evidence: `tests/unit/parsers/builtin/test_m04_review_regressions.py`
и `tests/security/parsers/test_m04_probe_regressions.py` — `34 passed` после
воспроизведения дефектов. Узкие parser unit/property/security suites —
`598 passed`; `make lint typecheck` — успешно (135 / 133 files).
Повторный review локального diff потребовал сохранить ownership YAML `%TAG`:
обход typed отказа unsafe tag воспроизведён и закрыт отдельным regression test.
После исправления новых существенных findings в локальном diff не найдено.

Окончательный `make check test-integration test-security` завершился успешно:
lock, lint, typecheck, полный pytest, strict MkDocs, wheel/sdist и isolated
distribution verification; отдельно integration — `11 passed`, security —
`216 passed`. В integration остаются пять backend deprecation warnings SWIG.
Общие незакрытые AC-06/07/11/12 остаются вне этих локальных исправлений.

## Подготовка ручного commit и Draft PR M4

Дата: 2026-09-09. Обновлены только план M4, пояснение статусов в acceptance audit
и этот файл; source/tests/dependencies в этой подготовке не изменялись.
Полный checklist, команды и причины пропусков:
[передача M4](../plans/M04_technical_parsers.md#m04-manual-handoff).

- Свежий `make check test-integration test-security` — exit 0: Ruff 135 файлов,
  strict mypy 133 файла, pytest `1630 passed, 5 warnings`, strict docs,
  wheel/sdist/rebuild и isolated base verification; integration `11 passed`,
  security `216 passed`.
- `.venv/bin/pytest -q packages/structuraguard/tests/docs` — `45 passed`.
- `make docs` после checklist edits — успешно: выявленный неверный anchor
  исправлен на явный `m04-manual-handoff`, повтор strict build прошёл.
- `git diff --check` и whitespace check каждого untracked файла — без ошибок.
- Проверены все 94 commit-кандидата (23 modified, 71 untracked), включая малые
  текстовые OOXML fixtures. Build/docs outputs и caches игнорируются.
  Secrets/debug artifacts не обнаружены локальным сигнатурным поиском и
  просмотром; canary fixtures и worker JSON transport не удалялись.
- Отдельные secret scanners не установлены; реальные Tika/LLM, remote CI,
  all-extras install, полный resource/native corpus и другие OS не проверялись.
  Причины и release/version gap указаны в плане; приёмка остаётся частичной.
- Повторный review handoff diff не добавляет новых существенных findings;
  незакрытые AC и ограничения не скрыты. Staging area пустая; ручные git/PR
  действия остаются за пользователем.

## Исправление HTML compatibility CI 2026-09-10

На базе `c5abec6` устранена зависимость error contract от HTML5 dispatch stdlib:
marked sections явно направляются в прежний parser, неизвестное имя сохраняет
`PARSER_MALFORMED_INPUT` с line provenance. Inert text не проверяется как markup.
Публичный API, dependencies, версии package и CI matrix не менялись.

Исходный тест не ослаблен. Новый `test_html_compatibility.py` содержит 39
regression cases; до fix на настоящем Python 3.14.2 получено `25 failed,
14 passed`, после — `99 passed` вместе с прежним markup suite.
Такие же 99 tests проходят на 3.12.9/3.12.12/3.13.11.

- Полный pytest на Python 3.12.12, 3.13.11 и 3.14.2: по `1669 passed, 5 warnings`.
- `make lock-check lint typecheck docs test-build test-integration test-security`:
  exit 0; Ruff 136 файлов, mypy 134 файла, docs/package verification успешны,
  integration `11 passed`, security `216 passed`.
- Литерал malformed HTML в новых docs экранирован после обнаруженного падения
  preprocessing; strict docs build повторён успешно, checks не отключались.
- Parser/security review fix не выявил новых существенных findings; конкретные
  команды и ограничения: [дополнение плана M4](../plans/M04_technical_parsers.md#m04-html-ci-fix).

Результаты локальны для macOS с isolated locked environments, рабочая `.venv`
не заменялась. Повтор remote Linux CI остаётся за новым запуском после передачи
fix; агент commit/push/PR не выполнял. Открытые AC и release gate M4 сохранены.

## Принятые архитектурные решения

- [ADR 0001](../adr/0001-public-api-and-run-policies.md) — async-first API,
  отдельный sync facade и run policies.
- [ADR 0002](../adr/0002-security-boundary-defaults.md) — fail-closed security
  defaults и запрет secrets в errors/audit.
- [ADR 0003](../adr/0003-two-stage-parsing-contracts.md) — technical parsing,
  semantic `ParsePlan`, checked execution и отдельный DB `MappingPlan`.
- [ADR 0004](../adr/0004-lossless-physical-extraction.md) — lossless physical
  extraction, bounded provenance и parser schema 1.1.
- [ADR 0005](../adr/0005-safe-markup-extraction.md) — независимые safe markup
  adapters, optional backends, physical provenance и ограничения библиотек.
- [ADR 0006](../adr/0006-bounded-document-adapters.md) — bounded XLSX/PDF/DOCX
  worker, read-only OOXML fidelity, POSIX limits и граница sandbox M12.
- [ADR 0007](../adr/0007-opt-in-tika-egress.md) — opt-in Tika HTTP fallback,
  source-bound secret approval, response-relative XHTML provenance и caller isolation.
- [ADR 0008](../adr/0008-bounded-structural-profiling.md) — bounded sampling,
  profile coverage и evidence без потери неоднозначности.
- [ADR 0009](../adr/0009-deterministic-structure-analysis.md) — deterministic
  scoring, закрытая grammar и unresolved semantics.
- [ADR 0010](../adr/0010-verified-parse-plan-execution.md) — physical replay,
  недоверенный wrapper, provenance и terminal success после cleanup.
- [ADR 0011](../adr/0011-llm-provider-foundation.md) — provider-neutral contract,
  scripted fake, deterministic-only provider и safe metadata.
- [ADR 0012](../adr/0012-policy-aware-llm-routing.md) — явный HTTP lifecycle,
  trusted schema registry, policy routing и общий budget.
- [ADR 0013](../adr/0013-validated-llm-structure-analysis.md) — закрытый proposal
  вместо executable model output, source aliases и обязательный M5 validator.
- [ADR 0014](../adr/0014-hybrid-semantic-parsing.md) — три режима, bounded document
  spans, deterministic merge/execution, confidence и preview/report semantics.
- [ADR 0015](../adr/0015-bounded-sqlite-inspection.md) — bounded SQLite reflection,
  read-only file connection и закрытый catalog scope.
- [ADR 0016](../adr/0016-scoped-postgresql-inspection.md) — scoped SQLAlchemy Core
  queries PostgreSQL вместо broad reflection, отдельная read-only transaction.
- [ADR 0017](../adr/0017-canonical-catalog-and-dependency-graph.md) — явная
  projection catalog-v1, version gate, pure fingerprint и FK/SCC algorithms.

## Следующий рекомендуемый шаг

Перед передачей текущего M7 diff проверить [план](../plans/M07_database_inspector.md),
[матрицу приёмки](../plans/M07_acceptance.md) и [security review](../plans/M07_security_review.md).
После публикации ветки независимо подтвердить remote CI, включая отдельный
PostgreSQL job. Локальная проверка не подтверждает прочие OS/DB versions.
Commit/push/PR этой работой не выполняются. Следующие capabilities SDK требуют
отдельных задач; наличие каталога и graph не означает готовность mapping/load.

Production PII, analyzer registry, aggregate report и router/session composition
M6 остаются за пределами M7. Исторический [checklist M6](../plans/M06_llm_semantic_parsing.md#checklist-commit-pr)
сохранён; ограничения parser backends и Tika — в [приёмке M4](../plans/M04_acceptance_audit.md).

[spec-m4]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m4-technical-parsers
[spec-fr-014]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#fr-014-structural-profiling-и-parseplan
[spec-m5]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m5-structural-profiler-и-deterministic-parseplan
[spec-fr-015]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#fr-015-llm-assisted-semantic-parsing
[spec-m6]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m6-llm-assisted-semantic-parsing
[spec-database]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#9-анализ-целевой-базы-данных
[spec-m7]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m7-database-inspector

## Проверка документации M6 {#m06-docs-checks}

Обновлены русские public docstring providers/router, LLM/Hybrid analyzers,
session, document factories и contracts. Руководства M6, public API, главная
страница и архитектура согласованы с bounded runtime; offline CSV example
перенесён в начало semantic parsing guide. Новых архитектурных решений нет:
используются ADR 0011–0014 и ссылки на канонические разделы ТЗ.

- `uv run --locked --no-sync pytest -q packages/structuraguard/tests/docs/test_m06_examples.py`:
  **46 passed**, включая public docstrings и три примера с запретом socket events.
- `uv run --locked --no-sync pytest -q packages/structuraguard/tests/docs`:
  **109 passed**; полная доступная проверка примеров документации.
- `uv run --locked --no-sync pytest -q packages/structuraguard/tests/docs
  packages/structuraguard/tests/unit/llm
  packages/structuraguard/tests/unit/structure/test_document_flow.py
  packages/structuraguard/tests/unit/structure/test_m06_document_acceptance.py`:
  **197 passed** (4.51 s), включая fake HTTP и PDF/DOCX source provenance.
- `make lint typecheck docs test-build`: **exit 0**; Ruff — 224 файла,
  mypy — 222 файла, strict MkDocs со ссылками/anchors, offline wheel/sdist
  rebuild/install и примеры из установленного core без HTTP extra успешны.
- `git diff --check`: без замечаний.

Первый расширенный docstring test нашёл четыре отсутствующих описания validator
methods; они добавлены. Strict docs обнаружил два неверных anchors; ссылки исправлены.
Первый document regression запуск внутри sandbox дал 195 passed и два отказа
memory watchdog; разрешённый локальный повтор с доступом к `/bin/ps` дал 197 passed.
Финальные команды с packaging/document backends выполнены с `UV_OFFLINE=1` и
`UV_CACHE_DIR=/Users/katana/.cache/uv`; внешние LLM не вызывались. Live endpoints,
semantic quality реальной модели и внешний HTTP link checker не проверялись;
MkDocs проверяет локальные links/anchors, ссылки на ТЗ сверены с local headings.

## Исправления финального review M6 {#m06-review-fix-checks}

Исправлены только три findings: сохранение `NEEDS_REVIEW` для saved document
draft, общий deadline saved-plan validation и bounded terminal report при
заполненном issue budget. Изменены `structure/hybrid.py`, `parsing/session.py`,
добавлен `tests/security/structure/test_m06_final_review.py`; уточнены docstring,
semantic parsing guide и план. Публичный API, wire schemas и dependencies прежние.
Матрица finding → regression test: [план M6](../plans/M06_llm_semantic_parsing.md#m06-final-review-fixes).

Проверки выполнены сначала на новых cases, затем на затронутых suites и gates:

- `uv run --locked --no-sync pytest -q packages/structuraguard/tests/security/structure/test_m06_final_review.py`:
  **12 passed** (1.93 s), controlled clocks/events/IDs и fake providers.
- `uv run --locked --no-sync pytest -q packages/structuraguard/tests/unit/structure
  packages/structuraguard/tests/security/structure packages/structuraguard/tests/property/structure
  packages/structuraguard/tests/docs/test_m06_examples.py`: **351 passed** (22.88 s).
- `make lint typecheck`: Ruff — **225 файлов**, strict mypy — **223 файла**, без ошибок.
- `make test`: **2219 passed** (82.84 s).
- `make test-integration`: **18 passed / 2201 deselected** (5.09 s).
- `make test-security`: **433 passed** (21.19 s).
- `make docs lock-check test-build`: **exit 0**; strict MkDocs, lockfile
  (**100 packages**) и offline wheel/sdist build/install/examples успешны.
- `git diff --check`: без замечаний.

Для полного запуска использованы `UV_OFFLINE=1`,
`UV_CACHE_DIR=/Users/katana/.cache/uv`, `PYTEST_ADDOPTS=-q` и локальное разрешение
для parser watchdog/packaging. Реальные LLM API не вызывались. Пять предупреждений
full/integration suite относятся к существующим SWIG types PDF backend.
Первая strict docs сборка обнаружила отсутствующий anchor этого раздела;
добавлен фактический отчёт для целевой ссылки, повторная сборка прошла.

Повторные `structuraguard-review` и `structuraguard-security` нового fix diff
не выявили других существенных findings. Точные исходные blockers не входят в
saved plan: positive penalty консервативно сохраняет review всего доступного
document scope; manual approval API не вводился. K5/K7 остаются частичными;
production PII, безопасный aggregate report, analyzer registry и router/session
composition не входят в эти исправления. Live provider/TLS/proxy, полный внешний
CVE/SCA audit и нагрузочные проверки максимальных объёмов не выполнялись.

## Исторические проверки M6-C {#m06-c-checks}

После review fixes полноты scope и cancellation/deadline выполнены:

- Узкий analyzer/security/docs набор: `73 passed`.
- `make lint typecheck`: Ruff — 201 файл, без замечаний; strict mypy — 199 файлов,
  без ошибок.
- `make test`: `2086 passed`, 78.95 s.
- `make test-integration`: `16 passed / 2070 deselected`, 4.40 s.
- `make test-security`: `387 passed`, 18.99 s.
- `make docs lock-check test-build`: strict MkDocs, lockfile и isolated wheel/sdist
  verification успешно. `git diff --check` — успешно.

Запуск: `UV_OFFLINE=1 UV_CACHE_DIR=/Users/katana/.cache/uv PYTEST_ADDOPTS=-q make
lint typecheck test test-integration test-security docs lock-check test-build`.
Сеть для LLM не использовалась; scanner/fake clocks/HTTP transport управляются tests.
Пять warnings полного набора относятся к SWIG types PDF dependency.
Review и residual scope: [план M6](../plans/M06_llm_semantic_parsing.md#review-m6-c).
