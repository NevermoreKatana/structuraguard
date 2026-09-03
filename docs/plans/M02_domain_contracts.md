# План M02 «Доменные модели и contracts»

Статус: реализация завершена, но milestone не готов к ручному commit и PR:
финальный security review выявил два незакрытых High findings на границе
публичных validation/report contracts.

Дата: 2026-09-03.

## Цель

Добавить в SDK стабильные типизированные contracts для цепочки
`SourceArtifact → ExtractedBatch → ParsePlan → NormalizedBatch → MappingPlan →
reports`, не реализуя adapters или orchestration и не ломая публичное
поведение M1.

Реализация поставлена в `structuraguard.contracts`, `structuraguard.domain` и
`structuraguard.ports`. Корневые exports M1 сохранены; concrete parsers,
analyzers/executors, DB/LLM adapters, stores и orchestration не добавлены.

## Основание и принятые уточнения

План опирается на текущую задачу, `AGENTS.md`, `PROJECT_CONTEXT.md`,
`SPEC_INDEX.md` и точечно извлечённые разделы ТЗ: `1.1`, `1.2`, `FR-001`–`FR-003`,
`FR-012`, `FR-014`, `FR-015`, `9.1`, `9.3`–`9.4`, `14`, `16`, `19.1`,
`NFR-007`, `NFR-008`, `NFR-012`, `24`, `25`, `M2` и `30.2`. Полное ТЗ не
загружалось.

В реализации принят `ADR 0003` о двухэтапном parsing. Он:

- расширить `ADR 0001` цепочкой technical parsing → semantic parsing;
- расширить `ADR 0002`, разрешив LLM формировать только
  недоверенное структурированное предложение `ParsePlan` из bounded
  context без tools, credentials, source/DB handles и права на execution;
- закрепить fingerprint chain, validated-plan boundary и новый словарь
  statuses из редакции v2;
- сохранить `DATABASE_FINGERPRINT_MISMATCH` как канонический код
  schema drift и не вводить дублирующий `DATABASE_SCHEMA_DRIFT` до появления
  public behavior.

`docs/architecture.md`, `docs/requirements.md` и
`docs/codex/PROJECT_CONTEXT.md` выровнены с ADR. Каноническим стало
ожидаемое repository tooling имя `StructuraGuard_SDK_Technical_Specification.md`:
файл с суффиксом `_v2` переименован без изменения содержания, ссылки, extractor,
Codex pack validator и `MANIFEST.sha256` проверяются синхронно.

## Наблюдаемое состояние M1

- В `main` влит M1, package имеет версию `0.1.0`.
- Корневой `structuraguard.__all__` содержит ровно 12 lazy exports:
  `SDKConfig`, две facade и девять classes ошибок.
- `SDKConfig` пуст, immutable, `extra="forbid"` и не читает environment.
- Восемь имён facade operations имеют только общую
  `(*args: object, **kwargs: object) -> Never` семантику и всегда возбуждают
  `SDK_OPERATION_NOT_IMPLEMENTED`; предметные signatures M1 не фиксирует.
- `StructuraGuardError.error_code` остаётся открытой uppercase-строкой;
  в M1 стабильны только `SDK_OPERATION_NOT_IMPLEMENTED` и
  `SYNC_API_IN_ASYNC_CONTEXT`. Поля, deep-immutable `details` и redaction
  сохраняются.
- `contracts`, `domain`, `ports`, DTO, plans, reports и adapter protocols ещё
  не существуют.
- `scripts/verify_distribution.py` проверяет точные allowlists package
  files и root exports; новые modules без обновления verifier сломают
  `make test-build`.

В M2 корневой `__all__`, facade signatures и exception base contract не
меняются. Новые публичные импорты появляются через
`structuraguard.contracts` и `structuraguard.ports`. Это аддитивный API;
выпуск milestone поднимает package version до `0.2.0`.

## Границы scope

В M2 входят:

- immutable DTO, value objects, enums, artifact references и reports;
- декларативные `ParsePlan` и `MappingPlan` и validation evidence;
- типизированные ports для Parser, semantic parsing, DB и LLM;
- чистая canonical serialization/fingerprinting и проверка lineage;
- contract, serialization, import-boundary и packaging tests;
- ADR и выравненная документация.

Из M2 исключены:

- registry, format detection и concrete format parsers;
- structural profiler, semantic algorithms, реализации `ParsePlanValidator`,
  `MappingPlanValidator` и executors; локальные DTO/domain invariants остаются в M2;
- LLM adapters, router, prompts, retry/fallback и сетевые вызовы;
- DB reflection, SQLAlchemy models, SQL, staging, loader и transactions;
- source snapshot/lease implementation, pipeline state machine, facade methods и orchestrator;
- любая новая production dependency.

## Архитектурные границы

### `contracts`, `domain` и `ports`

- `structuraguard.contracts` содержит только stable frozen DTO,
  discriminated unions, enums, reports и serializable artifact references. Здесь
  нет I/O, clocks, ID generation и imports из `domain`, `ports`, facade и adapters.
- `structuraguard.domain` содержит только чистые инварианты,
  canonical serialization/fingerprinting и factories, которые импортируют
  `contracts`. Он не дублирует DTO и не выполняет I/O.
- `structuraguard.ports` содержит `Protocol` и runtime request/context
  contracts. Он импортирует только stdlib и `contracts`; adapter packages
  позже будут зависеть от него.
- `contracts` может импортировать только stdlib и Pydantic;
  `domain` — stdlib, Pydantic и `contracts`; `ports` — stdlib и `contracts`.
  SQLAlchemy, HTTPX/LiteLLM, parser libraries, `sdk.py`, `sync_sdk.py`, apps и
  infrastructure запрещены AST boundary test.

