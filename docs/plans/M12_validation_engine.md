# M12 — conservative normalization и многоуровневая валидация

Статус: standalone-сервисы A–D готовы к ручному commit и отдельному PR в `main`;
общий milestone выполнен частично: 4 критерия закрыты, 5 закрыты частично.
Автоматические batch integration/projection, derived schema 1.3 и сквозной
coordinator остаются отдельной работой. См. [аудит критериев](M12_acceptance_audit.md).
Обновлено: 2026-09-13. [API scalar registry](../normalization.md).
Архитектурные предложения зафиксированы в [ADR 0022](../adr/0022-conservative-normalization-and-validation.md).
Канонический scope: [M12 в ТЗ](https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m12-validation-engine).
Результаты проверок и ограничения текущего PR — в
[подготовке к ручному commit](#m12-commit-readiness).

## Цель

Детерминированно нормализовать значения без потери raw content и получать полный
отчёт применимых проверок типов, JSON Schema, DB constraints, business rules и
provenance, который не разрешает загрузку при ошибках или недоказанных условиях.

### Основание и текущее поведение

Из ТЗ прочитаны только normalization/validation, связанные контракты и M12.
Воспроизводимое извлечение без загрузки полного документа в контекст:

```bash
python3 scripts/extract_spec_sections.py 'FR-012.1' 'FR-012.2' 'FR-014' '14. Mapping Plan' '15. Нормализация' '16. Валидация' 'M12. Validation Engine' 'Задание 12. M12'
```

В `SPEC_INDEX.md` строка Validation engine ссылается на §15–16; соседняя строка
Security также содержит M12. Scope здесь определяется фактическим заголовком
M12: normalizers, schema, DB constraints, business rules, provenance, all-errors.

| Существующий механизм | Следствие для M12 |
|---|---|
| `NormalizedValue` содержит tagged scalars, `raw_value`, `normalized_value`, `source_refs`, `origins`, `selection`; collections неизменяемы | Не заменять DTO словарями и не перезаписывать raw. Arrays/objects пока не являются scalar values. |
| При `selection` поле `transformations` обязано точно совпадать с её операцией | Нельзя просто дописать `parse_decimal` к истории M5: нужна versioned история следующего этапа. |
| `ParsePlanExecutor` проверяет реальный physical stream; token/spans могут извлекаться из полного raw parent text | Нормализовать результат selection, а provenance проверять до physical origin, включая selector и offsets. |
| M8 `profiling/_patterns.py` уже распознаёт числа/даты; `LocalePolicy` содержит `ru_RU`, `en_US`, `en_GB`, `unspecified` | Переиспользовать доказанные grammar primitives после выделения чистых helpers; ranked inference не даёт разрешение менять значение. |
| `MappingPlan` 1.0/1.1 связывает всю fingerprint chain; `FieldMapping` не имеет transformations | Нормализация завершается до M8–M11. Сохранять закрытый MappingPlan и повторять downstream после изменения данных. |
| M11 проверяет декларацию и metadata, возвращая `MappingPlanValidationResult`/`ValidatedMappingPlan` | Это отдельный обязательный gate; record validation не отменяет его veto. Wrapper и согласованные hashes не удостоверяют автора и не разрешают запись. |
| `ValidationIssue` имеет только `code`, `severity`, `message_key`, `source_refs`; `ValidationReport` запрещает unbound physical refs | Не добавлять raw messages. Нужен отдельный отчёт M12 с именованными bindings и безопасными locations. |
| `DatabaseType`/catalog-v1 содержит limits, enum/domain, keys, FK и инертный CHECK text | Можно проверять значения по metadata; нельзя исполнять выражения из каталога или считать snapshot доказательством наличия строк. |

Проверены реализации и связанные tests contracts M2/M11, execution M5,
profiling M8 и mapping validation M11. Приоритетные решения:
[ADR 0001](../adr/0001-public-api-and-run-policies.md),
[0003](../adr/0003-two-stage-parsing-contracts.md),
[0010](../adr/0010-verified-parse-plan-execution.md),
[0018](../adr/0018-bounded-normalized-profiling.md),
[0021](../adr/0021-mapping-plan-validation.md).

Целевое поведение полного M12: неоднозначный ввод остаётся неизменным с issue; доказанные преобразования
получают воспроизводимую историю. Один вызов собирает независимые проблемы всех
уровней и отличает проверенное, неприменимое, зависимое и недоказанное условие.
Единый вызов и автоматическое управление зависимостями пока не реализованы.

## Критерии приёмки

- [x] **AC-01.** Примеры §15 проходят при явно заданной policy: trim, русская сумма → `Decimal`, дата, телефон с `+`, boolean и empty → null. Повторная обработка с той же policy не меняет результат и не дублирует историю.
- [x] **AC-02.** `01/02/2026` и `1,250` при неоднозначной локали дают соответственно `AMBIGUOUS_DATE` и `AMBIGUOUS_NUMBER`; raw, origin и selected input сохранены. Money никогда не проходит через float.
- [ ] **AC-03 — частично.** Draft 2020-12 проверяет object/array projection, required/additional properties, numeric bounds и композицию схем; локальные refs работают, попытки retrieval не вызывают file/network I/O. Проверен caller-provided JSON; автоматические projection и обратный индекс locations отсутствуют.
- [x] **AC-04.** Проверяются все категории §16.4 с явным исходом: локальный pass/failure, подтверждение read-only snapshot либо недоказанное условие. Неизвестный CHECK/UNIQUE/FK не становится pass.
- [x] **AC-05.** Все 16 операторов §16.5 имеют positive/negative/null/missing/boundary cases. SQL, Python, callbacks и expression strings не становятся инструкциями.
- [ ] **AC-06 — частично.** Подмена location, physical raw, selector, normalization step, любого bound fingerprint и чужой batch обнаруживается даже после согласованного пересчёта недоверенных DTO hashes. Проверена цепочка source → ParsePlan → normalization; автоматический rebind/revalidation MappingPlan/catalog отсутствует.
- [ ] **AC-07 — частично.** В одном наборе присутствуют независимые normalization/type/schema/DB/rule/provenance issues; порядок стабилен. Ошибка одного поля не скрывает нарушения других, зависимые проверки не создают выдуманных ошибок. Проверены отдельные validators и trusted aggregation; общий engine и per-check dependency outcomes отсутствуют.
- [ ] **AC-08 — частично.** Между batches проверяются exact uniqueness, ссылки и агрегаты. До terminal, EOF и cleanup нет успешного итогового evidence; превышение лимита исключает ACCEPTED. Проверены completed datasets/snapshots; автоматическая сборка межуровневого dataset из stream отсутствует.
- [ ] **AC-09 — частично.** Legacy wire/hash и семантика M2/M5/M8/M11 сохранены; новые версии поддержаны всеми затронутыми consumers. Нет DDL, записи target data, LLM repair или import-time I/O. Legacy/additive compatibility проверена; derived schema 1.3 и миграция consumers отсутствуют.

Матрица «критерий → наблюдаемый тест» для всех девяти IDs и дополнительных
требований A–D находится в [аудите приёмки](M12_acceptance_audit.md).
Отметки AC-01/02 относятся к scalar API A, AC-04/05 — к catalog/adapter scope C;
они не подтверждают готовность общего coordinator.

## Затронутые контракты

Ниже имена новых DTO/ports — проектируемый API, а не уже доступные классы.
Пути исходников относительно `packages/structuraguard/src/structuraguard/`.

| Файлы | Изменение |
|---|---|
| `contracts/normalization.py`, `ports/normalization.py` | Immutable `NormalizationPolicy`, field bindings по `SemanticFieldRef`, ordered normalizer descriptors, `NormalizationTrace`, результат нормализации; pure `Normalizer` port. Registry — instance-owned service, фиксируемый на run. |
| `contracts/normalized.py` | Schema 1.3.0 для derived batches/manifests: отдельный trace, upstream normalized binding, normalization policy/registry binding; обновление canonical hashes и validation инвариантов. |
| `contracts/record_validation.py`, `ports/validation.py` | `ValidationOptions`, `ValidationContext`, async `RecordValidator`, `RecordValidationReport`, `RecordIssueLocation`, per-check outcomes и именованные evidence bindings. Входы передаются раздельно для all-errors, без fail-fast request-конструктора на всей совокупности. |
| `contracts/business_rules.py`, `contracts/constraint_validation.py` | Закрытый discriminated `RuleSet`; typed DB check requests/results и отдельный async `ConstraintReader` в `ports/validation.py`. В DTO нет connection, SQL, callable или credentials. |
| `contracts/common.py`, `exceptions.py`, package exports | Additive vocabulary issues/fatal errors; использовать SDK `ValidationError`, не отдавать сторонние exceptions/messages напрямую. |
| `contracts/reports.py` | Сохранить прежний `ValidationReport`; явная безопасная summary-проекция M12 без physical refs. Старые invariants не ослаблять. |
| `domain/normalized_fingerprint.py`, `profiling/_stream.py`, M9–M11 consumers | Принимать schema 1.3.0 с проверкой derivation. Content hash M8 по-прежнему описывает значения/topology, lineage hash отдельно включает normalization evidence. |

Legacy 1.0–1.2 читаются без автоматической миграции и без добавления пустых полей
в wire/hash. M12 не признаёт legacy provenance достаточным без replay; legacy
1.0 без проверяемой lineage получает явный отказ нового сервиса. Старые API
остаются читаемыми. Миграции пользовательской БД и изменения facade не требуются.

### Порядок уровней и зависимостей

Синтаксические проверки §16.1 остаются у technical parsers. M12 проверяет их
завершённые artifacts и DTO intake, а не повторно разбирает XML/CSV/контейнеры.

| Порядок | Уровень | Когда выполняется |
|---|---|---|
| L0 | Bounded intake, policy/registry/schema/rules validation; artifact lineage | До serialization, expensive traversal и любых optional reads. Невалидный общий context останавливает небезопасную работу. |
| L1 | Conservative normalization | После проверенного ParsePlan execution; до M8 profiling → M9/M10 mapping → M11 validation. |
| L2 | Типы и semantic constraints | На итоговых selected/normalized values без coercion: integer/Decimal/date/UTC datetime/bool/UUID/email/string/enum; containers на явной projection. |
| L3 | JSON Schema | На immutable логической projection records/entities с обратным индексом к value IDs. |
| L4 | DB constraints | На target projection только после актуального M11. Сначала local checks, затем отдельно разрешённый read-only preflight. |
| L5 | Business rules | На явно выбранном semantic или target scope; только доступные проверенные operands. |
| L6 | Полная provenance validation и сведение отчёта | Проверка physical replay → selection → normalization → target binding для каждого значения; завершение после EOF/cleanup всех owned streams. |

Это порядок результата, а не право доверять provenance до L6: physical refs,
origin ownership и hashes проверяются при потреблении данных. При их подмене
не выполнять normalizers/DB lookups для затронутого значения. L6 закрывает полную
цепочку и coverage. Нормализатор и engine — отдельные сервисы, связанные evidence:
engine повторно значения не исправляет. Ошибки L1 включаются в общий отчёт.
До передачи keys в C2 их полная цепочка physical replay → selection → normalization
уже должна быть проверена; финальный L6 завершает общую coverage, а не откладывает
проверку источника данных до момента после внешнего чтения.

После неудачной нормализации независимые schema/type checks видят сохранённое
исходное selected значение; арифметические правила с неподходящим operand имеют
`blocked_by`, а не ложное нарушение суммы. При отказе M11 доступны semantic
проверки, но target checks отмечаются зависимыми и DB I/O запрещён.

### Immutability, raw preservation и repair boundaries

1. Входы, `raw_value`, `origins`, locations и `selection` никогда не мутируются.
   Нормализатор получает `normalized_value` после selection, не полный raw parent.
   Успех создаёт новую value/batch/manifest version; IDs связывают то же наблюдение,
   а новые fingerprints отражают новые значения и policy.
2. В schema 1.3.0 `transformations` сохраняет прежний смысл selection. Отдельный
   trace хранит selected input, ordered шаги с ID/version/config fingerprint и
   input/output evidence, итоговый semantic type. Chain проверяется повторным
   исполнением разрешённых pure normalizers. Секреты не копируются в issues.
3. Цепочка атомарна для одного значения: при ошибке шага результат всей цепочки
   не применяется; исходный DTO сохраняется, failed step отражается в outcome.
   Нельзя выдавать частично обработанное значение за успешно нормализованное.
   На failure нельзя присвоить `money` строке в обход текущего DTO invariant.
4. Registry и все options замораживаются на run; один ID/version имеет одну
   реализацию. Повтор с теми же bindings возвращает тот же итог и историю;
   смена policy требует нового прохода от сохранённого selected input. Не
   накладывать новую policy поверх старого normalized output.
5. Только явные field bindings разрешают trim/empty-to-null/locale conversion.
   `ParseField.locale_hint` (`ru-RU`) и M8 inference являются hints, не authority;
   trusted policy явно преобразует подтверждённый hint в существующий `LocalePolicy`.
   Приоритет: field policy → run policy → unspecified; process locale не читается.
6. Ни один validator не исправляет вход. Запрещены fuzzy enum matching, guessed
   timezone/currency/country, округление/обрезка, удаление дублей, подстановка
   required/FK/PK, выполнение schema defaults и исправление email по догадке.
   Допустимые изменения задаются только до валидации в normalization policy.
7. LLM не вызывается M12. Repair ParsePlan/MappingPlan возможен только во внешнем
   существующем policy-controlled flow: новая revision, полный независимый gate,
   replay. Изменение нормализации инвалидирует M8 profile, mapping candidates,
   MappingPlan/M11 evidence и record report. Просто пересчитать старый hash нельзя.
   Provenance/security/policy failures не понижаются до warning или quarantine.

### Ошибки, полнота и безопасная диагностика

`ValidationIssue` сохраняется как envelope. `RecordIssueLocation` хранится рядом
в отчёте с однозначным соответствием: layer, batch/record/entity/value ordinal,
schema/rule/constraint index. Имена полей, JSON Pointer с пользовательскими keys,
CHECK text и исходные значения остаются только в чувствительном локальном
artifact под policy; safe summary содержит codes и числовые координаты.
Physical refs включаются лишь после разрешения по bound replay; несуществующий
ref описывается location index, не публикуется как проверенный `source_ref`.

| Категория | Стабильные codes для реализации |
|---|---|
| L1 ambiguity / invalid input | `AMBIGUOUS_DATE`, `AMBIGUOUS_NUMBER`, `NORMALIZATION_INVALID_VALUE`, `NORMALIZATION_LOCALE_REQUIRED`, `NORMALIZATION_TIMEZONE_REQUIRED`, `NORMALIZATION_CURRENCY_MISMATCH`, `NORMALIZATION_LOSSY_CONVERSION` |
| L2 | `VALIDATION_TYPE_MISMATCH`, `VALIDATION_FORMAT_INVALID`, `VALIDATION_ENUM_INVALID` |
| L3 | `JSON_SCHEMA_INVALID`, `JSON_SCHEMA_VIOLATION`, `JSON_SCHEMA_REF_FORBIDDEN`, `JSON_SCHEMA_REF_UNRESOLVED`, `JSON_SCHEMA_FEATURE_UNSUPPORTED` |
| L4 | `DB_NOT_NULL_VIOLATION`, `DB_LENGTH_VIOLATION`, `DB_NUMERIC_RANGE_VIOLATION`, `DB_NUMERIC_SCALE_VIOLATION`, `DB_ENUM_VIOLATION`, `DB_PRIMARY_KEY_VIOLATION`, `DB_UNIQUE_VIOLATION`, `DB_CHECK_VIOLATION`, `DB_FOREIGN_KEY_VIOLATION`, `DB_WRITE_FORBIDDEN`, `DB_IDENTITY_VIOLATION`, `DB_CONSTRAINT_UNVERIFIED` |
| L5 | `BUSINESS_RULE_INVALID`, `BUSINESS_RULE_OPERATOR_UNSUPPORTED`, `BUSINESS_RULE_VIOLATION`, `BUSINESS_REFERENCE_UNAVAILABLE` |
| L6 | Переиспользовать `INVALID_SOURCE_LOCATION`, `PROVENANCE_REFERENCE_MISSING`, `UPSTREAM_FINGERPRINT_MISMATCH`; добавить `PROVENANCE_VALUE_MISMATCH`, `PROVENANCE_SELECTION_MISMATCH`, `PROVENANCE_TRANSFORMATION_MISMATCH`, `PROVENANCE_REPLAY_REQUIRED` |
| Fatal intake/runtime | `VALIDATION_INPUT_INVALID`, `VALIDATION_POLICY_INVALID`, `NORMALIZER_NOT_FOUND`, `NORMALIZER_DUPLICATE_REGISTRATION`, `NORMALIZER_REGISTRY_FROZEN`, `NORMALIZER_OUTPUT_INVALID`, `VALIDATION_REPORT_LIMIT_EXCEEDED`; существующие `SECURITY_LIMIT_EXCEEDED`, `PROCESSING_TIMEOUT`, `CONTRACT_VERSION_UNSUPPORTED` |

`message_key` — фиксированный словарь, severity задаётся кодом и явно допустимой
policy, не текстом схемы или ответом LLM. Коды M11 передаются без переименования.
Нарушения данных — issues; неисправный trusted context, registry/plugin contract
или невозможность безопасного выполнения — SDK typed exception. Не принимать
`model_construct`/`model_copy` как доказательство валидности; preflight и
повторная bounded DTO validation обязательны. Cancellation распространяется
после cleanup, не преобразуется в validation pass.

All-errors означает все применимые проверки в пределах объявленного бюджета:

- Check outcomes: `passed`, `failed`, `not_applicable`, `blocked_by`, `unverified`.
  `not_applicable` требует причины, например отсутствие CHECK; выключенный
  обязательный reader или неизвестная DB semantics означает `unverified`.
- Ошибки группируются стабильно по layer → global record ordinal → entity/value
  ordinal → rule/schema/constraint index → code. Независимый порядок dict/set,
  async completion, hash seed и часы не влияют на evidence fingerprint.
  Разные locations с одинаковым code сохраняются; дубли одного check/location
  устраняются. Внутренние причины `anyOf` не становятся самостоятельными ошибками
  успешной ветки; у failed composition сохраняется bounded дерево причин.
- `complete` означает законченный обход и учёт всех обязательных checks, а не
  их успех. REJECTED при error/critical; NEEDS_REVIEW при отсутствии доказанного
  нарушения, но наличии unverified/незакрытых prerequisites; ACCEPTED только
  при complete, отсутствии блокеров и полностью учтённых records.
- `total = valid + invalid + unresolved` на terminal; pending до EOF отдельно.
  Один record считается один раз независимо от количества issues. Coverage
  provenance считается по всем output values, включая unmapped; для пустого
  dataset знаменатель явно нулевой, coverage — not applicable.
- Hash отчёта связывает source/extraction/ParsePlan, исходный и derived normalized
  manifests, normalization policy/registry, M11 result/MappingPlan, catalog/target/
  access policy, schema registry, rules/reference snapshots, validation options
  и ordered outcomes. UTC timestamps и raw diagnostics в него не подмешиваются.
- В `ValidationOptions` конечны bytes/depth/items входов, число batches/records,
  steps per value, decimal digits/exponent, schema resources/ref visits/branches,
  rule nodes/work, unique keys, DB keys/queries/rows, replay state и report bytes/issues.
  Проверять до выделения памяти и дорогих операций. При cap: typed failure с
  bounded partial evidence (`complete=false`), без успешного terminal outcome.
  Не обрезать отчёт молча и не подменять exact checks samples/sketches.

## Шаги

Порядок реализации: **A → B → C → D**. В каждом шаге сначала tests, затем minimal
implementation и узкий прогон. Все команды запускаются из корня репозитория.
Пути tests относительно `packages/structuraguard/tests/`; новые директории создаются
соответствующим шагом. Базовые contracts L0 и failure outcomes вводятся в A,
boundaries B/C применяются сразу; D завершает сквозное evidence и acceptance.

### A. Normalizer registry и locale-aware built-ins

Текущая задача выделила исполнимый scalar scope: registry/snapshots, pure built-ins,
custom protocol/config, атомарный `NormalizationResult` и property/security tests.
`normalize_value()` сохраняет полный исходный DTO и отдельную историю, поэтому
legacy `NormalizedValue.transformations` не меняется. Schema 1.3.0 и производные
batches/manifests остаются отдельным шагом A; consumers M8–M11 не объявлены
поддерживающими ещё несуществующую версию. Phone/email canonical checks и UUID
включены в scalar built-ins по текущей задаче пользователя.
Фактические файлы, проверки и ограничения scalar scope перечислены в
[отчёте приёмки](M12_A_normalizers_acceptance.md); список ниже также включает
запланированную batch integration.

- **Файлы:** `contracts/normalization.py`, `contracts/normalized.py`,
  `ports/normalization.py`, `normalization/registry.py`, `normalization/service.py`,
  `normalization/builtins.py`, `domain/scalar_grammar.py`, `profiling/_patterns.py`,
  `profiling/_stream.py`, `domain/normalized_fingerprint.py`, exports и codes.
- **Поведение:** immutable registry snapshot; явная регистрация из trusted
  composition, без discovery/import по имени из данных. Unknown/duplicate ID,
  версия и malformed result дают typed отказ. Pure plugin — доверенный Python
  extension point, не sandbox; в persisted descriptor только ID и typed config.
  Общие locale primitives выделить из M8 без переноса ranked inference/PII scan.
- **Built-ins:** opt-in trim; empty-to-null с явным набором tokens; strict integer
  без bool и потери ведущих нулей идентификаторов; exact decimal/money с проверкой
  grouping и разрешённой currency; date по явному format/locale; datetime с
  явным offset → UTC; boolean по непересекающимся allowlisted tokens; phone —
  удаление только разрешённых separators у номера с явным `+` и проверкой длины.
  Не добавлять country code, не удалять extension, не менять digits произвольно.
  UUID/email имеют opt-in canonical checks в scalar registry; обязательность
  поля и record-level semantic constraints относятся к L2.
- **Tests:** реализованы `unit/normalization/test_normalizer_registry.py`,
  `test_normalizers.py`, `property/normalization/test_normalization_properties.py`,
  `security/normalization/test_boundary.py`, `contract/normalization/test_protocol.py`.
  Для batch integration запланированы `unit/normalization/test_lineage.py` и
  `unit/contracts/test_m12_normalization_contracts.py`.
  Проверить §15, RU/US/GB/unspecified, NBSP/grouping, conflicting currency,
  ambiguous/slashed dates, leap day, UTC overflow/naive time, Decimal context,
  huge exponents, no-op/idempotence, immutable inputs, failed-chain rollback,
  unknown plugin, forged trace и сохранение legacy wire/hash.
- **Проверка:** `uv run --locked --no-sync pytest -q packages/structuraguard/tests/unit/normalization packages/structuraguard/tests/property/normalization packages/structuraguard/tests/security/normalization packages/structuraguard/tests/unit/contracts/test_m12_normalization_contracts.py packages/structuraguard/tests/unit/profiling packages/structuraguard/tests/unit/structure/test_execution.py`.

### B. JSON Schema Draft 2020-12

По текущей задаче реализован самостоятельный async validator над caller-projected
JSON: meta-validation, локальная URN policy, guarded recursion, safe regex profile,
all-errors paths/codes и bounded cache. [API](../json-schema-validation.md),
[приёмка](M12_B_schema_acceptance.md), [ADR 0023](../adr/0023-local-json-schema-validation.md).
Nested IDs/dynamic refs/custom vocabulary и произвольные regex отклоняются явно;
их полноценная поддержка и topology projection ниже остаются будущей интеграцией.
Фактические файлы — `contracts/json_schema.py`, `ports/json_schema.py`,
`validation/json_schema.py` и helpers `_schema_input/_schema_regex/_schema_backend.py`.

- **Файлы:** `contracts/record_validation.py`, `ports/validation.py`,
  `validation/types.py`, `validation/projection.py`, `validation/json_schema.py`,
  `validation/_schema_input.py`; package `pyproject.toml` и `uv.lock`.
- **Поведение:** явно выбрать `Draft202012Validator`, проверить schema перед
  запуском и обходить `iter_errors`. Существующий wire dump DTO не является JSON
  instance: Decimal передавать числом без float/string round-trip; даты — ISO,
  datetime — UTC representation. Для numeric comparisons/`multipleOf` нужна
  точная арифметика с ограничением digits/work, включая числовые schema literals;
  bool не является integer, математически целое JSON number соответствует integer.
  API для validator/type checker подтверждён [документацией jsonschema](https://python-jsonschema.readthedocs.io/en/stable/validate/).
- **Projection:** schema binding явно выбирает entity/record и child collections
  через существующую topology; сохраняются order, missing versus null и обратный
  индекс к values. Так проверяются arrays/nested objects, не расширяя scalar union
  и не разбирая строки как JSON. Не выводить неизвестные collection paths по
  догадке. Unsupported mapping arrays/JSON в M11 остаётся блокером загрузки.
- **Refs/formats:** local fragments и заранее переданные immutable resources;
  `referencing.Registry` без retrieval. Запретить network/file retrieval после
  URI resolution, включая вложенные `$id`, `$ref`, `$dynamicRef`; allowlisted
  URI является только ключом локального ресурса. Незнакомый draft/vocabulary и
  unresolved refs дают явный отказ. Это использует официальный
  [in-memory referencing API](https://python-jsonschema.readthedocs.io/en/stable/referencing/).
  По умолчанию `format` — annotation; L2 выполняет обязательные semantic проверки.
  Отдельный opt-in assertion mode включает только объявленные formats и отвергает
  неподдержанные, без DNS/network. Нельзя заявлять full Format-Assertion vocabulary
  при неполной реализации ([Draft 2020-12, §7](https://json-schema.org/draft/2020-12/json-schema-validation)).
- **Limits:** ограничить refs/branches/recursion до и во время traversal.
  Неограниченные пользовательские regex не выполнять: для `pattern` и
  `patternProperties` определить закрытое линейное подмножество с доказуемой
  стоимостью; остальное — `JSON_SCHEMA_FEATURE_UNSUPPORTED`, не silent pass.
  Nested repeats/backrefs/lookarounds запрещены. Обычный async timeout не
  прерывает CPU-bound regex; поддержку произвольного ECMAScript regex отложить.
- **Dependency gate:** `jsonschema` и `referencing` объявлены прямыми runtime
  dependencies после проверки MIT/Python compatibility; версии и hashes закреплены
  lock. При обновлениях повторять license/support/supply-chain проверку.
  Проверить installed-package import и отсутствие side effects; core/domain не
  импортирует jsonschema.
- **Tests:** `unit/validation/test_types.py`, `test_json_schema.py`,
  `test_projection.py`; `contract/validation/test_draft202012.py`;
  `security/validation/test_schema_boundary.py`. Cases: exact Decimal `0.3/0.1`,
  `required`, `enum`, bounds, `additionalProperties`, `$defs`, local recursive refs,
  `prefixItems`, `contains`, `unevaluatedProperties`, compositions, missing/null,
  safe/unsafe patterns, SSRF/file refs, cycle/work caps, injection в messages/keys.
- **Проверка:** `uv run --locked --no-sync pytest -q packages/structuraguard/tests/unit/validation/test_types.py packages/structuraguard/tests/unit/validation/test_json_schema.py packages/structuraguard/tests/unit/validation/test_projection.py packages/structuraguard/tests/contract/validation/test_draft202012.py packages/structuraguard/tests/security/validation/test_schema_boundary.py`.

### C. DB constraints и safe business-rule DSL

- **Файлы:** `contracts/business_rules.py`, `contracts/constraint_validation.py`,
  `validation/db_constraints.py`, `validation/business_rules.py`,
  `validation/_rule_input.py`, `ports/validation.py`,
  `database/constraint_reader.py`, `database/_constraint_queries.py`.
  Общие проверенные range/precision predicates M11 выделять в domain только
  при реальном переиспользовании; не импортировать приватный validator в contracts.
- **C1 / local DB:** NOT NULL отличает missing от null: отсутствие допускается
  только для подтверждённого default/generated/identity, explicit null — нет.
  Проверить длину без truncation, enum/domain, numeric range/scale без округления,
  generated/non-writable/operation/source identity veto. Для PK/UNIQUE — exact
  composite keys всего входа; FK — полные упорядоченные пары и parent scope.
  Учитывать dialect, null semantics и collation: неизвестная семантика не
  заменяется Python equality или SQLite affinity. Metadata samples не являются
  доказательством constraint. Отсутствующего incoming parent не считать нарушением
  до завершения соответствующего scope/EOF; более поздний batch может содержать
  parent. Budget ограничивает pending refs и exact keys.
- **C2 / DB state:** отдельный read-only `ConstraintReader` с SQLite/PostgreSQL
  adapters получает только закрытые key requests по разрешённому catalog scope.
  SQLAlchemy формирует параметризованные запросы; chunk/query/row/time budgets
  обязательны. Нет raw SQL, DDL, writer handle или full-table materialization.
  Result связывает target/catalog/policy, набор проверяемых keys и read snapshot;
  consistency нельзя подразумевать между несколькими независимыми SELECT.
  Проверить UNIQUE/FK относительно существующих строк и совместно с incoming keys.
  В upsert существующая строка по подтверждённой identity не является duplicate
  insert; conflict другой unique key и две incoming updates одной identity — issues.
  Nulls/collations/partial/expression/deferred cases без подтверждённой поддержки
  получают `DB_CONSTRAINT_UNVERIFIED`. Reader отсутствует — тот же явный исход.
- **CHECK:** catalog SQL хранить как metadata, не интерпретировать и не исполнять.
  Для local CHECK допустима явно настроенная typed rule с binding к конкретному
  constraint/catalog fingerprint и проверенной эквивалентностью. Само совпадение
  имени/hash эквивалентность не доказывает; без такого evidence CHECK остаётся
  unverified до DB enforcement. Unknown domain CHECK также блокирует acceptance.
  Final UNIQUE/FK/CHECK enforcement и TOCTOU защита остаются обязанностью будущей
  транзакционной загрузки; read-only pass не становится правом на commit.
- **DSL:** discriminated Rule classes и прямой dispatch по allowlist; field refs
  разрешаются по schema, не через getattr/dotted Python expressions. Полный минимум:
  `required_if`, `equals`, `not_equals`, `gt`, `gte`, `lt`, `lte`, `date_lt`,
  `date_lte`, `sum_equals`, `mutually_exclusive`, `at_least_one`, `unique_by`,
  `matches_reference`, `min_items`, `max_items`. Aliases классов не добавляют
  новые операции.
  `sum_equals` использует typed operands/ограниченный product числовых полей,
  а не `expression="quantity * unit_price"`. Decimal arithmetic точна; default
  tolerance = 0, ненулевая tolerance только явно в policy, без округления данных.
  Missing и null различаются; required/presence проверяются явно, сравнение
  недоступного operand даёт зависимый исход. Aggregates scoped по конкретному
  parent/collection; empty sum = 0, null item не пропускается молча.
  `unique_by` имеет явные scope/null semantics и exact state между batches;
  `matches_reference` использует immutable fingerprint-bound справочник или
  разрешённый reader, не файл/URL из RuleSet. Лимит на key state даёт отказ.
- **Tests:** `unit/validation/test_db_constraints.py`, `test_business_rules.py`;
  `property/validation/test_constraint_boundaries.py`;
  `security/validation/test_rule_boundary.py`, `test_constraint_reader.py`;
  `integration/test_sqlite_record_validation.py`,
  `integration/database/test_postgresql_record_validation.py`.
  Проверить каждый operator; cross-batch duplicate/parent/sum; null/missing,
  generated/defaults, composite keys, upsert conflicts, decimal scale/negative
  scale/overflow, dialect differences, unknown CHECK, stale reference snapshots,
  key limits, read-only policy и вредоносные expressions/identifiers.
- **Проверка:** `uv run --locked --no-sync pytest -q packages/structuraguard/tests/unit/validation/test_db_constraints.py packages/structuraguard/tests/unit/validation/test_business_rules.py packages/structuraguard/tests/property/validation/test_constraint_boundaries.py packages/structuraguard/tests/security/validation/test_rule_boundary.py packages/structuraguard/tests/security/validation/test_constraint_reader.py packages/structuraguard/tests/integration/test_sqlite_record_validation.py`;
  `uv run --locked --no-sync pytest -q -W error::sqlalchemy.exc.SAWarning -m database_integration packages/structuraguard/tests/integration/database/test_postgresql_record_validation.py`.

### D. Provenance validation и all-errors report

Уточнение поставки 2026-09-12: текущая задача реализует physical/normalization replay
и итоговый `DetailedValidationReport` через bounded completed snapshots.
Автоматическая projection и ingest coordinator остаются интеграционным продолжением;
непроверенные required layers не получают pass. См. [ADR 0025](../adr/0025-provenance-replay-and-validation-report.md)
и [приёмку D](M12_D_provenance_acceptance.md). Ниже сохранён общий целевой scope M12.

- **Файлы:** `validation/provenance.py`, `validation/engine.py`,
  `validation/reporting.py`, `validation/_bounded.py`,
  `contracts/record_validation.py`, `contracts/reports.py`, `ports/validation.py`;
  `structure/_runtime.py` и `structure/execution.py` только для выделения общего
  verified selection/replay helper, без параллельного интерпретатора ParsePlan.
- **Replay:** caller предоставляет повторяемый stream immutable physical snapshot
  с bound manifest/profile и checked ParsePlan context. Повторить проверки M5,
  сравнить raw origin/location и selection с исходным normalized artifact, затем
  воспроизвести normalization trace и связать результат с точным MappingPlan.
  Самодостаточные origin DTO, sample index, content hash M8 или validation wrapper
  не доказывают источник. Без physical replay — `PROVENANCE_REPLAY_REQUIRED`.
  Не открывать path/URL из source location и не выполнять CSS/XPath как запросы.
  Batches соединять ограниченным state по refs/order; невозможность уложиться
  в budget требует explicit failure, а не накопления всего dataset.
- **Formats:** CSV row/column; JSON pointer; XML XPath; HTML selector;
  LOG line range; XLSX sheet/cell; PDF page/block/bounding box; DOCX block/table.
  Координаты проверяются против реально extracted objects. Для selected tokens,
  joined lines и M6 spans проверяются parent text, границы, порядок и selector
  fingerprint. Raw означает physical model, а не восстановленные bytes исходного
  файла: исходные bytes отдельно связывает source fingerprint.
- **Report:** собрать L1–L6 и M11 blockers по описанным outcomes; сохранить
  полную named lineage, counters, check coverage и immutable locations. На EOF
  сверить manifest counts/hashes, global IDs и exact constraints; late error,
  cancellation или cleanup failure не оставляют successful terminal artifact.
  Отчёт — evidence проверки snapshots, не отдельная load capability.
- **Tests:** `unit/validation/test_provenance.py`, `test_report.py`,
  `unit/contracts/test_m12_validation_contracts.py`;
  `contract/validation/test_record_validator.py`;
  `property/validation/test_all_errors.py`;
  `security/validation/test_provenance_forgery.py`, `test_report_privacy.py`;
  `integration/test_validation_engine.py`, `smoke/test_validation_boundaries.py`.
  Матрица всех formats, selection/span tampering, forged wrappers/rehashed inputs,
  wrong run/target/policy, разные batch boundaries, missing/duplicate terminal,
  late cross-batch failure, cancellation/cleanup и all-errors со stable ordering.
  Monkeypatch file/network/LLM/SQL execution: pure layers не делают I/O; reader
  доступен только в C2, не выдаётся normalizer/schema/DSL/provenance helper.
- **Проверка:** `uv run --locked --no-sync pytest -q packages/structuraguard/tests/unit/validation packages/structuraguard/tests/unit/contracts/test_m12_validation_contracts.py packages/structuraguard/tests/contract/validation packages/structuraguard/tests/property/validation packages/structuraguard/tests/security/validation packages/structuraguard/tests/integration/test_validation_engine.py packages/structuraguard/tests/smoke/test_validation_boundaries.py`.
- **Документация:** `docs/validation.md`, `examples/validation_engine.py`,
  `docs/plans/M12_acceptance.md`, `docs/plans/M12_security_review.md`, согласование
  статуса ADR 0022 и navigation. Пример: один источник с несколькими независимыми
  ошибками, полный безопасный отчёт и успешный повтор с явной новой policy.

### Завершение milestone

После A/B/C — узкий suite и `make lint typecheck`; после D и финального
correctness/security review — `make lint typecheck test test-integration
test-security test-database docs test-build`. PostgreSQL gate требует Docker;
недоступность среды фиксируется как непроверенное, не считается pass.
`git diff --check` и strict docs build обязательны. Для текущей задачи планирования
достаточны docs build, review документа и проверки существующих затронутых contracts;
команды будущих suites выше не являются отчётом об их выполнении.

### Проверка исходной задачи планирования — 2026-09-12

- `make lint` — Ruff format/check: 387 файлов, без ошибок.
- `make typecheck` — mypy: 383 файла, без ошибок.
- `make docs` — strict build, включая ссылки плана и ADR, пройден.
- Baseline suite: `test_m02_contracts.py`, `test_m02_security_regressions.py`,
  `test_m11_validation_contracts.py` из `unit/contracts/` и
  `unit/structure/test_execution.py`: 563 passed; ещё два PDF/DOCX cases сначала
  остановлены sandbox из-за недоступного `/bin/ps` memory watchdog, затем оба
  прошли с разрешённым доступом. Все 565 выбранных cases подтверждены.
- Correctness/security review плана завершён: уточнены provenance до DB lookup
  и отложенная проверка missing parent до конца scope. Существенных открытых
  findings нет. Полные runtime/integration/database suites для изменения только
  документации не запускались; они обязательны при завершении реализации M12.

## Подготовка к ручному commit и PR — 2026-09-13 {#m12-commit-readiness}

Текущий diff можно оформить отдельным PR с самостоятельными сервисами A–D.
Такой PR не закрывает полный M12: незавершённые AC-03/06/07/08/09 перечислены
выше. Ветка — `feat/m12-validation-engine`, предполагаемая base — `main`.
Commit, staging и PR автоматически не создавались.

В diff 103 файла: 35 production, 45 tests, 18 документов и 5 файлов
конфигурации/сборки. Из них 19 tracked modifications и 84 новых файла.
Состав: contracts/ports, normalization/validation, DB reader и общие domain
helpers; unit/contract/property/security/integration/docs/packaging/smoke tests;
руководства A–D, ADR 0022–0025, планы/audit/security report, API/index/state;
`mkdocs.yml`, оба `pyproject.toml`, `uv.lock`, `scripts/verify_distribution.py`.
В текущем шаге подготовки изменены только этот план и `PROJECT_STATE.md`.

После исправления трёх Medium из финального review новых существенных findings
не выявлено. Доказательства, минимальные исправления и 21 regression/control case
приведены в [security report](M12_security_review.md#m12-final-review-fixes).
Проверка состава всех 103 файлов не обнаружила credential patterns, debug/unsafe
вызовов в production, бинарных или случайных временных/generated files. Wheel,
sdist и `site/index.html` подтверждены как ignored через `git check-ignore`;
build outputs не входят в состав commit. Это локальный scan текущего diff,
а не проверка всей Git history или внешней инфраструктуры.

### Фактически выполненные проверки

Полные runtime gates выполнены после последних исправлений Python-кода.
При подготовке commit менялась только документация; повторяются static/docs
checks, примеры, lock и проверка diff. Числа main/integration/security перекрываются
и не суммируются как число уникальных тестов.

| Команда | Фактический результат |
|---|---|
| `make lint` | Ruff format/check: 456 файлов, OK |
| `make typecheck` | Strict mypy: 452 файла, OK |
| `make test` | 3811 passed, 116 PostgreSQL cases deselected |
| `make test-integration` | 30 passed, 3897 deselected |
| `make test-security` | 935 passed |
| `make test-database` | 116 passed на PostgreSQL 16/18; SAWarning как error |
| `make test-build` | Offline wheel/sdist build и `distribution verification OK` |
| `make lock-check` | Lock актуален, 106 packages |
| `make docs` | Strict MkDocs build, внутренние ссылки и anchors — OK |
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/docs` | 137 passed, включая семь копируемых примеров M12 |
| `git diff --check` | Exit 0; дополнительно проверены trailing whitespace новых файлов |
| `git status --short --branch`, `git diff --cached --name-only`, `git ls-files --others --exclude-standard -z`, `git check-ignore` | Состав diff проверен; staged files нет; build outputs ignored |

Узкие проверки до полных gates: 17 unit/security regression/control cases,
4 PostgreSQL temporal-precision cases и 409 cases всех затронутых M12 suites —
passed. Связь с findings сохранена в
[отчёте исправлений](M12_security_review.md#m12-final-review-fixes).
Команды узких проверок, полных gates и текущей подготовки:

```bash
UV_CACHE_DIR=/private/tmp/structuraguard-m12-uv-cache uv run --locked --no-sync pytest -q \
  packages/structuraguard/tests/security/validation/test_m12_final_review.py \
  packages/structuraguard/tests/unit/validation/test_m12_temporal_precision.py
UV_CACHE_DIR=/Users/katana/.cache/uv uv run --locked --no-sync pytest -q \
  -W error::sqlalchemy.exc.SAWarning -m database_integration \
  packages/structuraguard/tests/integration/database/test_postgresql_record_validation.py \
  -k timestamp_precision
UV_CACHE_DIR=/Users/katana/.cache/uv uv run --locked --no-sync pytest -q \
  packages/structuraguard/tests/unit/normalization \
  packages/structuraguard/tests/unit/validation \
  packages/structuraguard/tests/contract/normalization \
  packages/structuraguard/tests/contract/validation \
  packages/structuraguard/tests/property/normalization \
  packages/structuraguard/tests/property/validation \
  packages/structuraguard/tests/security/normalization \
  packages/structuraguard/tests/security/validation \
  packages/structuraguard/tests/docs/test_m12*.py \
  packages/structuraguard/tests/integration/test_sqlite_record_validation.py
UV_CACHE_DIR=/Users/katana/.cache/uv make test test-integration test-security test-database test-build
UV_CACHE_DIR=/private/tmp/structuraguard-m12-uv-cache make lint typecheck docs
UV_CACHE_DIR=/Users/katana/.cache/uv uv run --locked --no-sync pytest -q packages/structuraguard/tests/docs
UV_CACHE_DIR=/Users/katana/.cache/uv make lock-check
git diff --check
```

При подготовке использован временный uv cache для локальных checks и основной
cache для offline build/примеров с parser watchdog. Доступ к `/bin/ps`, Docker
и локальным test servers разрешался вне sandbox. Ранее возникшие sandbox/cache
отказы и неверный docs anchor исправлены или проверены повторным успешным запуском;
они не записаны как пропущенные gates. Пять существующих SWIG DeprecationWarning
в main/integration остаются; настройки проверок не ослаблялись.

### Непроверенное и причины

| Сценарий/проверка | Почему не подтверждён |
|---|---|
| Сквозной engine, projection/reverse index, MappingPlan/catalog revalidation, schema 1.3 и новые consumers | Реализации пока нет; standalone tests не закрывают эти критерии |
| Автоматическое объединение stream между уровнями и loader/staging commit/rollback | Coordinator не реализован; запись не входит в поставленный API A–D |
| Другие ОС и версии Python | Локальная среда — macOS/Python 3.12.9; CI matrix здесь не запускалась |
| Production-scale benchmark и абсолютные wall-clock/RSS caps | Проверены конечные budgets и adversarial cases; отдельный нагрузочный benchmark не выполнялся, process isolation отсутствует |
| Реальные LLM API и рабочие БД | Не требуются для deterministic validators; используются fakes, SQLite и временные PostgreSQL 16/18, платные API не вызывались |
| Полный актуальный CVE-аудит всех транзитивных dependencies | В scope review проверены новые dependencies и lock; внешний vulnerability feed не запрашивался |
| Совместимость с будущим состоянием `main` | Remote fetch/merge и создание PR не выполнялись; проверки относятся к текущему checkout |

Полные runtime/build gates не повторялись после изменения только этого плана и
`PROJECT_STATE.md`: production, tests, dependencies и настройки сборки не менялись.
Актуальные результаты после последнего изменения Python-кода приведены выше.

## Риски

- **Совместимость:** schema 1.3.0 и отдельный trace затрагивают M8–M11 consumers.
  До migration tests нельзя выпускать derived data, которое downstream не понимает.
- **DB semantics и TOCTOU:** CHECK expressions, collations, native types и состояние
  строк не полностью выводятся из metadata. Fail-closed outcomes снижают долю
  автоматического acceptance; final transactional gates нельзя заменить preflight.
- **Ресурсы:** schema composition/regex, replay joins, global uniqueness и точная
  Decimal arithmetic требуют конечных work/state caps. All-errors полный только
  внутри этих caps; budget failure никогда не выглядит успешной частичной проверкой.
- **Privacy:** raw/origins, schema keys, rules, DB lookup keys и полные reports могут
  содержать restricted data. Safe summary не включает values/текст/path; hashes
  не являются анонимизацией или удостоверением trusted origin.

Обязательный scope — A–D, включая явные исходы для неподдержанных constraints.
Последующие улучшения: более широкое доказанное покрытие CHECK/regex,
policy-controlled repair orchestration, новые locale/currency families,
external spill stores, DB array/JSON loading, generated-key propagation,
staging/loader и facade ingest. Их отсутствие не скрывается под успешным validation
outcome. Произвольное исполнение кода/SQL остаётся запрещённым.
