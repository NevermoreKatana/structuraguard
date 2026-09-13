# M15 — SDK orchestrator: source → report

Статус: bounded scope M15 реализован, проверен и подготовлен к ручному commit
и последующему Pull Request в `main`. Дата: 2026-09-13.
Итоговый [checklist и фактические проверки](#m15-commit-readiness) приведены ниже.
Последующее задание расширило scope до реализации, включая `IngestResult`.
Фактический API и ограничения: [руководство](../sdk-orchestrator.md),
архитектурное решение: [ADR 0034](../adr/0034-sdk-orchestrator.md).

## Цель

Связать существующие M3–M14 components в пошаговый и одношаговый async SDK
с одинаковыми gates, воспроизводимыми результатами и sync wrappers, без новых
parser/DB/LLM/storage adapters и без расширения исполняемой grammar планов.

### Требования и наблюдаемое поведение

Из ТЗ изучены только §6 «Режимы работы SDK», §7 «Полный pipeline», §23 «Публичный
API SDK», §24 «Результат работы», §25 «Статусы pipeline» и §33 «Критерии приёмки».
Для §23 прочитан весь диапазон до §24: extraction script ошибочно принимает
комментарии внутри Python-примера за Markdown-заголовки и обрезает раздел.
Разделы требований зарегистрированы в [SPEC_INDEX](../codex/SPEC_INDEX.md).

До M15 `packages/structuraguard/src/structuraguard/sdk.py` содержал
заглушки `inspect_source`, `inspect_database`, `create_plan`, `validate_plan`,
`execute`, `analyze`, `ingest`, `propose_schema`; остальные запрошенные методы
отсутствовали. `packages/structuraguard/src/structuraguard/sync_sdk.py`
делегировал заглушкам через `asyncio.run` и уже запрещал вызов внутри event loop.
`SDKConfig` был пуст, registry по умолчанию пустой и instance-local. Конструкторы
не делали I/O, не запускали discovery, threads или event loop.

После M15 поддержанный источник проходит один и тот же coordinator через
пошаговые вызовы или `ingest`. Ни successful proposal, ни fingerprint, ни
промежуточный batch не дают разрешения на запись. Неподдержанный сценарий
завершается явным typed outcome до запрещённого side effect.

Основания решений: [ADR 0001](../adr/0001-public-api-and-run-policies.md),
[0010](../adr/0010-verified-parse-plan-execution.md),
[0012](../adr/0012-policy-aware-llm-routing.md),
[0014](../adr/0014-hybrid-semantic-parsing.md),
[0020](../adr/0020-llm-semantic-mapping-proposals.md),
[0026](../adr/0026-staging-and-loader-transactions.md),
[0027](../adr/0027-read-only-dry-run-planning.md) и
[0031](../adr/0031-security-audit-and-parser-boundaries.md).

## Критерии приёмки

1. Все 12 групп методов ниже имеют строгие типы; async и sync дают эквивалентные
   результаты. Существующие constructor arguments и identity registry сохранены.
2. Поддержанный bounded источник проходит весь stage flow; каждый переход
   проверяется. Анализ и планирование не вызывают staging writer/target writer.
3. ParsePlan и MappingPlan независимо проверяются при создании execution и при
   reuse. Нет target DML до EOF обоих datasets и завершения обязательных checks.
4. `deterministic` и `no_llm` дают ноль model calls. `llm_assisted` и `llm_first`
   сохраняют существующую семантику M6; review/preview не превращается в успех.
   Табличный поток не вызывает LLM для каждой строки.
5. Dry-run проходит parsing, profiling, mapping и record validation; target,
   sequences и persistent staging остаются неизменными. Прогнозы отделены от
   фактических counts, `loaded_records=0`.
6. PostgreSQL load использует существующие staging, read-only inspector,
   отдельного writer, allowlist/denylist, проверку schema/grants, transaction,
   rollback и ledger. Повтор того же idempotency binding не делает повторный DML.
7. Отмена, timeout, review, security rejection, известный rollback, неизвестный
   commit и post-commit warning различаются; cleanup не пропускается.
8. Отчёты содержат только реально полученные artifacts/evidence. Hooks и audit
   не получают raw values, prompts, DSN или текст внешних exceptions.
9. Ограничения ниже проверяются отрицательными тестами. Приёмка M15 не закрывает
   автоматически все пункты §33: source transports, большой streaming load,
   generated PK, demo и evaluation не появляются от соединения компонентов.

## Затронутые контракты

Все пути компонентов далее относительно `packages/structuraguard/src/structuraguard/`.
Пути `tests/...` относятся к `packages/structuraguard/`; `docs/...` — к корню repo.
Contracts, их wire versions и существующие ports переиспользуются. Новая работа —
facade signatures, runtime ownership, перевод результатов между существующими
DTO, state machine и composition root. `contracts`/`domain` не импортируют pipeline
или infrastructure; новые production dependencies, schema migrations и runtime DDL
не нужны.

### Что действительно реализовано

| Стадия | Существующий API | Обязательная граница |
|---|---|---|
| Source gate | `SecuritySession.snapshot(SourceStream, kind, expected_size)`, `guarded_reader`, `BoundedSnapshot` | `SourceStream` принадлежит host; snapshot целиком в памяти, но ограничен до накопления |
| Detection / technical parse | `ParserRegistry.session().select(SourceArtifact, ProbeContext)`, `SelectedParser.parse`, `ValidatedParserStream`; `InProcessParserRunner` / `SandboxParserRunner` | MIME/extension только hints; frozen registry, validated output и terminal manifest |
| Structure / semantic parse | `StructuralProfiler.profile`, `DeterministicStructureAnalyzer.analyze`, `SemanticParsingSession.analyze_structure/create_parse_plan/validate_parse_plan/parse_semantically` | Replay одного extraction; `HybridAnalysis` может содержать draft и review issues |
| ParsePlan gates | `ParsePlanValidator.validate_source`, `ParsePlanExecutor.execute`, `ParseExecutionContext` | Полный physical replay, одинаковые options, проверка refs и lineage |
| Normalized profile | `NormalizedDataProfiler.profile(..., context=...)` | Полный `NormalizedDatasetManifest`, bounded statistics и PII classification |
| Database inspection | `PostgreSQLDatabaseAdapter.inspect`, `SQLiteDatabaseAdapter.inspect`, `DatabaseInspectionRequest` | Trusted targets, read-only и scope; их `execute` остаётся заглушкой |
| Mapping | `DeterministicMapper.rank`, `LLMSemanticMapper.propose`, `MappingScope`, `DatabaseSemanticCatalog` | Возвращают candidates/proposal, **не MappingPlan** и не разрешение на загрузку |
| Mapping validation | `MappingPlanValidator.validate` | Возвращает `MappingPlanValidationResult` **либо** `MappingPlanInputReport`; обработать обе ветви |
| Normalization / validation | `NormalizerRegistrySnapshot.normalize_value`, `JsonSchemaValidator.validate`, `DatabaseConstraintValidator.validate`, `BusinessRuleValidator.validate`, `ProvenanceValidator.validate`, `ValidationReportBuilder.combine` | Caller готовит projection и связывает все результаты; каждый validator подтверждает только свой scope |
| Staging | `RunStagingStore.begin/stage/seal/read_*/transition/cleanup`, memory/PostgreSQL implementations | References-only: store не хранит и не восстанавливает payload |
| Dry-run / load | `PostgreSQLDryRunPlanner.plan(DryRunRequest)`, `PostgreSQLLoader.execute(LoadRequest)` | Выходы `DryRunExecutionPlan` / `PostgreSQLLoadResult`; единый `DatabaseAdapter.execute` не использовать |
| Security / audit | `SecuritySession`, `ContentProtector`, `InjectionAwareSecurityScanner`, `AuditChain`, `AuditSigner`, `AuditChainStore` | Scanner имеет обязательный trusted base scanner; signed target audit принадлежит loader |

### Разрывы и предел обязательного scope

- Runtime `SourceAnalysis` и `NormalizedData` сохраняют явный lease; новый
  `IngestResult` schema 1.0.0 компонует существующие component reports. Это
  единственное расширение wire contracts по прямому требованию последующего
  задания. Отдельные `SourceReport`/`DatabaseReport` не выдумываются: используются
  `SourceArtifact`, `ProbeResult`, manifest и `DatabaseCatalog`.
- Нет реализованного source-path gate/`SourcePathPolicy`, общего transport для
  `path/bytes/dict/list/iterable`, durable replay store или spill на диск.
  Обязательный вход M15 — уже доступный host `SourceStream` через M14 snapshot;
  допустим также явно переданный fingerprint-bound reader с перепроверкой.
  Facade не открывает path/URL и не сериализует произвольные Python objects.
  Unsupported inputs дают typed отказ. Внутренний `structure.text_sources.TextSource`
  не является публичным raw-text transport из ADR 0001.
- Replay и M12/M13 требуют bounded immutable snapshots: `DryRunRequest.batches`
  — tuple; provenance требует physical и normalized tuples. Нужен контроль суммы
  bytes/items всех удерживаемых artifacts, а не только размера одного batch.
  Использовать минимальный совместимый budget компонентов; превышение — отказ,
  без скрытого spill. Обработка parser batches не доказывает O(batch) память
  всего ingest; критерий §33.9 этим планом полностью не закрывается.
- Assembly `MappingPlan` — новая чистая orchestration-функция над существующими
  `FieldMapping`, `MappingIdentity`, `MappingRelation`. Нельзя просто взять top-1:
  проверять ambiguity, collisions, обязательные поля, полный FK и identity.
  Однозначный поддержанный выбор создаёт draft; остальные возвращают существующий
  deterministic/semantic результат для review или принимают caller-supplied plan.
  Новых ranking heuristics и inference generated keys здесь нет.
- M13 `loading/projection.prepare` принимает только copy values: transformations,
  issues или `raw_value != normalized_value` дают `DRY_RUN_PROVENANCE_UNVERIFIED`.
  M15 вызывает M12, но не обходит этот gate и не переписывает manifests после
  conversion. Изменяющая значения normalization остаётся доступна отдельному
  компоненту; в ingest такой результат требует review и блокирует load.
  Derived batches/rebinding — отдельное расширение contracts, не часть M15.
- `best_effort` есть в enum, но `DryRunPolicy`/loader поддерживают только `atomic`
  и явный `quarantine_invalid`. `best_effort` отклоняется, не переводится в другой
  режим. SQLite используется для inspection, не как новый production loader.
- Persistent staging требует реального владельца retained artifacts. Без него
  нельзя выдать фиктивные `StagingArtifactReference.retained_until`. Memory composition
  держит artifacts сама в пределах run; persistent composition получает действительные
  references от host. Создание durable artifact adapter не входит в M15.

### Public facade: входы, результаты и обязанности

Знак «+» ниже означает типизированную композицию имеющихся объектов, а не новый
domain DTO. Перед реализацией закрепить точные tuple/type aliases в facade и
contract tests. Runtime source context владеет lease/replay и не сериализуется.

| Метод | Вход → результат | Поведение |
|---|---|---|
| `inspect_source` | `SourceStream` + trusted source metadata → `SourceArtifact` + `ProbeResult` + `ExtractedDatasetManifest` в runtime source context | Security gate, bounded snapshot, detection, technical extraction до EOF; никакого LLM/DB |
| `analyze_structure` | source context → `HybridAnalysis` | `SemanticParsingSession.analyze_structure`; внутри уже есть profiling и deterministic/LLM analysis. Кэшировать в одной session, не вызывать analyzer повторно |
| `create_parse_plan` | тот же source context + результат анализа → `ParsePlan | None` | Достать draft через ту же session; при `None` причины/assessment остаются в analysis и run outcome |
| `validate_parse_plan` | source context + `ParsePlan` → `ParsePlanValidationResult` | `validate_parse_plan` session / M5 full replay; не вызывать generation, не доверять сохранённому wrapper |
| `parse_semantically` | source context + `ParsePlan` → bounded `NormalizedBatch` sequence + `SemanticParseReport` | Потребить session iterator до terminal и закрыть; preview без terminal не передавать в profiling/mapping/load |
| `inspect_database` | trusted target / bound inspector + `DatabaseInspectionRequest` → `DatabaseCatalog` | Read-only inspection; target ID/policy не берутся из недоверенного plan |
| `profile_records` | полная normalized sequence + `NormalizedProfileContext` → `NormalizedDataProfile` | Сверить manifest/EOF, classification и budgets; сохранить snapshot для последующих стадий |
| `create_mapping_plan` | normalized manifest/profile + catalog + scope → `MappingPlan | None` + existing candidate/proposal result | M9; M10 только при разрешённой неоднозначности; чистая assembly. Нет SQL, записи или implicit approval |
| `validate_mapping_plan` | plan + manifest + catalog + optional profile → `MappingPlanValidationResult | MappingPlanInputReport` | Независимый M11; `validated_plan` появляется только при принятой проверке |
| `execute` | полный normalized snapshot + mapping plan + target + physical replay/evidence + policies → status + существующие reports/outcomes | Повтор M11 и M12, staging, dry-run/load. Внешний report не заменяет physical replay; нет auto-remapping |
| `ingest` | поддержанный source + target + явные policies + optional saved plans → та же композиция reports/outcomes | Весь flow одной session/run; самостоятельно закрывает созданные ресурсы. Staged API и ingest используют одинаковые функции |
| sync wrappers | Те же конечные входы/результаты для всех методов | Один async engine; проверка running loop до coroutine creation, `SYNC_API_IN_ASYNC_CONTEXT`, без фонового thread |

`analyze`, `create_plan`, `validate_plan` сохранить как совместимые convenience
entry points: `analyze` заканчивается анализом/candidates, `create_plan` делегирует
созданию DB mapping после semantic parsing, `validate_plan` — M11. Они не получают
прав на запись. `propose_schema` остаётся явной заглушкой вне M15, DDL запрещён.
По [ADR 0001](../adr/0001-public-api-and-run-policies.md) не переносить неоднозначный
`mode="auto_safe"/"atomic"` из примеров: `safety_policy`, `error_policy`, `dry_run`
— отдельные axes. LLM configuration и constructor parameters расширять только
явными keyword arguments, не читать environment. Для M15 единственный вариант
safety policy — `auto_safe`; никакого force/override hard gates. Error policy
передаётся через существующие M13 policy DTO.

## Шаги

Это исходная последовательность реализации: имена предполагаемых test-файлов
и команды «Проверка» в шагах описывают план, а не журнал выполненных команд.
Фактическое размещение tests отражено в [матрице приёмки](M15_acceptance.md),
последние результаты — в [checklist передачи](#m15-commit-readiness).

### 1. Composition root и ownership

Новый `pipeline/composition.py` собирает существующие components и immutable
политики; `sdk.py` только делегирует. В `pipeline/session.py` — run-local UUID,
`SecuritySession`, deadline, текущий статус, fingerprints, bounded artifacts,
semantic session, candidate results, reports и cleanup ownership.
Это runtime composition, не новый service framework или parallel protocol layer.
Здесь же создать минимальный каркас `pipeline/state.py` и зафиксировать effective
policies из шага 6; последующие вертикальные шаги сразу работают под этими gates.
Шаг 5 завершает terminal branches и наблюдаемость, а не добавляет безопасность
задним числом после появления loader.

- Trusted host задаёт registry, source transport, inspector/writer targets,
  staging, scanner, provider(s), signer/keys, audit store, aliases и rules.
  Отсутствующая обязательная dependency отклоняет соответствующую операцию до I/O.
  Не подставлять allow-all scanner, random signer, temporary DB или другой provider.
- Оставить пустой default parser registry и `.parsers` identity. Explicit builtin
  registration выполняется до session; snapshot/freeze защищает от изменений run.
  Аналогично фиксируются normalizer registry, rules/aliases и policies.
- Компонентам передаётся один общий `SecuritySession`; локальные limits только
  сужаются. DB adapters получают `resources`; retries не сбрасывают counters.
- Runtime source context выполняет ADR 0001: `aclose`, context exit, конечный
  срок lease; закрытый/истёкший context отклоняет staged calls. Raw snapshot
  освобождается вместе с последним owned replay. Host stream/borrowed provider
  закрывает host; facade не обещает закрыть объект с отсутствующим close port.
- В одной session запрещены конкурентные операции и повтор execution. Разные
  runs изолированы, включая budgets и hook sequence. Нет глобального state.

**Тест сначала:** расширить `tests/unit/test_facades.py`, добавить
`tests/unit/pipeline/test_composition.py`: no I/O/discovery при construction,
изоляция runs, missing dependency, narrowing policy, закрытый lease.
**Проверка:** `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/test_facades.py packages/structuraguard/tests/unit/pipeline/test_composition.py`.

### 2. Source → semantic dataset

Файлы: `pipeline/source.py`, `pipeline/parsing.py`, `pipeline/session.py`, `sdk.py`.
Сначала `SecuritySession.snapshot` и safe metadata, затем guarded probe/parse;
terminal extraction фиксируется один раз. Replay возвращает те же batches/IDs,
а не повторно читает изменившийся transport. Держать physical snapshot в общем
budget M15; downstream не потребляет one-shot iterator повторно.

`SemanticParsingSession` обеспечивает analysis cache, ParsePlan validation и
execution. Coordinator делает stage transitions вокруг этих API, не дублирует
алгоритмы M5/M6. Если внутренний composite call уже исполнил validation, не
выдавать event за отдельный ещё не выполненный stage: фиксировать evidence после
возврата и выполнять публичный независимый validation перед execution.

Security gates до parser и каждого egress: resource limits; trusted parser или
sandbox admission; content classification; injection observation; exact payload
approval; проверка ответа/ParsePlan. `ContentProtector` не изменяет fingerprint-bound
physical source. Для M6 нет готового универсального wiring redaction→request:
использовать уже поддержанный exact payload approval и разрешённый deployment;
если policy требует отсутствующего преобразования payload — отказ до egress.
Не менять samples после выпуска approval и не выдавать fingerprint за доказательство
redaction. Strict sandbox без предоставленного host backend — отказ, не in-process.

**Тест сначала:** `tests/unit/pipeline/test_source_flow.py`,
`test_semantic_flow.py`, `tests/security/pipeline/test_source_gates.py`:
malformed/oversized input, MIME conflict, late parser failure, missing terminal,
replay mismatch, lease expiry, saved plan, zero LLM на повторяющейся таблице,
review preview, no-egress при policy deny.
**Проверка:** `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/pipeline/test_source_flow.py packages/structuraguard/tests/unit/pipeline/test_semantic_flow.py packages/structuraguard/tests/security/pipeline/test_source_gates.py packages/structuraguard/tests/unit/structure/test_semantic_session.py packages/structuraguard/tests/unit/structure/test_hybrid.py`.

### 3. Profile → catalog → MappingPlan

Файлы: `pipeline/mapping.py`, `pipeline/session.py`, `sdk.py`.
В `ingest` normalized profile предшествует inspection. Пошагово независимый
`inspect_database` допустим раньше, как в §23, но M11/loader всегда получают
актуальные bindings. Schema metadata тоже недоверенна и проходит privacy gates.

M9 сохраняет top-k и blockers. M10 вызывается только по явной LLM policy и при
недостаточном deterministic evidence. Его `COMPLETED/action=auto` относится
к proposal. Assembly использует только проверенные candidate IDs, supported
identity/FK и операции caller policy; минимальная confidence не снимает veto.
Caller-supplied MappingPlan идёт сразу в M11 без generation и автоматического
исправления. Все malformed-input и review ветви M11 сохраняются.

**Тест сначала:** `tests/unit/pipeline/test_mapping_flow.py`:
unique mapping, collision/tie, missing required field, composite identity/FK,
no candidates, M10 confirm/reject, M11 input report, чужой target/policy/manifest.
**Проверка:** `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/pipeline/test_mapping_flow.py packages/structuraguard/tests/unit/mapping/test_m11_validation.py packages/structuraguard/tests/unit/mapping/test_semantic_mapper.py`.

### 4. Record gates → staging → dry-run/load

Файлы: `pipeline/validation.py`, `pipeline/loading.py`, `sdk.py`.
`execute` принимает полный immutable normalized snapshot и доказательства его
происхождения; bare normalized iterator или accepted чужой report недостаточны.

1. Повторить M11 по actual manifest/catalog/policy. Сформировать детерминированную
   target projection через существующую `loading/projection.prepare`, сохранив
   record/entity/value IDs для перевода issues; не писать второй projection engine.
2. Применить явно заданные M12 normalizers, JSON Schema, DB constraints, business
   rules и provenance replay. Required layers задаёт trusted composition; явно
   неприменимый optional schema/rules stage не изображать как выполненную проверку.
   `ValidationReportBuilder.combine` связывает scopes/fingerprints, считает record
   один раз; missing/unverified required layer блокирует execution.
3. Value-changing outcome не подменяет исходный `NormalizedBatch`: остановиться
   на существующем M13 copy-only ограничении. Security/schema/identity/system
   failures никогда не становятся quarantine records. Пока внешний M12 record
   failure нельзя передать в M13 quarantine без потери binding, весь такой run
   требует review; не удалять записи из snapshot ради продолжения.
4. Live run: проверить artifact references/retention → `begin` → `stage` всех
   batches → `seal` с revision. Передать `LoadRequest` тому же staging store/loader.
   Loader владеет `SEALED → EXECUTING → COMMITTED/ROLLED_BACK/UNKNOWN` и повтором
   M11/DB constraints, schema/policy/grants в writer transaction; не повторять
   его lifecycle через конкурентные facade transitions.
5. Dry-run: стадия STAGING означает проверенную bounded проекцию в памяти по
   ADR 0027. Не вызывать `begin/stage/seal/cleanup` persistent store и bootstrap.
   Затем `PostgreSQLDryRunPlanner.plan` через inspector без writer credentials.
   Никаких INSERT с rollback, sequence/default evaluation или persistent artifacts.
6. `quarantine_invalid` включается явно и только при ledger/policy поддержки
   M13: quarantine зависимых групп принадлежит loader. FK ordering, transaction,
   generated-key veto, idempotency и commit/audit evidence не воспроизводить в SDK.

**Тест сначала:** `tests/unit/pipeline/test_validation_gates.py`,
`test_loading_flow.py`, `tests/integration/database/test_sdk_orchestrator.py`:
все required M12 layers до DML; transformed values и unverified provenance;
dry-run неизменность target/staging/sequences; insert/upsert; schema drift;
rollback; quarantine dependency group; idempotency и lost commit response.
**Проверка:** `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/pipeline/test_validation_gates.py packages/structuraguard/tests/unit/pipeline/test_loading_flow.py packages/structuraguard/tests/unit/validation/test_provenance_report.py`;
затем `uv run --locked --no-sync pytest -W error::sqlalchemy.exc.SAWarning -m database_integration packages/structuraguard/tests/integration/database/test_sdk_orchestrator.py`.

### 5. State machine, hooks, cancellation и report/audit

Файлы: `pipeline/state.py`, `pipeline/events.py`, `pipeline/reporting.py`,
`pipeline/session.py`. Использовать существующий `PipelineStatus`, не добавлять
новые wire statuses для отдельных callbacks.

Полный логический flow:

```text
source security gate → detection → technical parser → structural profile
→ deterministic/LLM semantic analysis → ParsePlan validation
→ ParsePlan execution → normalized data profile → database inspection
→ deterministic/LLM DB mapping → MappingPlan validation
→ normalization/record validation → staging → dry-run/load → report/audit
```

Успешная последовательность full run:

```text
CREATED → SOURCE_PROBING → TECHNICAL_PARSING → STRUCTURE_PROFILING
→ STRUCTURE_ANALYZING → PARSE_PLAN_CREATED → PARSE_PLAN_VALIDATING
→ SEMANTIC_PARSING → NORMALIZED_DATA_PROFILING → DATABASE_INSPECTING
→ MAPPING → MAPPING_PLAN_CREATED → MAPPING_PLAN_VALIDATING
→ NORMALIZING → VALIDATING → STAGING → LOADING
→ COMPLETED | COMPLETED_WITH_WARNINGS
```

Перед вызовом публикуется вход в stage, после подтверждённого результата — его
evidence. Для composite M6 события внутренних substages фиксируются только по
доступному evidence, без обещания live progress внутри component API. Report/audit
финализируются до terminal publication; отдельного REPORTING enum нет.

| Ветка | Допустимое завершение и условие |
|---|---|
| Неоднозначность, low confidence, missing/unverified checks, unsupported semantics, schema/plan drift | `NEEDS_REVIEW`; сохранить reasons/draft, последующие stages запрещены |
| Security policy запрещает действие | `REJECTED_SECURITY`; typed code, без следующего parser/LLM/DB side effect |
| Runtime/adapter failure до commit | `FAILED`; `ROLLED_BACK` только при отдельном подтверждении отката, не по одному факту exception |
| Caller cancellation до commit | `CANCELLED` после cleanup/rollback; `CancelledError` распространяется caller, не превращается в обычный successful return |
| Deadline до commit | `FAILED` с `PROCESSING_TIMEOUT`; при известном rollback transaction outcome сохраняется отдельно |
| Подтверждённый commit и последующий cancel/timeout/hook/audit-delivery/cleanup failure | `COMPLETED_WITH_WARNINGS`; commit и counts сохраняются |
| `LOAD_OUTCOME_UNKNOWN` | `FAILED` + `TransactionOutcome.UNKNOWN`, staging UNKNOWN; не заявлять rollback и не повторять DML |
| Dry-run готов / заблокирован | `COMPLETED` или `COMPLETED_WITH_WARNINGS` / `NEEDS_REVIEW`; outcome DRY_RUN, фактическая загрузка ноль |

Таблица переходов хранит разрешённые edges и predicates, а не порядок enum.
Terminal states не имеют исходящих edges. `NEEDS_REVIEW` не ждёт человека,
держа connection/transaction: review-and-resume создаёт новый run с новым
inspection/validation и связанными fingerprints. Existing stage retry не делает
переход назад: retry — попытка текущего stage до фиксации результата.

Отмена отменяет активную awaitable операцию; coordinator закрывает owned iterators
и sessions в `finally`, ждёт adapter rollback/cleanup. Cleanup защищён от повторной
отмены отдельным ограниченным временем, не оставляет бесконтрольных background tasks.
CPU loops проверяют deadline на bounded checkpoints; SDK не обещает прервать
блокирующий trusted in-process callback или host transport без cooperative support.
При неполном cleanup нет успешного terminal; неизвестный transaction outcome
сохраняется. До commit `CancelledError` повторно поднимается с безопасным run ID
в доступном runtime context; последний partial report остаётся в этом context
и safe audit, без raw report в exception details. Решение о commit берётся у
loader: внешний timeout wrapper не должен заменять его committed result на FAILED.

Пошаговые вызовы source session останавливаются на своём checkpoint, без ложного
`COMPLETED` всего ingest. Standalone inspector/validator имеет отдельную короткую
run path. `execute` начинает с admission normalized snapshot, profiling/inspection
и validation: skipped source stages не изображаются выполненными. Reuse ParsePlan
всё равно требует profiling и `PARSE_PLAN_VALIDATING`; reuse MappingPlan пропускает
generation, но не `MAPPING_PLAN_VALIDATING`. Эти paths перечислить явно в тестах.

**Event hooks.** Frozen per-run sequence типизированных async callbacks принимает
существующий `AuditEvent`: `stage_started`, `stage_completed`, `stage_failed`,
`stage_retry`, `run_finished` в допустимой grammar event_type. `status` указывает
stage/outcome, IDs — opaque UUID, timestamps — UTC, artifacts — fingerprints.
Callbacks последовательны, awaited и ограничены remaining deadline; нет фоновой
очереди, безлимитного progress на каждый record или повторной отправки по умолчанию.
Callback не может заменить plan/policy/result или разрешить запись. До commit
его ошибка останавливает run; после commit становится warning. Ошибка handler
ошибки не запускает рекурсию: сохранить безопасный code и завершить cleanup.
Terminal hook dispatch входит в финализацию перед публикацией terminal snapshot:
его failure влияет на выбранный конечный статус, а не создаёт запрещённый переход
из уже опубликованного `COMPLETED`. Reentrant вызов того же run отклоняется.

**Audit.** Hooks — уведомления, не механизм transactional audit. Legacy `AuditEvent`
и `AuditStore` не объявлять HMAC-signed. `SecurityAuditEvent`/`AuditChain` используют
существующие `AuditKind` и trusted UUID mappings provider/model/actor/target.
Обязательные signer/store/key prerequisites проверяются до опасной работы.
Target `LOAD_COMMITTED` и durable intent создаёт loader в target transaction.
Внешняя доставка после commit может дать наблюдаемый audit gap, но не rollback.
Failure/rollback event записывается после transactional cleanup; его ошибка
сохраняет `AUDIT_GAP`, не стирает первопричину. Durable delivery/ack выполняет host.

**Reports.** Финальный runtime result собирает фактически доступные
`SourceArtifact`/`ProbeResult`/manifest, `StructureProfile`, ParsePlan,
`SemanticParseReport`, normalized profile, `DatabaseCatalog`, candidate/proposal,
MappingPlan, оба validation outcomes, `DetailedValidationReport`/`ValidationReport`,
`DryRunExecutionPlan` либо `PostgreSQLLoadResult`/`LoadReport`, security reports и
audit evidence. Не достигнутые стадии представлены отсутствием результата,
а не пустыми «успешными» reports. Несколько `SecurityReport` сохраняют собственные
purpose/request bindings; объединять их путём подмены fingerprint нельзя.

Compatibility `LoadReport` строится только при наличии всех его обязательных
fingerprints. Прогнозы inserts/updates/skips/quarantine брать из dry-run plan;
фактические значения — из committed result. Target units не суммировать как
source records. `replayed=True` сохраняет исторический outcome и отдельно ноль
новых writes, `attempt_run_id` не заменяет исходный run ID. Raw origins, plans,
catalog names и подробные component reports считаются чувствительными; hooks/logs
получают только безопасную проекцию, не `model_dump()` всего result.

**Тест сначала:** `tests/unit/pipeline/test_state_machine.py`, `test_events.py`,
`test_outcomes.py`, `test_reports.py`, `tests/security/pipeline/test_audit_boundary.py`:
все разрешённые/запрещённые edges, ранние выходы, cancellation каждой I/O стадии,
hook exception/reentrancy, budget exhaustion, UNKNOWN и post-commit warnings,
отсутствующие reports, counts, redaction и отказ signed audit.
**Проверка:** `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/pipeline/test_state_machine.py packages/structuraguard/tests/unit/pipeline/test_events.py packages/structuraguard/tests/unit/pipeline/test_outcomes.py packages/structuraguard/tests/unit/pipeline/test_reports.py packages/structuraguard/tests/security/pipeline/test_audit_boundary.py`.

### 6. Политики, повторное использование и границы повторов

Файлы: `pipeline/composition.py`, `pipeline/parsing.py`, `pipeline/mapping.py`,
`pipeline/session.py`. Не добавлять универсальный retry decorator вокруг ingest.

| Граница | Timeout / retry / fallback |
|---|---|
| Весь run | Один monotonic deadline `SecuritySession`; budget не сбрасывается staged calls или cache lookup. Cleanup имеет отдельный конечный reserve |
| Snapshot, probe, parser, physical replay | Общий deadline + существующие parser limits; retry по умолчанию отсутствует. Не перечитывать one-shot/изменившийся source и не выбирать другой parser после runtime failure |
| M6 parsing LLM | `SemanticParsingSession` принимает `LLMProvider`, **не** `PolicyAwareLLMRouter`. Использовать существующий `LLMRunProvider(resources=session)` вокруг явно выбранного provider; внутренний M6 wrapper остаётся локальным budget без второго resource accounting. Нет нового router-as-provider adapter |
| M10 mapping LLM | `PolicyAwareLLMRouter(resources=session)` с явной `LLMRoutingPolicy`; retry/fallback только существующих route policies и только разрешённых кодов. Один владелец reservation на фактическую попытку |
| DB inspection/lookups | Общий budget плюс statement/lock/cleanup limits adapters. Orchestrator не повторяет query/transaction автоматически |
| Staging и loader | CAS/revision и ledger принадлежат M13. После неопределённого commit — только существующий reconcile/replay того же binding; новый ключ не лечит UNKNOWN |

`ParsingPolicy.mode`: deterministic не вызывает LLM; llm_assisted использует
детерминированный plan при достаточном evidence; llm_first запрашивает semantic
анализ даже при наличии базового plan в поддержанном M6 scope. У M6 transient
failure может вернуть deterministic preview с `NEEDS_REVIEW`, без terminal manifest;
это наблюдаемая ветвь, не fallback в успешную загрузку. Ошибки validation/security
не ремонтируются другим parser/provider/plan автоматически.

`LLMRoutingPolicy` независимо задаёт разрешённые destinations, classifications и
budgets. `no_llm` запрещает calls в обоих semantic stages, включая llm_first:
несовместимая комбинация требует явного отказа/review, не сетевого запроса.
Глобальный M14 maximum охватывает M6 и M10. Для M6 fixed deployment admission
должен заранее проверить classification/capabilities и exact scanner policy binding;
многомаршрутный parsing failover отсутствует и не обещается. Для M10 каждая попытка
сохраняется в router history и не расширяет исходный egress approval. Отсутствие
base scanner или обязательной DLP подготовки блокирует LLM; сам prompt не gate.

**Reuse по fingerprint:**

- ParsePlan: перепроверить source SHA-256, extraction fingerprint/IDs/parser/options,
  profile fingerprint, plan content/schema/revision и execution options; затем
  полный M5 replay. Совпадение расширения или набора колонок недостаточно.
  Новый extraction с другими IDs не перебиндить автоматически.
- MappingPlan: source/extraction/parse-plan/normalized fingerprints, свежий database
  fingerprint, target ID и policy fingerprint; независимый M11 и M12. Normalized
  identity может зависеть от execution/run IDs: новый parse run не обязан иметь
  прежний hash даже для тех же bytes. Reuse требует полного совпадения существующих
  bindings, не нового «schema-only» cache key.
- Fresh grants/security policies и writable schema проверяются даже при cache hit;
  fingerprint каталога не доказывает grants. Approval/review не переносится на
  изменённый artifact. Не мутировать immutable plan: правка → новая revision/hash.
- Cache только run-local; durable plan store/deduplication не добавлять. Повтор
  analysis внутри session использует её cache; новый run не наследует исчерпанный
  budget или authorization. Audit связывает планы и фактическую validation.

**Тест сначала:** `tests/unit/pipeline/test_policies.py`, `test_plan_reuse.py`,
`tests/security/pipeline/test_llm_budget.py`: матрица parsing/routing, zero calls,
общий budget двух LLM stages, ровно один reservation, expired approval,
fingerprint/registry/policy drift, no hidden fallback и отсутствие автоматического DML retry.
**Проверка:** `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/pipeline/test_policies.py packages/structuraguard/tests/unit/pipeline/test_plan_reuse.py packages/structuraguard/tests/security/pipeline/test_llm_budget.py`.

### 7. Sync wrappers, совместимость и end-to-end приёмка

Файлы: `sync_sdk.py`, `sdk.py`, `config.py`, lazy exports `__init__.py`,
`docs/public-api.md`, `docs/codex/SPEC_INDEX.md` и новый
`docs/adr/0034-sdk-orchestrator.md` при реализации.
ADR закрепляет runtime ownership, конечные paths, report composition и
ограничения относительно §23/24; этот план не утверждает новую архитектуру молча.

Sync facade остаётся отдельной оболочкой. Finite calls используют `asyncio.run`,
не выдают наружу async generators и не удерживают незавершённые generators после
закрытия loop. Пошаговый sync source context хранит только bounded immutable
artifacts и checkpoints; registry/parser iterators закрываются в породившем вызове.
Для provider sessions с loop affinity весь sync workflow выполняется в явном
context manager с одним `asyncio.Runner`, создаваемым при входе, а не в constructor;
owned clients открываются/закрываются в том же loop. Async borrowed clients из
другого loop не переиспользуются. При отсутствии такой composition — typed отказ,
без фонового thread или скрытого повторного LLM analysis.

**Тест сначала:** `tests/unit/pipeline/test_sync_facade.py`,
`tests/integration/test_sdk_pipeline.py`,
`tests/integration/database/test_sdk_orchestrator.py` и existing smoke/import tests.
Проверить 12 групп методов, эквивалентность stepped/ingest/sync, empty source,
caller early close, running-loop error без unawaited coroutine, LLM HTTP lifecycle,
по два независимых runs одного facade, accepted/review/rejected reports и no I/O
при root import/constructor. Для E2E load брать supported copy-compatible данные;
numeric conversion, LOG token selection и другие несовместимые cases отдельно
доказывают отказ, а не потерю provenance ради успеха.

**Проверка:** `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/pipeline/test_sync_facade.py packages/structuraguard/tests/integration/test_sdk_pipeline.py packages/structuraguard/tests/smoke`.
Перед завершением реализации M15: `make lint`, `make typecheck`, `make test`,
`make test-integration`, `make test-security`, `make docs`, отдельно
`make test-database` с доступным Docker/PostgreSQL. Обычный `make test` исключает
`database_integration`; его зелёный результат не подтверждает live load.
Выполнить `structuraguard-review` и `structuraguard-security` по финальному diff.

## Риски

- **Память и source support:** bounded snapshots позволяют композицию текущих APIs,
  но не заменяют durable replay/streaming transport. Превышение бюджета и отсутствие
  host path policy остаются явными отказами; полный §33.4/9 не закрыт.
- **Контракт результата:** полный JSON `IngestResult` чувствителен; для logs
  предназначен `safe_summary`. Nullable reports отражают невыполненные стадии.
- **Полнота загрузки:** copy-only M13, отсутствие generated PK propagation и
  поддержки передачи внешней M12 quarantine ограничивают множество успешных E2E
  scenarios. Поддержка новых преобразований требует отдельного изменения contracts.
- **Неопределённый commit:** без durable ledger/audit evidence нельзя отличить
  потерянный ответ от отсутствующей записи. Только reconcile, без слепого retry.
- **Host ownership:** безопасность transport, реальные retention guarantees,
  base scanner, защита ключей, sandbox backend и audit delivery принадлежат host.
  Facade проверяет admission, но не обещает предоставить отсутствующие adapters.
- **Будущие расширения:** raw path/text/dict/list wrappers, schema-only templates,
  persistent cache, streaming/spill, derived normalized batches, richer report DTO,
  parsing router bridge, `best_effort`, generated keys, demo/evaluation — вне M15.
  Их нельзя скрыто включить в scope или считать реализованными по успешному happy path.


## Уточнения реализации M15

- M5 включает run identity в normalized record IDs. MappingPlan переиспользуется
  только при точном normalized fingerprint; новый run на тех же bytes может
  потребовать новый MappingPlan. Скрытого rebind нет.
- Внешние callbacks получают существующие `AuditEvent`; retries M6/M10 отражаются
  в provider attempts, поскольку сам coordinator stages не повторяет.
- Async `SourceAnalysis.aclose` и sync `close_source`/context manager закрывают
  leases. Host продолжает владеть provider/transport/artifact lifecycle.
- Copy-only M13 и поддержка OS sandbox ограничены существующими adapters.
  Непроверенное преобразование не проходит в load.

## Результаты первичной приёмки реализации

Повторная проверка по каждому критерию, найденные regressions и актуальные
результаты: [матрица приёмки M15](M15_acceptance.md).
Последующий [security review M15](M15_security_review.md) фиксирует найденные
дефекты source classification и их regression tests.

Исторические результаты первого этапа реализации от 2026-09-13. Последующие
приёмка, security review и исправления расширили набор tests; актуальные counts
для передачи находятся в [итоговом checklist](#m15-commit-readiness).

| Проверка | Результат |
|---|---|
| `make lint` | Ruff format/check: успешно, 590 файлов |
| `make typecheck` | mypy: успешно, 586 source files |
| `make test` | 4412 passed; 440 database tests исключены этим target |
| `make test-database` | 440 passed, PostgreSQL 16 и 18 |
| `make test-integration` | 30 passed |
| `make test-security` | 1391 passed |
| `make docs` | MkDocs strict: успешно |
| `make test-build` | wheel/sdist, offline rebuild/install и installed examples: успешно |
| `git diff --check` | Успешно |

Integration/security subsets пересекаются с общим suite; эти counts нельзя
складывать как число уникальных тестов. Общий suite сообщает пять upstream
deprecation warnings от bindings документных парсеров. После уточнения assertions
для unknown/ambiguous structure повторно прошли все девять tests из `test_llm.py`;
после обновления package manifest/exports повторены lint, typecheck и test-build.

Фактические SDK tests находятся в `tests/unit/pipeline/`,
`tests/integration/database/test_sdk_orchestrator.py`,
`tests/docs/test_m15_example.py` и `tests/unit/test_operations.py`.
Они проверяют fake parser/LLM/DB composition, modes/no hidden fallback,
unknown/ambiguous structure, prompt injection, plan fingerprint reuse/mismatch,
safe error causes, cancellation/timeout, hooks, signed audit anchors и sync lease
lifecycle. PostgreSQL cases подтверждают dry-run без target/staging изменений,
live source→report, поздний schema/grants drift и rollback после фактического DML.

Review выполнен по `structuraguard-review` и `structuraguard-security`.
Приёмка относится к ограничениям этого плана и ADR 0034: bounded snapshots,
copy-only load, host-owned integrations и точный normalized fingerprint для
MappingPlan reuse. Она не заявляет поддержку отсутствующих adapters и расширений
из раздела «Риски».

## Подготовка к ручному commit и PR {#m15-commit-readiness}

Проверено 2026-09-13: ветка `feat/m15-sdk-orchestrator`, HEAD `576bd3d`
(merge M14). Index пуст; commit, push и PR не создавались. Код и tests после
последних исправлений review не менялись; на этом шаге обновлены этот план и
`docs/codex/PROJECT_STATE.md`. Существенных открытых findings в проверенном diff
не обнаружено. Приёмка относится к bounded copy-only M15, а не ко всему §33 ТЗ.
Канонический scope: [M15 в ТЗ][spec-m15-handoff].

### Checklist критериев приёмки

Подробное соответствие каждого критерия конкретным test functions и наблюдаемым
эффектам сохранено в [матрице](M15_acceptance.md).

- [x] **1. Public API:** typed async/sync, все группы методов, эквивалентность
  результатов, constructor/registry compatibility и guard активного event loop.
- [x] **2. Pipeline:** полный поддержанный flow, законные transitions;
  analyze/planning не вызывают staging или target writer.
- [x] **3. Plans:** независимые validation, fingerprint bindings, полный EOF;
  непринятый ParsePlan/MappingPlan не получает права на load.
- [x] **4. Policies:** deterministic/NO_LLM без model calls, llm_assisted/llm_first,
  явные ошибки без hidden fallback, повторные строки без вызова LLM на каждую.
- [x] **5. Dry-run:** ноль изменений target/staging/sequences, отдельный прогноз;
  отклонённый concurrent execute не меняет режим активного запуска.
- [x] **6. PostgreSQL:** отдельные principals, DB scope, schema/grants drift,
  transaction/rollback и явный recovery того же M13 ledger binding без нового DML.
- [x] **7. Lifecycle:** cancellation/deadline, review/security/error outcomes,
  cleanup; UNKNOWN отличается от подтверждённого COMMITTED с warning.
- [x] **8. Evidence:** fingerprints, планы и реально полученные reports;
  безопасные hooks/audit, отсутствие выдуманных counts при потере loader response.
- [x] **9. Ограничения:** отрицательные tests на limits, неизвестную/неоднозначную
  структуру, отсутствующий sandbox и изменение значений при copy-only load.

Дополнительные regression tests критериев 5/7/8 после финального review:
`security/pipeline/test_execute_admission.py` (7 cases),
`unit/pipeline/test_acceptance_outcomes.py::test_committed_staging_preserves_outcome_without_loader_result`
(4 cases), PostgreSQL `test_rejected_concurrent_execute_preserves_dry_run` и
`test_committed_staging_preserves_outcome_after_lost_response` (6 cases на 16/18).
Пути здесь относительно `packages/structuraguard/tests/`.
Описание дефектов и red→green evidence — в
[PROJECT_STATE](../codex/PROJECT_STATE.md#m15-review-fix-checks).

### Проверки рабочего дерева

- [x] `git diff --check`; дополнительно проверены все новые файлы через
  `git diff --no-index --check -- /dev/null <path>`, поскольку обычный diff
  не включает untracked files.
- [x] Состав diff проверен по tracked changes и `git ls-files --others
  --exclude-standard`: **47 файлов**, из них **13 modified и 34 new**.
  Это 15 runtime, 21 test, 10 docs/config и `scripts/verify_distribution.py`.
  Случайных generated/temp files и symlinks в этом составе нет.
- [x] Добавленные строки и новые файлы проверены на credential/private-key
  patterns; AST 15 изменённых runtime files — на debug calls, запрещённое
  исполнение/десериализацию и framework imports. Найденных secrets/debug artifacts
  нет. DSN и значения тестовых fixtures не выдаются за реальные credentials.
- [x] `dist/` и `site/` — ожидаемые ignored outputs сборок; в index их нет.
- [x] Production manifests и `uv.lock` не изменены; новых production dependencies
  нет. Lock consistency проверена отдельным target.
- [x] Русские public docstrings, ограничения и migration note соответствуют
  текущему API; два копируемых примера проверяются без сети. ADR 0034 и
  `PROJECT_STATE.md` актуальны, канонический раздел ТЗ дан ссылкой.
- [x] Повторный review исправлений закрывает оба существенных findings;
  сохранены прежние assertions, lint, typecheck и security checks.

### Фактически выполненные команды

Все results ниже получены 2026-09-13 после последних runtime fixes. Полные suites
выполнены на этапе исправления review; на этапе передачи повторены lock-check,
docs tests, strict docs и проверки diff. Повторять неизменённые runtime suites
после правок только этого плана и PROJECT_STATE не требовалось.

Для uv/make использован `UV_CACHE_DIR=/private/tmp/structuraguard-m15-uv-cache`;
для offline `make test-build` — `/Users/katana/.cache/uv` с готовыми build wheels.
Полные команды узких SDK/PG прогонов записаны в
[журнале исправлений](../codex/PROJECT_STATE.md#m15-review-fix-checks).

| Команда | Фактический результат |
|---|---|
| Узкий SDK pytest из журнала | 306 passed |
| SDK PostgreSQL module из журнала | 18 passed, PostgreSQL 16/18 |
| `make lint` | Ruff format/check PASS, 599 files |
| `make typecheck` | mypy PASS, 595 source files |
| `make test` | 4668 passed, 448 database cases deselected |
| `make test-database` | 448 passed, PostgreSQL 16/18 |
| `make test-integration` | 30 passed, 5086 deselected |
| `make test-security` | 1420 passed |
| `make test-build` | Offline wheel/sdist, isolated install/examples PASS |
| `make lock-check` | PASS, resolved 109 packages |
| `uv run --locked --no-sync pytest packages/structuraguard/tests/docs -q` | 209 passed |
| `make docs` | MkDocs strict build и проверка локальных ссылок/anchors PASS |
| `git diff --check` и проверка новых файлов выше | PASS |

Counts пересекающихся suites не суммируются. Пять прежних upstream SWIG
deprecation warnings в main/non-DB integration не подавлены. Первая попытка
`make lock-check` завершилась panic самого uv в macOS SystemConfiguration внутри
sandbox; та же команда вне sandbox прошла. Workers, локальные HTTP fixtures и
Docker проверялись вне sandbox; реальные платные LLM API не использовались.
Первая strict-сборка документации на этом шаге обнаружила две новые ссылки на
несуществующие кириллические anchors. Ссылки исправлены на страницу приёмки,
сборка повторена без ослабления strict mode.

### Непроверенное и ручные действия

Ни один обязательный локальный quality target не пропущен. Reasons для сценариев
за пределами подтверждённой приёмки перечислены в
[матрице ограничений](M15_acceptance.md):
реальные LLM исключены заданием; transport/sandbox/retention/audit host integrations
не предоставляются M15; streaming/spill, generated PK, cross-run resume и расширенный
performance benchmark вне scope; kill/crash и все сочетания cleanup failures
не моделировались. FK/quarantine/business rules покрыты component suites;
полной новой source→report матрицы этих вариантов нет.

- [ ] Ручные review, выбор файлов, commit и создание PR с base `main` — действия
  пользователя; этот шаг подготовил материалы и не выполнял Git mutations.
- [ ] Remote CI, актуальность удалённого `main` и mergeability после публикации —
  не проверены: remote refs не обновлялись, commit/PR по заданию не создавались.
- [ ] Другие OS/Python/DB версии и deployment host guarantees — в этом окружении
  не проверялись; локальные результаты не заменяют эти проверки.

[spec-m15-handoff]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m15-sdk-orchestrator