### Immutable/value-object semantics

- Все serialized DTO используют один внутренний base contract с
  `frozen=True`, `extra="forbid"` и строгой boundary validation.
- Frozen Pydantic model не считается глубоко immutable сама по себе:
  sequences хранятся как tuples, предметные mappings — как tuples
  typed entries, а structured LLM payload — как bounded canonical JSON object
  string с отдельным payload fingerprint.
- `Any`, mutable `dict/list`, mutable defaults, non-finite numbers и naive datetime
  в stored state запрещены. Метаданные принимают только ограниченное
  рекурсивное JSON-compatible значение.
- IDs, names и versions непусты, ограничены по длине и не
  создаются неявно. Timestamp, ID и версию producer передаёт caller.
- Equality сравнивает полное валидированное значение и точный
  subtype, а не только ID/fingerprint. Hashability обещается только для
  малых key-like value objects.
- Live snapshot/stream/connection является identity/resource object и не
  попадает в equality и serialization. `SourceArtifact` описывает
  immutable fingerprint-bound snapshot, компактный `SourceArtifactRef` — его
  идентичность в downstream DTO; live reader/lease передаётся отдельно через
  runtime context.

### Version и fingerprint

У persisted top-level artifacts используются разные, невзаимозаменяемые
поля:

- `schema_version` — SemVer формата serialized contract;
- `revision` — положительное поколение редактируемого plan;
- `fingerprint` — typed SHA-256 по canonical payload без самого поля
  `fingerprint`;
- явные upstream references: `source_fingerprint`, `extraction_fingerprint`,
  `profile_fingerprint`, `parse_plan_fingerprint`, `normalized_fingerprint`,
  `database_fingerprint` и `target_policy_fingerprint`;
- `ProducerMetadata` — component ID/version, SDK version и, если LLM использована,
  provider/model/prompt/generation versions из typed allowlist без prompt text,
  arbitrary headers и secrets.

Все SHA-256 references сохраняются в единственной форме
`sha256:<64 lowercase hex>`; bare digest на входе нормализуется до этой формы.

Канонический serializer использует только stdlib/Pydantic, сортирует mapping
keys, отклоняет NaN/Infinity и имеет собственную версию. Изменение
одного семантического поля меняет fingerprint; insertion order не меняет.

## Модели и инварианты

### Физическая Extracted Source Model

`SourceLocation` делается discriminated union, а не моделью с набором
независимых optional-полей. Минимальные variants: line range,
tabular row/cell, sheet cell, JSON Pointer, XPath, CSS selector и document
page/block/bounding box. Каждый variant содержит `SourceArtifactRef`;
пустая или противоречивая location невозможна. Для plugins допустим
только namespaced extension variant с bounded frozen metadata.

`ExtractedValue`, `ExtractedBlock`, `ExtractedTable`, `ExtractedCell` и
`ExtractedTreeNode` хранят raw scalar, technical type hint, physical IDs,
ordering и location. `ExtractedBatch` добавляет non-negative batch index,
source/parser/version references, `extraction_id`, `batch_fingerprint`, ordered tuples
blocks/tables/trees и terminal marker. IDs уникальны внутри batch, а
`PhysicalSourceRef` всегда квалифицирован `(extraction_id, batch_index, kind, local_id)`.

Terminal batch содержит `ExtractedDatasetManifest`: непрерывную упорядоченную
последовательность lineage-aware batch summaries, единственный terminal marker,
aggregate `extraction_fingerprint`, counts и bounded `ExtractedSourceIndex`
допустимых physical references. Индекс является selective allowlist для
sample/evidence/selectors и не дублирует каждый physical объект. Проверка stream
сверяет summary и существование каждого indexed reference. Manifest отклоняет
пропуск, reorder, duplicate batch/index/ID и смешение разных source/parser runs.
Поэтому batch fingerprint не подменяется aggregate fingerprint, а validator может
проверять references без полной загрузки raw content.
Fingerprint terminal batch считается без вложенного manifest, чтобы исключить цикл.

Физическая модель не знает entity types, semantic field names,
DB schemas/tables/columns, business normalization и `MappingPlan`.

### StructureProfile, ParsePlan и checked-plan boundary

`StructureProfile`, `StructureCandidate` и `StructureEvidence` ссылаются
только на source/extraction fingerprints и physical IDs/locations; profile имеет
собственный `profile_fingerprint`. Profile описывает
наблюдаемую структуру, а candidate — ранжируемую гипотезу;
ни один из них не является authority на execution.

`StructureAnalysisResult` — отдельный outcome-discriminated contract: `plan_created`
содержит `ParsePlan`, `needs_review` — ranked candidates/issues без executable plan,
`rejected` — typed failure. Поэтому analyzer не обязан фабриковать plan при
неоднозначности.

`ParsePlan` — discriminated union с общими `plan_id`, `schema_version`,
`revision`, `fingerprint`, source/extraction/profile fingerprints, finite confidence,
producer и evidence:

- `TabularParsePlan` — table selector, header/data/footer boundaries, repeated headers,
  typed field selectors и entity grouping;
- `TreeParsePlan` — record roots, relative paths, field selectors и parent/child rules;
- `LogParsePlan` — bounded record variants, line grouping и ссылки на безопасные
  pattern operators;
- `DocumentParsePlan` — section/block selectors, key-value/table extraction targets
  и grouping rules.

Variant-specific rules тоже образуют закрытые discriminated unions. В
схеме нет generic `operator: str` + `args: dict`, code, SQL, callbacks,
shell или произвольной regex execution. DTO отклоняет structural invalid
states; проверка реального существования references и safety patterns остаётся
реализации validator в M5.

`ParsePlanValidationResult` содержит typed issues, decision и точные
source/extraction/profile/plan/validator fingerprints. Только decision `ACCEPTED` без
error issues может содержать `ValidatedParsePlan`. Executor port принимает
только эту обёртку и обязан повторно сверить manifest, batch sequence и fingerprints. Обёртка
не считается неподделываемой security capability.

### Семантическая Normalized Data Model

`RawScalar` и `NormalizedScalar` являются tagged discriminated envelopes для
`string`, `integer`, finite `number`, `decimal`, `boolean`, `date`, `datetime`,
`bytes` (только physical raw) и `null`. Это сохраняет subtype в JSON и не смешивает
`bool` с `int`, `date/datetime` со `str` или `Decimal` с binary float.

`NormalizedValue` хранит `NormalizedScalar`, semantic type ID, non-empty
lineage и ordered typed transformations/issues. Денежные значения — `Decimal`,
datetime — timezone-aware UTC. `SemanticField`, `SemanticEntity` и
`NormalizedRecord` задают семантические names, entities и parent/child links;
duplicate fields/IDs, dangling parent, cycles и значение без provenance
отклоняются.

`NormalizedBatch` содержит source, extraction и parse-plan references,
batch index, `batch_fingerprint`, records и terminal marker. Terminal batch содержит
`NormalizedDatasetManifest` с непрерывной последовательностью lineage-aware batch
summaries, aggregate `normalized_fingerprint`, typed semantic schema/index и counts.
Stream validation сверяет counts и глобальную уникальность record/entity/value IDs.
Он не содержит
DB identifiers и loading policy. Синтетическое/derived value явно ссылается
на input lineage и transformation, а не имеет пустую location. Fingerprint terminal
batch аналогично не включает manifest.

### DatabaseCatalog и MappingPlan

`DatabaseCatalog` и immutable catalog entries описывают dialect,
schemas, tables, columns, PK/FK/unique/checks, writability, non-secret
`TargetIdentity`, `schema_version` и database/target-policy fingerprints. Строка DSN и credentials
никогда не попадает в catalog, plans, errors и reports.

`MappingCandidate` связывает `SemanticFieldRef` с catalog object reference
и evidence, но не даёт authority на write. `MappingPlan` содержит
`plan_id`, `schema_version`, `revision`, `fingerprint`, `target_id`,
source/extraction/parse-plan/normalized/database/target-policy fingerprints,
entity/field/relation mappings, `LoadOperation` и confidence. Он не содержит SQL,
code, callbacks или physical source selectors.

Граница принципиальна:

- `ParsePlan` отвечает, как physical IDs/locations становятся
  semantic entities/fields, и не знает о БД;
- `MappingPlan` отвечает, как только normalized semantic references
  становятся catalog-bound targets, и не возвращается к row/JSONPath/XPath.

Иллюстративное поле `source_path` из старого примера MappingPlan в этой
модели заменяется на `SemanticFieldRef`. Это не изменение смысла
текущей задачи, а её прямое требование о двух разных plans.

`MappingPlanValidationResult` и `ValidatedMappingPlan` повторяют
checked-plan pattern и связывают evidence с plan, `NormalizedDatasetManifest`,
semantic schema/index, catalog,
target identity и policy fingerprints. DB execution port принимает только
`ValidatedMappingPlan` и перепроверяет его перед write.

`LoadContext` не является пустым extension bag. Он связывает writer session с
`TargetIdentity`/policy fingerprint и содержит immutable `LoadPolicy`: `dry_run`,
`ErrorPolicy` (`atomic` по умолчанию, явно выбранные `quarantine_invalid` или
`best_effort`), maximum allowlist/denylist reference, staging и transactional-audit
capability evidence, idempotency key, deadline/cancellation context и требование
rollback до terminal outcome. Реализация staging/transaction остаётся M13, но
удаление любого обязательного safety field уже в M2 отклоняется contract tests.
Target commit запрещён до получения и сверки terminal `NormalizedDatasetManifest`
и всей ожидаемой последовательности batch fingerprints.

### Provenance на всех переходах

| Артефакт | Обязательная связь |
| --- | --- |
| `ExtractedBatch/Manifest` | source fingerprint, parser ID/version, qualified physical refs, ordered batch fingerprints и aggregate extraction fingerprint |
| `StructureProfile/Candidate` | source/extraction fingerprints и evidence на physical IDs/locations |
| `ParsePlan` | source/extraction/profile fingerprints, evidence и producer metadata |
| `ValidatedParsePlan` | source/extraction/profile/plan fingerprints, validator version, decision и issues |
| `NormalizedBatch/Manifest` | source/extraction/parse-plan fingerprints, ordered batch fingerprints и aggregate normalized fingerprint; lineage каждого value и transformation |
| `MappingCandidate/Plan` | source/extraction/parse-plan/normalized/database/target-policy fingerprints и semantic/catalog references |
| `ValidatedMappingPlan` | mapping-plan, normalized, database, target и policy fingerprints плюс validation evidence |
| Reports/audit | `run_id`, artifact references, component versions, counts/issues/status; без secrets и restricted raw values |

Cross-artifact mismatch не исправляется fallback: contract возвращает
typed issue/error и не выдаёт validated wrapper.

### Stable enums, codes и reports

Закрытые wire vocabularies определяются через `StrEnum`:
`PipelineStatus`, `SemanticParsingMode`, `ParsePlanKind`, built-in location/block kinds,
`ValidationDecision`, `IssueSeverity`, `LoadOperation`, `ErrorPolicy` и
`TransactionOutcome`.
Semantic entity/field names, parser/provider/adapter IDs остаются расширяемыми
validated identifiers, а не enums.

Их wire values фиксируются сразу: `SemanticParsingMode` — `deterministic`,
`llm_assisted`, `llm_first`; `ParsePlanKind` — `tabular`, `tree`, `log`, `document`;
location kinds — `line_range`, `tabular_cell`, `sheet_cell`, `json_pointer`,
`xpath`, `css_selector`, `document_block`, `extension`; block kinds — `line`,
`paragraph`, `heading`, `list`, `key_value`, `metadata`, `extension`;
`ValidationDecision` — `accepted`, `rejected`, `needs_review`; `IssueSeverity` —
`info`, `warning`, `error`, `critical`; `LoadOperation` — `insert_only`, `upsert`;
`TransactionOutcome` — `not_started`, `dry_run`, `committed`, `rolled_back`,
`unknown`; `ErrorPolicy` — `atomic`, `quarantine_invalid`, `best_effort`.

`PipelineStatus` фиксирует полный словарь v2: `CREATED`, `SOURCE_PROBING`,
`TECHNICAL_PARSING`, `STRUCTURE_PROFILING`, `STRUCTURE_ANALYZING`,
`PARSE_PLAN_CREATED`, `PARSE_PLAN_VALIDATING`, `SEMANTIC_PARSING`,
`NORMALIZED_DATA_PROFILING`, `DATABASE_INSPECTING`, `MAPPING`,
`MAPPING_PLAN_CREATED`, `MAPPING_PLAN_VALIDATING`, `NORMALIZING`, `VALIDATING`,
`STAGING`, `LOADING`, `COMPLETED`, `COMPLETED_WITH_WARNINGS`, `NEEDS_REVIEW`,
`REJECTED_SECURITY`, `ROLLED_BACK`, `FAILED` и `CANCELLED`. State machine и
transitions остаются M15.

`BuiltInErrorCode` фиксирует уже принятые `SDK_OPERATION_NOT_IMPLEMENTED`,
`SYNC_API_IN_ASYNC_CONTEXT`, `SECURITY_SANDBOX_REQUIRED`, `PARSER_NO_TEXT_LAYER`,
`SOURCE_FINGERPRINT_MISMATCH`, `SOURCE_PATH_NOT_ALLOWED`,
`SOURCE_SNAPSHOT_EXPIRED`, `DATABASE_FINGERPRINT_MISMATCH`,
`DATABASE_TARGET_MISMATCH`, `AUDIT_DURABILITY_REQUIRED`, `PROCESSING_TIMEOUT`,
`DDL_FORBIDDEN`, `TARGET_NOT_ALLOWED` и добавляет требуемые contracts:
`CONTRACT_VERSION_UNSUPPORTED`, `PROVENANCE_INVALID`, `PARSE_PLAN_INVALID`,
`MAPPING_PLAN_INVALID`, `PROMPT_INJECTION_DETECTED`, `SECURITY_LIMIT_EXCEEDED`,
`LLM_DATA_ROUTING_FORBIDDEN`, `LLM_OUTPUT_INVALID`. При этом
`StructuraGuardError.error_code: str` не закрывается: adapters могут иметь
namespaced uppercase extension codes. Pydantic construction errors отличаются
от public `structuraguard.ValidationError` и не переименовываются.

`Issue.code` также остаётся extension-safe uppercase-строкой, а встроенный
`BuiltInIssueCode` начинает стабильный additive vocabulary: `INVALID_SOURCE_LOCATION`,
`DUPLICATE_ARTIFACT_ID`, `INVALID_BATCH_SEQUENCE`,
`PROVENANCE_REFERENCE_MISSING`, `UPSTREAM_FINGERPRINT_MISMATCH`,
`UNKNOWN_PLAN_OPERATOR`, `PLAN_CONTAINS_EXECUTABLE_CONTENT`,
`AMBIGUOUS_STRUCTURE`, `UNSUPPORTED_CONTRACT_VERSION`. Удаление или смена смысла
кода требует major contract version; новые коды добавляются совместимо.

Реализованные stage-specific `SemanticParseReport`, `ValidationReport`,
`LoadReport`, `SecurityReport` и `AuditEvent` используют `schema_version`,
immutable `ProducerMetadata`, typed issues, counts, statuses и artifact
references. Terminal success не совместим с error/critical issues, а
review/failure/rollback/cancellation требуют typed issue. `SecurityReport`
связывает request, content, canonical payload, classification, routing и
redaction evidence; `allowed` требует фактически просканированные элементы.
`SecurityApproval` переносит разрешающий report и его canonical fingerprint в
`LLMRequest`, где вся security lineage сверяется повторно. `AuditEvent` принимает
только `event-<UUID|ULID>`, а `run_id` — только `run-<UUID|ULID>`;
pre-security events запрещают evidence,
а terminal success/`REJECTED_SECURITY` встраивают allowed/blocked
`SecurityReport`, сверяют run/redaction/hash и не допускают report из будущего.
Typed issues сохраняют `code` и `message_key`, но не произвольный human text.
Physical source refs допустимы только там, где contract сверяет их с
manifest/profile allowlist; reports и mapping outcomes без такого binding
отклоняют непустые `source_refs`. `LoadReport` содержит role-specific target,
source/extraction/parse/normalized/mapping/validation/database/policy lineage;
terminal validation/load outcomes требуют полного баланса record counts;
dry-run отделяет `would_load_records` от фактически записанных записей.
Остальные stage reports и
`IngestResult` остаются scope orchestrator;
его будущий contract не копирует
упрощённый happy-path example: outcome-discriminated variants допускают early
failure/review только с уже созданными stage reports; `COMPLETED` требует
полную цепочку, а `ROLLED_BACK` — подтверждённый transaction outcome.
Reports не принимают free-form raw values или human messages. Диагностика
передаётся через stable `code`/`message_key`; локализованный текст формируется
вне persisted contract. Identifier/token canaries покрыты негативными tests.

## Protocol signatures

| Port | Фиксируемый contract |
| --- | --- |
| `Parser` | Immutable `adapter_id`, `version`, `priority`; async `probe(SourceArtifact, ProbeContext) -> ProbeResult`; streaming `parse(SourceArtifact, ParseContext) -> AsyncIterator[ExtractedBatch]`. Оба context содержат один fingerprint-bound reader contract и immutable resource limits. Parser не получает LLM/DB ports и не возвращает semantic values. |
| `SemanticStructureAnalyzer` | Async `analyze(StructureAnalysisRequest) -> StructureAnalysisResult`; request содержит extracted manifest/index, profile, candidates, bounded sample/context и mode, но не DB catalog/credentials; result различает plan/review/rejected. |
| `ParsePlanValidator` | Pure `validate(ParsePlanValidationRequest) -> ParsePlanValidationResult`; request связывает plan с `SourceArtifactRef`, `ExtractedDatasetManifest`/index и `StructureProfile`. Реализация появится в M5. |
| `ParsePlanExecutor` | Streaming `execute(AsyncIterable[ExtractedBatch], ValidatedParsePlan, ParseExecutionContext) -> AsyncIterator[NormalizedBatch]`; никогда не принимает raw `ParsePlan`. |
| `MappingPlanValidator` | Pure `validate(MappingPlanValidationRequest) -> MappingPlanValidationResult`; request связывает plan с `NormalizedDatasetManifest`/semantic index, `DatabaseCatalog` и `MappingPolicyRef`. Реализация появится в M11. |
| `DatabaseInspector` | Async read-only `inspect(DatabaseInspectionRequest) -> DatabaseCatalog`; request не сериализует DSN. |
| `DatabasePlanExecutor` | Async `execute(AsyncIterable[NormalizedBatch], ValidatedMappingPlan, LoadContext) -> LoadReport`; повторно сверяет target/policy/database/plan fingerprints до write. |
| `DatabaseAdapter` | Composite protocol из `DatabaseInspector` и `DatabasePlanExecutor`; composition не отменяет разные inspection/writer principals. |
| `LLMProvider` | Immutable `capabilities`; async `generate_structured(LLMRequest) -> LLMResponse`. Purpose-discriminated request для semantic parsing и mapping содержит только bounded/minimized/masked data, response schema ID/version и data-classification/routing evidence; tools, source/DB handles и credentials в contract отсутствуют. |

Async iterator methods фиксируют одну семантику вызова: caller итерирует
результат без дополнительного `await`, а coroutine methods сначала ожидаются.
Это уточняет иллюстративную `async def ... -> AsyncIterator` запись ТЗ.

## Критерии приёмки

1. `structuraguard.contracts` и `structuraguard.ports` имеют явные, без дублей
   `__all__`; корневые 12 exports M1, lazy import и все M1 tests неизменны.
2. Все public DTO fully typed, extra fields отклоняются, nested input defensively
   copied/frozen; мутация после construction не меняет модель.
3. Физические и семантические модели не смешаны; замена format parser не меняет
   downstream contracts.
4. Batch и dataset fingerprints различены; manifests отклоняют missing, duplicate,
   reordered или mixed-run batches, а physical refs квалифицированы batch identity.
5. `StructureAnalysisResult` явно представляет plan, review и rejected outcomes;
   неоднозначность не создаёт фиктивный `ParsePlan`.
6. Все четыре `ParsePlan` variants проходят один union adapter; missing/unknown
   discriminator и payload чужого variant отклоняются.
7. `ParsePlan` не принимает DB identifiers, `MappingPlan` — physical selectors;
   payload одного plan не десериализуется как другой.
8. Validator requests получают manifests/indexes, необходимые для проверки refs;
   executor/load ports принимают только validated wrappers, а result с errors не
   содержит accepted wrapper.
9. Каждый переход имеет согласованные upstream fingerprints и provenance;
   разорванная/смешанная lineage отклоняется.
10. Fixed literal payload проходит validate → exact JSON dump → validate round-trip
    без потери tagged scalar subtype, union subtype, enums, versions, fingerprints
    и provenance.
11. Одинаковые значения одного exact type равны; другой subtype или изменение
    одного поля не равны. Canonical fingerprint повторяем и не зависит от map order.
12. Все перечисленные enum/error/issue/status wire values закреплены exact-value
    tests; unknown closed-vocabulary value отклоняется.
13. `LoadContext` без dry-run/error policy, target/policy binding, staging,
    transactional audit или rollback evidence отклоняется до adapter execution.
14. Fully typed fake implementations подставляются вместо каждого port под strict
    mypy; временный negative fixture доказывает отказ неверной signature. Shared
    contract tests проверяют async/streaming shape без external I/O.
15. AST/import tests подтверждают направление зависимостей, отсутствие
    infrastructure imports, environment/network/DB/file side effects и optional imports;
    wheel/sdist и installed-wheel smoke покрывают новые modules/exports.
16. Документация и ADR описывают ту же двухэтапную модель, statuses и LLM authority;
    старая parser → normalized схема не остаётся канонической.

### Итоговый checklist на 2026-09-03

- [x] 1. Dedicated exports и совместимость M1 —
  `test_m2_is_exported_only_from_dedicated_subpackages`,
  `test_protocol_exports_are_explicit_and_minimal` и M1 public-export tests.
- [ ] 2. Строгие immutable DTO и отклонение недопустимых состояний — основные
  свойства подтверждены `test_public_dto_contract_is_frozen_strict_and_typed`,
  `test_public_dto_annotations_do_not_expose_mutable_collections` и value-object
  tests, но `IssueCodeStr` пока принимает credential-like значение.
- [x] 3. Разделение physical и semantic models —
  `test_extracted_value_preserves_raw_physical_value_without_business_meaning`,
  typed fake contracts и layer-boundary tests.
- [x] 4. Batch/dataset fingerprints и целостность manifests — manifest cases в
  `test_m02_invariants.py` и stream regressions для foreign/mixed/reordered batches,
  cardinality и global IDs.
- [x] 5. Outcomes semantic analysis —
  `test_structure_analysis_outcomes_round_trip_without_fabricated_plan`.
- [x] 6. Закрытый union четырёх `ParsePlan` variants —
  `test_all_parse_plan_variants_round_trip_through_the_closed_union` и
  `test_parse_plan_union_rejects_missing_unknown_and_hybrid_variants`.
- [x] 7. Разделение `ParsePlan`/`MappingPlan` — schema boundary tests и
  `test_parse_and_mapping_plan_payloads_are_not_cross_deserializable`.
- [x] 8. Checked-plan boundary — validation outcome tests и negative mypy tests
  для raw parse/mapping plans.
- [x] 9. Provenance/fingerprint chain — broken-lineage, stale-plan, source-ref и
  mapping-evidence regressions.
- [x] 10. Exact JSON round-trip — tagged scalar, plan/outcome union и report
  round-trip tests в `test_m02_invariants.py`.
- [x] 11. Equality и canonical stability — value-object, scalar subtype,
  repeatability и mapping-order tests.
- [x] 12. Stable wire vocabularies — exact-value и unknown-value tests в
  `test_m02_contracts.py`, а также docs/status sync test.
- [x] 13. Обязательная load safety — `LoadPolicy`/`LoadContext` invalid-state
  tests для dry-run, staging, transaction, audit, rollback и bindings.
- [x] 14. Protocol substitutability — typed fakes, async/streaming shape и
  negative strict-mypy fixtures для всех девяти ports.
- [x] 15. Dependency/import/distribution boundaries — AST/import-side-effect,
  dependency-boundary, packaging и installed-wheel smoke checks.
- [x] 16. Docs/ADR consistency — `test_m02_architecture_contract.py`,
  `test_m02_examples.py` и strict MkDocs build.
- [ ] Итоговый security gate — санитизированный `ValidationError` сохраняет raw
  Pydantic error в `__context__` для constructor и union entrypoints.

### Findings, блокирующие ручной commit

- **High — `packages/structuraguard/src/structuraguard/contracts/common.py:114-117`.**
  `ValidationIssue.code` и
  `message_key` ограничены грамматикой, но не проходят `_safe_text`;
  `AKIAIOSFODNN7EXAMPLE` принимается и детерминированно сериализуется в
  reports/audit. Минимальное исправление: применить безопасный text validator к
  `IssueCodeStr` и добавить regression cases для credential canaries в обоих
  полях.
- **High — `packages/structuraguard/src/structuraguard/contracts/_base.py:253-340`
  и `packages/structuraguard/src/structuraguard/contracts/common.py:218-225`.** Новый
  redacted `ValidationError` поднимается внутри `except`, поэтому исходная
  Pydantic-ошибка остаётся в `__context__`; при прямом constructor call её
  `errors()` содержит отклонённый secret input. Минимальное исправление:
  сформировать redacted exception внутри `except`, поднять его после выхода из
  handler и рекурсивно проверить отсутствие secrets в `__context__`/`__cause__`
  для всех public validation entrypoints и union adapters.

### Фактически выполненные проверки

- Acceptance-focused pytest suite — `541 passed`.
- `make test` — `600 passed in 3.03s`.
- `make check` — lock check, Ruff для 53 файлов, strict mypy для 51 source file,
  `600 passed`, MkDocs strict и distribution build/installed smoke успешно.
- `git diff --check` — успешно.
- `git status --short --untracked-files=all` и
  `git status --short --ignored` — commit-кандидат содержит только ожидаемые
  source/docs/tests; `dist/`, `site/` и caches игнорируются.
- `git rev-parse HEAD:StructuraGuard_SDK_Technical_Specification_v2.md` и
  `git hash-object StructuraGuard_SDK_Technical_Specification.md` вернули один
  blob hash: ТЗ переименовано без изменения содержания.
- `rg` scans для credential/private-key patterns, debug hooks и generated files —
  реальные secrets/debug artifacts не найдены; совпадения secret patterns
  ограничены намеренными негативными test canaries.

Успешные tests не закрывают два High findings выше: текущий suite не проверяет
credential canary в `IssueCodeStr` и raw input во вложенной exception chain.

### Пропущенные проверки

- `make test-integration` и `make test-security` не запускались: таких targets в
  текущем `Makefile` нет; существующие contract security/import regressions входят
  в `make test`.
- Remote CI и Python 3.13–3.14 matrix не запускались: ветка/PR по условию задачи
  не создаются; независимая проверка возможна после ручной публикации ветки.
- Реальные DB, parser и LLM integration checks не запускались: adapters и
  executors находятся вне scope M2, а платные LLM API запрещены условием задачи.
- Hypothesis suite не запускался: Hypothesis отсутствует в dev dependencies;
  свойства проверяются табличными parameterized tests без добавления зависимости.

## Шаги реализации

### 1. Зафиксировать architecture decision и baseline M1

- Файлы: `docs/adr/0003-two-stage-parsing-contracts.md`, `docs/architecture.md`,
  `docs/requirements.md`, `docs/threat-model.md`, `docs/public-api.md`,
  `docs/codex/PROJECT_CONTEXT.md`, `docs/codex/SPEC_INDEX.md`,
  `docs/codex/PROJECT_STATE.md`, `mkdocs.yml`; переименование
  `StructuraGuard_SDK_Technical_Specification_v2.md` в
  `StructuraGuard_SDK_Technical_Specification.md`; обновление `MANIFEST.sha256`.
  `AGENTS.md`, `scripts/extract_spec_sections.py` и
  `scripts/validate_codex_pack.py` входят в обязательную проверку canonical path;
  менять их нужно только если переименование не восстановило текущий contract.
- Поведение: закрепить решения из этого плана, выровнять status/spec
  drift и отметить M2 как следующий additive public surface; не обещать
  реализацию adapters.
- Тест: существующие M1 public/export/facade tests проходят до
  и после docs-only change; MkDocs не находит broken links.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/test_public_exports.py
  packages/structuraguard/tests/unit/test_operations.py`; `make docs`.

### 2. Ввести common value contracts и canonical fingerprinting

- Тесты сначала: `packages/structuraguard/tests/unit/contracts/test_m02_contracts.py`
  и `packages/structuraguard/tests/unit/contracts/test_m02_invariants.py`.
- Файлы: `packages/structuraguard/src/structuraguard/contracts/_base.py`,
  `packages/structuraguard/src/structuraguard/contracts/common.py`,
  `packages/structuraguard/src/structuraguard/contracts/__init__.py`,
  `packages/structuraguard/src/structuraguard/domain/canonical.py`,
  `packages/structuraguard/src/structuraguard/domain/__init__.py`.
- Поведение: deep-frozen JSON values, IDs, versions, typed fingerprints,
  tagged raw/normalized scalars, producer/run metadata и exact-value
  enums/statuses/error/issue codes; round-trip, equality и golden hash без
  import-time effects.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/contracts/test_m02_contracts.py
  packages/structuraguard/tests/unit/contracts/test_m02_invariants.py`.

### 3. Добавить SourceArtifact, locations и Extracted Source Model

- Тесты сначала: physical/provenance cases в
  `packages/structuraguard/tests/unit/contracts/test_m02_invariants.py`.
- Файлы: `packages/structuraguard/src/structuraguard/contracts/source.py` и
  `packages/structuraguard/src/structuraguard/domain/lineage.py`.
- Поведение: валидные physical location variants, raw typed values,
  qualified physical IDs, distinct batch/aggregate fingerprints,
  `ExtractedDatasetManifest`/source index, rejection missing/reordered/duplicate/mixed
  batches, defensive copy и отсутствие semantic/DB knowledge.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/contracts/test_m02_invariants.py`.

### 4. Зафиксировать StructureProfile и discriminated ParsePlan

- Тесты сначала: structure/plan cases в
  `packages/structuraguard/tests/unit/contracts/test_m02_contracts.py` и
  `packages/structuraguard/tests/unit/contracts/test_m02_invariants.py`.
- Файл: `packages/structuraguard/src/structuraguard/contracts/parsing.py`.
- Поведение: profile/candidates/evidence и четыре строгих plan variants,
  `StructureAnalysisResult` plan/review/rejected outcomes, local invalid-state checks,
  source/extraction/profile/plan fingerprints и accepted validation evidence;
  никакого executor behavior.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/contracts`.

### 5. Добавить Normalized Data Model и цепочку provenance

- Тесты сначала: normalized/lineage cases в
  `packages/structuraguard/tests/unit/contracts/test_m02_invariants.py`.
- Файлы: `packages/structuraguard/src/structuraguard/contracts/normalized.py` и
  `packages/structuraguard/src/structuraguard/domain/lineage.py`.
- Поведение: semantic fields/entities/records, typed scalars, transformations,
  no-empty provenance, parent/link invariants, distinct batch/aggregate fingerprints,
  `NormalizedDatasetManifest`/semantic index и source/extraction/parse-plan binding.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/contracts/test_m02_invariants.py`.

### 6. Добавить DatabaseCatalog, MappingPlan и reports

- Тесты сначала: catalog/mapping/report/load и serialization cases в
  `packages/structuraguard/tests/unit/contracts/test_m02_invariants.py`.
- Файлы: `packages/structuraguard/src/structuraguard/contracts/database.py`,
  `packages/structuraguard/src/structuraguard/contracts/mapping.py`,
  `packages/structuraguard/src/structuraguard/contracts/reports.py`.
- Поведение: immutable catalog, semantic-to-catalog mapping без physical
  selectors/SQL, validation request с normalized manifest/index, checked MappingPlan
  evidence, explicit load safety policy, stage reports и valid outcome combinations;
  никакой DB reflection/load.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/contracts`.

### 7. Зафиксировать ports и substitutability

- Тесты сначала:
  `packages/structuraguard/tests/unit/ports/test_m02_protocols.py` и
  `packages/structuraguard/tests/unit/ports/test_m02_static_contracts.py` с fully
  typed fakes и subprocess-mypy negative fixture;
  `packages/structuraguard/tests/unit/contracts/test_m02_contracts.py`
  проверяет, что LLM requests не принимают tools/credentials/source/DB handles и
  требуют routing evidence, а DB execution нельзя вызвать без полного `LoadPolicy`.
- Файлы: `packages/structuraguard/src/structuraguard/ports/source.py`,
  `packages/structuraguard/src/structuraguard/ports/parser.py`,
  `packages/structuraguard/src/structuraguard/ports/semantic.py`,
  `packages/structuraguard/src/structuraguard/ports/database.py`,
  `packages/structuraguard/src/structuraguard/ports/llm.py`,
  `packages/structuraguard/src/structuraguard/ports/__init__.py`.
- Поведение: exact signatures из таблицы, корректная
  async/streaming semantics, reader/limits и для probe, и для parse, positive static
  substitution, manifests в validator requests, checked-plan/load-policy boundary и
  absence лишних capabilities в request objects.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/ports
  packages/structuraguard/tests/unit/contracts`;
  `uv run --locked --no-sync
  mypy packages/structuraguard/src/structuraguard/contracts
  packages/structuraguard/src/structuraguard/domain
  packages/structuraguard/src/structuraguard/ports
  packages/structuraguard/tests/unit/ports/test_m02_static_contracts.py`.

### 8. Закрепить dependency, public import и distribution contracts

- Тесты сначала:
  `packages/structuraguard/tests/smoke/test_m02_layer_boundaries.py`,
  `packages/structuraguard/tests/unit/contracts/test_m02_contracts.py` и расширение
  `packages/structuraguard/tests/smoke/import_probe.py`.
- Файлы: `packages/structuraguard/src/structuraguard/contracts/__init__.py`,
  `packages/structuraguard/src/structuraguard/domain/__init__.py`,
  `packages/structuraguard/src/structuraguard/ports/__init__.py`,
  `packages/structuraguard/pyproject.toml`, `uv.lock`,
  `scripts/verify_distribution.py`,
  `packages/structuraguard/tests/unit/test_public_exports.py`,
  `packages/structuraguard/tests/smoke/test_import_side_effects.py`,
  `packages/structuraguard/tests/packaging/test_distribution_verifier.py`,
  `packages/structuraguard/tests/packaging/test_metadata.py` и `docs/public-api.md`.
- Поведение: root `__all__` M1 не меняется; subpackage exports canonical;
  AST graph и import probes без infrastructure/side effects; wheel/sdist allowlists
  полны; package version и verifier синхронно равны `0.2.0`.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/smoke/test_m02_layer_boundaries.py
  packages/structuraguard/tests/smoke/test_import_side_effects.py
  packages/structuraguard/tests/unit/test_public_exports.py
  packages/structuraguard/tests/packaging`; `make test-build`.

### 9. Выполнить review и итоговые gates

- Review: correctness локальных/cross-artifact invariants, SemVer/wire
  compatibility, trust-boundary minimization, deep immutability, serialization, import graph
  и совпадение docs/code/tests.
- Файлы: весь M2 diff; новые findings исправляются в том же
  вертикальном шаге и не ослабляют tests.
- Проверка: `git diff --check`, `make lint`, `make typecheck`,
  `make test`, `make docs`, `make test-build`; эквивалентный полный target —
  `make check`.

`make test-integration` и `make test-security` в текущем Makefile отсутствуют.
Для contract-only M2 соответствующие security/import checks входят в
обычный `make test`; создавать фиктивные targets в плане не нужно.

## Отложенный scope по milestones

- M3–M4: source lease/reader implementation, registry, probe selection и parsers.
- M5: profiler, deterministic analyzer, `ParsePlanValidator` и `ParsePlanExecutor`.
- M6: LLM adapters/router/privacy policy и LLM-assisted analyzer.
- M7: DB reflection и catalog fingerprint для PostgreSQL/SQLite.
- M8–M10: normalized profiling и deterministic/LLM DB mapping.
- M11: `MappingPlanValidator` implementation.
- M12–M13: record validation, staging, transaction/load/rollback.
- M15: state machine, typed facade signatures, sync delegation и orchestrator.

## Риски

- Deep immutability не обеспечивается одним `frozen=True`; без
  защитного копирования и nested-mutation tests plans могут меняться без
  изменения fingerprint.
- Слишком свободные metadata/operators вернут code/SQL/prompt authority в
  декларативные plans; поэтому unknown variants и extra fields отклоняются.
- Слишком закрытые enums могут сломать plugins; enums применяются
  только к закрытому vocabulary, а extension IDs остаются validated strings.
- Canonical serialization — wire contract: его изменение без новой
  `schema_version` и migration policy сделает сохранённые plans невоспроизводимыми.
- Validated wrapper можно сконструировать в Python; он сужает API и
  несёт evidence, но не заменяет deterministic revalidation перед execution.
- Reports и LLM requests могут стать каналом утечки. В M2 они
  хранят только safe references, counts и bounded redacted metadata;
  punctuation/acronym/camelCase-normalized key denylist, common API-token/DSN/
  credential и Unicode log-forging canaries покрыты негативными tests.
  Persisted issues не имеют free-form message, audit IDs ограничены точными
  формами `event-<UUID|ULID>`/`run-<UUID|ULID>` и
  terminal audit связывает конкретный security report. Полные content-aware DLP
  и prompt-injection controls остаются M6/M14.
- Новые package files ломают exact distribution allowlist, если verifier
  не обновлён в том же шаге.
- Hypothesis заявлен в target stack, но отсутствует в текущих dev
  dependencies. M2 использует табличные pytest cases; новая dependency
  не добавляется только ради плана.
