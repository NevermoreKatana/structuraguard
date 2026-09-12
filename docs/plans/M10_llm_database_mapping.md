# M10 — LLM Semantic DB Mapper

Статус на 2026-09-11: M10 реализован и локально принят в scope semantic mapping
proposals. Все восемь критериев ниже подтверждены тестами; оба Medium findings
финального review исправлены и покрыты regression tests. Milestone готов
к ручному commit и PR в `main`; проверки перечислены в разделе готовности.
Commit и PR не созданы.

[Приёмка M10](M10_acceptance.md) содержит матрицу критериев, regression findings,
фактические результаты quality gates и непроверенные сценарии.
[Security review M10](M10_security_review.md) описывает найденные угрозы,
исправления и отдельные security regressions текущего diff.

Уточнение реализации от 2026-09-11: последующая задача пользователя включила
entity split по связанным таблицам. `tables.selected_candidate_ids` допускает
несколько таблиц, relation contexts разделяются по target-table pairs, чтобы
проверять цепочки и composite FK. Typed provider/schema/assignment errors
возвращаются без partial proposal, attempts сохраняются в router. Generated-key
propagation и автоматическое создание anchors для join hints не реализуются.
Эти уточнения имеют приоритет над первоначальными предложениями ниже; граница
зафиксирована в [ADR 0020](../adr/0020-llm-semantic-mapping-proposals.md),
текущее поведение — в [руководстве M10](../llm-database-mapping.md).

## Цель

Получать проверяемые semantic mapping proposals для нормализованных сущностей
из ограниченного набора таблиц, столбцов и связей M7–M9 через существующий
LLM layer M6, с вычисленным SDK confidence и явным `NEEDS_REVIEW`.

Основание: каноническое
[ТЗ](https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md),
§10, §11.8–11.9, §12, §14–14.1, §19.5–19.6, §20.2, §20.10–20.11 и M10.
Устаревшая строка «Validation engine … M10» в `docs/codex/SPEC_INDEX.md` исправлена:
milestone соответствует заголовку **M10. LLM Semantic DB Mapper** из ТЗ.

До изменения M9 возвращает только детерминированные field→column кандидаты,
scores, blockers и ambiguity. Отдельный async mapper теперь оценивает
их смысл совместно с table/relation candidates; результат остаётся
предложением для последующего MappingPlan validation.

Обязательный scope: подготовка candidates/prompt, privacy preflight, LLM choice,
локальная проверка ответа, confidence и review state. MappingPlan execution,
DB writes, DDL/DML, staging, loader, подбор `insert/upsert`, SDK ingest facade,
трансформации значений и автоматическое изменение normalized entities исключены.

## Критерии приёмки

- [x] Для каждой допустимой группы связанных entity types формируется один bounded
  prompt. Все targets проходят scope/type/writability checks и top-k pruning
  **до** security scan и provider call; полный catalog/profile не отправляется.
- [x] LLM возвращает только строгий `SemanticMappingDecision` из trusted schema
  registry. Все source/candidate IDs принадлежат именно отправленной группе;
  согласованность tables, columns и ordered FK pairs проверяет SDK.
- [x] Используются M6 provider/router, capabilities, budgets, approval и call metadata.
  Новый provider protocol, vendor adapter или обход router не создаются.
- [x] Classification не понижается после masking. Credentials отсутствуют даже
  в локальном prompt; restricted cloud и запрещённый fallback дают отказ до egress.
- [x] Итоговый confidence вычисляет SDK. Типовые запреты, неизвестный FK, collision,
  непроверенный конкурент или просьба модели о review не снимаются высоким score.
- [x] `no_llm`, пустые candidates, malformed output, отсутствие approval и исчерпание
  бюджета имеют явные исходы. Ошибка LLM не превращается в успешное подтверждение.
- [x] Default tests работают через `FakeLLMProvider` и контролируемый HTTP transport,
  без внешней сети и credentials. SQLite integration подтверждает отсутствие writes.
- [x] M6–M9 API и fingerprints сохраняются; proposal не является `ValidatedMappingPlan`,
  `SecurityApproval` или разрешением на импорт.

Наблюдаемые тесты каждого пункта перечислены в
[матрице приёмки](M10_acceptance.md#m10-acceptance-matrix).
При передаче дополнительно сверены с AST все 37 именованных тестов отчёта.
Последующие timeout/schema-provenance regressions и актуальные прогоны
зафиксированы в [PROJECT_STATE](../codex/PROJECT_STATE.md#m10-review-fix-checks).

## Затронутые контракты

Пути `src` и `tests` ниже означают `packages/structuraguard/src/structuraguard`
и `packages/structuraguard/tests` относительно корня репозитория.

### Существующая основа

| Компонент и файлы | Повторное использование и граница |
|---|---|
| M6: `src/ports/llm.py`, `contracts/reports.py`, `contracts/llm.py`, `llm/router.py` | `LLMProvider`, `LLMRequest/Response`, уже существующий purpose `semantic_mapping`, `ProviderCapabilities`, `PolicyAwareLLMRouter`, `LLMBudget`, `LLMCallRecord`. Router — coordinator, он не реализует `LLMProvider` |
| M6: `src/llm/_structured.py`, `llm/openai_compatible.py`, `llm/fake.py`, `llm/no_llm.py` | Экспортированные `LLMPromptTemplate`/`LLMResponseSchema`, native JSON Schema либо JSON object mode с локальной validation, temperature 0, typed errors, fake и disabled behavior |
| Security: `src/ports/security.py`, `contracts/reports.py` | Внедряемый trusted `SecurityScanner`, `SecurityScanRequest`, `SecurityReport`, exact-payload `SecurityApproval`. `PIIClassifier` M8 не заменяет scanner |
| M7: `src/contracts/database.py`, `domain/database_fingerprint.py` | Catalog schema 1.1.0 / `catalog-v1`, canonical fingerprint, точные refs, FK graph с ordered composite pairs, SCC/self-reference/join hints; graph не доказывает grants и наличие parent rows |
| M8: `src/contracts/profiling.py`, `profiling/profiling.py`, `profiling/pii.py` | Завершённый `NormalizedDataProfile` schema 1.0.0: типы, patterns/coverage, identity hints, `FieldRelationship`, PII state. Labels/extrema могут содержать PII, даже когда `ProfileExample.value=None` |
| M9: `src/ports/mapping.py`, `mapping/mapper.py`, `mapping/_inputs.py`, `mapping/_results.py` | `CandidateMapper.rank` / `DeterministicMapper`, bounded preflight, type/policy blockers, шесть сигналов, `base_score` отдельно от штрафов, competitor count/gap до top-k, `MappingScope` |
| M9: `src/contracts/deterministic_mapping.py`, `contracts/semantic_catalog.py` | Неизменные `MappingWeights`, `FieldCandidates`, `DeterministicMappingResult`, `DatabaseSemanticCatalog`; descriptions — недоверенные данные, identity/allowed operations — hints |
| M2: `src/contracts/mapping.py` | Существующие `MappingCandidate`, `MappingPlan`, validation DTO остаются без изменения wire format. `normalized_fingerprint` связывает manifest, а не content hash M8 |

Сохраняются решения [ADR 0011](../adr/0011-llm-provider-foundation.md),
[0012](../adr/0012-policy-aware-llm-routing.md),
[0017](../adr/0017-canonical-catalog-and-dependency-graph.md),
[0018](../adr/0018-bounded-normalized-profiling.md) и
[0019](../adr/0019-deterministic-mapping-candidates.md).

### Добавления M10 и совместимость

- `src/contracts/semantic_mapping.py`: immutable `SemanticMappingOptions`,
  `SemanticMappingContext`, `SemanticMappingCandidateSet`, table/relation candidate
  DTO, `SemanticMappingDecision` и `SemanticMappingResult` с group outcomes.
  Column candidates ссылаются на существующие `MappingCandidate` и explanations;
  M9 weights/score не перезаписываются.
- `src/mapping/semantic.py`: `LLMSemanticMapper.propose` принимает готовые profile,
  catalog, обязательный scope и optional semantic catalog. Внедряются существующий
  candidate mapper, trusted scanner, router и run context; constructor без I/O.
  Mapper вызывает `rank` один раз, затем последовательно обрабатывает группы.
  Новая абстракция provider, новый registry providers и sync facade не нужны.
- Новые helpers внутри `mapping`: `_semantic_candidates.py`, `_semantic_prompt.py`,
  `_semantic_validation.py`, `_semantic_confidence.py`. Contracts/domain не
  импортируют LLM infrastructure; provider lifecycle принадлежит composition root.
- Result хранит source/extraction/parse-plan/manifest/content/profile fingerprints,
  DB target/policy/scope/hints/ranking options bindings, candidate-set и prompt/schema
  version/hash, classification/redaction/routing fingerprints, SDK contributions,
  причины review и безопасные `LLMCallRecord`. Full DTO чувствителен; `safe_summary()`
  содержит только `groups`, `ambiguous`, `classification`, `status` и `action`.
- Пример entity-oriented `MappingPlan` из §14 шире текущего field-only DTO.
  M10 возвращает отдельный proposal и не формирует `MappingPlan`: иначе пришлось бы
  потерять relation decisions или самовольно выбрать load operation. Преобразование
  proposal в план и независимая validation остаются следующему этапу.
  Новых dependencies и миграций БД нет. Долгоживущая граница закреплена
  принятым [ADR 0020](../adr/0020-llm-semantic-mapping-proposals.md).

## Шаги

### 1. Определить группу и candidate set до LLM

1. До serialization/hash ограничить размеры входов, затем повторно проверить DTO,
   catalog fingerprint и все bindings M8/M9/scope/hints, включая forged
   `model_construct/model_copy`. Проверить соответствие каждого candidate source,
   target, explanation и classification; hashes не удостоверяют происхождение.
2. Группировать **типы сущностей**, а не отдельные records: вершины — `entity_type`,
   рёбра — межтиповые `parent_child` из M8. Связная компонента даёт одну группу;
   изолированный тип — отдельную. Self-reference сохраняется внутри группы.
   `co_occurrence` используется как evidence внутри группы, не объединяет весь
   dataset и не объявляется FK. Сортировка по exact refs стабильна.
3. Column candidates — top-k M9 для каждого поля после allow-minus-deny,
   исключения system/non-writable/generated targets и доказанных type mismatches.
   Default k=5, ceiling=10; M9 `competitor_count`, `tie_count`, gap и blockers
   сохраняются даже при k=1. Не запрашивать у модели недостающие targets.
4. Table candidates для entity type — только таблицы из объединения его column
   candidates. Base score таблицы: среднее лучших M9 `base_score` её колонок
   по **всем** полям entity type; отсутствие кандидата даёт 0 без перенормировки.
   Stable top-k по score, qualified name, ID. Затем оставить только column
   candidates этих таблиц; потерю покрытия/конкурентов отметить явно.
5. Relation candidates строить из source `parent_child` evidence и существующих
   edges M7 между retained tables. Хранить source field pairs и полный ordered
   состав catalog FK; обе стороны и все компоненты проходят metadata scope.
   Generated parent key допустим лишь как уже разрешённое graph evidence, не
   writable column target. Не угадывать FK по co-occurrence, имени или одному
   компоненту composite key. Base score — минимум M9 base scores подтверждённых
   field→column anchors; неполный набор остаётся unresolved без selectable relation.
6. Для одного source relation context возвращать до k=5 (ceiling 10) вариантов,
   stable tie-break по FK ID и ordered pairs. Возможные join-table paths состоят
   только из двух проверенных FK существующего M7 join hint, с теми же scope/
   coverage checks. Цикл/self-reference всегда требует будущей явной стратегии.
   Перебор ограничен work budget; полный Cartesian product не материализуется.
7. Сформировать immutable lookup: короткий opaque source/candidate ID → точные
   refs и evidence. Внешние имена не используются как управляющие identifiers.
   Hash candidate projection связывает prompt/response, а локальный fingerprint
   полного candidate set дополнительно связывает lineage и scope. Модель не
   может расширить этот набор; `suggest_unlisted_candidate` в M10 отсутствует.

Новые budgets допускают только сужение ceilings:

| Ресурс | Default / ceiling |
|---|---|
| Группы на run | 32 / 64 |
| Entity types / fields / relation contexts в группе | 8 / 8; 32 / 32; 32 / 32 |
| Top-k таблиц на тип, колонок на поле, связей на context | 5 / 10 для каждого |
| Canonical payload одной группы / response | 64 KiB / 128 KiB; 64 KiB / 128 KiB |
| Имя / description; aliases на target | 256 bytes / 256 bytes; 512 bytes / 1024 bytes; 4 / 8 |
| Examples на поле / один example | 2 / 2; 128 bytes / 128 bytes |
| Candidate preparation work / retained state / result | 100 000 / 1 000 000 операций; 16 / 32 MiB; 8 / 16 MiB |
| Общая длительность propose, включая scanner | 30 / 300 секунд; более короткий M6 deadline также обязателен |

Проверить shape всех групп до первого provider call. Слишком большую компоненту
не разрезать молча и не терять поля: `MAPPING_LIMIT_EXCEEDED`, без частичного
proposal. Descriptions можно сокращать по UTF-8 границе с явным счётчиком;
поле/candidate/FK pair не обрезается. Token/context limits, schema и system/wire
overhead дополнительно проверяются M6 для конкретного deployment. Unknown limits
не означают бесконечность. Нужны cancellation checkpoints и общий monotonic
deadline подготовки/scan/calls; router budget не перезапускается между группами.

### 2. Подготовить bounded prompt и применить privacy policy

- Trusted `LLMPromptTemplate` с отдельными ID/version/hash описывает только выбор
  из candidate set и возврат schema. Source names, aliases, comments, descriptions,
  examples и relation evidence сериализуются в отдельный JSON data envelope как
  недоверенные данные; они никогда не интерполируются в system text.
- Проекция содержит только поля данной группы: canonical type, pattern categories
  и coverage, null/unique ratios с exact/estimated, безопасные labels, top-k
  candidates и разрешённые PK/FK hints. Не сериализовать весь DTO через
  `profile.model_dump()`/`catalog.model_dump()` в prompt. Не включать extrema,
  raw records, file paths/provenance, enum labels без проверки, default/check/index
  SQL expressions, credentials или connection configuration.
- В M10 raw value examples не отправляются. Из M8 masked examples доступны лишь
  kind/ordinal; допустимы явно синтетические placeholders вроде `[EMAIL_1]` или
  `[PERSON_1]`, а не восстановленные значения. Names/aliases/comments/descriptions
  также проходят masking/scan; небезопасный label заменяется opaque ID либо
  исключается с отметкой недостаточного evidence. Matching M9 остаётся локальным.
- Минимальный класс — максимум trusted run classification, M8 profile/field PII
  classes и classification target metadata/semantic catalog от host policy.
  `unknown`/неполное покрытие либо отсутствие classification DB metadata не
  трактуется как PUBLIC: default RESTRICTED и review до уточнения trusted policy.
  M8 classifier не покрывает произвольные DB comments и все пользовательские
  PII patterns; этот scan обязан обеспечить внедрённый `SecurityScanner`.
- Masking не понижает класс. Credentials/DSN/API keys/password/private keys
  удаляются или блокируют запрос независимо от locality. Минимальная версия не
  хранит обратную подстановку значений; если она потребуется host application,
  это отдельное защищённое хранилище с TTL, недоступное модели и logs.
- После pruning, masking и любых сокращений вызвать `SecurityScanner.scan` на
  **точном canonical payload**, purpose `llm_input`. Использовать source fingerprint
  как `content_fingerprint`; payload содержит безопасную проекцию обоих источников,
  полный candidate-set binding хранится локально. Проверить report/run/payload/
  classification/redaction/routing identity и лишь затем создать `SecurityApproval`.
  Нет scanner/allow report — нет LLM call; fingerprint не заменяет фактический scan.
- Suspicious content блокируется без raw snippet в ошибке. Blocked report,
  отсутствие approval или неверные bindings дают `LLM_POLICY_DENIED` без egress
  и partial proposal; timeout scanner даёт `LLM_TIMEOUT`. Detection эвристична:
  реальные controls — ограничение
  полномочий, strict schema, candidate membership и deterministic validation.

### 3. Повторно использовать M6 transport и router

- Создавать `LLMRequest` с purpose `semantic_mapping`, response schema
  `semantic-mapping-decision` версии `1.0.0`, trusted prompt identity, exact approval
  и byte bound. Зарегистрировать prompt/schema через существующие M6 exports.
  Все objects schema закрыты и required: M6 registry не принимает optional defaults.
- Вызвать `PolicyAwareLLMRouter.generate_structured` один раз на группу. Все группы
  одного run используют **тот же router**; call/token/deadline reservations и
  история общие. Не оборачивать router в фиктивный `LLMProvider` и не создавать
  по router на поле/группу. Фактический provider/model брать из response/call records.
- Structured output, purpose, locality и известные context/input/output limits
  обязательны. Native JSON Schema используется при capability; иначе существующий
  JSON object mode и та же локальная strict validation. Tool-calling не включается,
  даже если deployment заявляет эту capability. Credentials выбранного provider
  остаются только в отдельных transport headers, отсутствуют в prompt/DTO output.
- Сохраняются `fixed`, `no_llm`, `local_only`, `privacy_first`, `fallback`.
  RESTRICTED → только явно разрешённый local; privacy_first запрещает также
  CONFIDENTIAL cloud. UNKNOWN locality закрывает egress. Fallback повторно
  проверяет exact request/caps/policy; маскирование и classification не меняются.
- Одна логическая заявка может иметь несколько M6 attempts только при timeout,
  rate limit или unavailable и наличии следующего разрешённого provider. Нет
  собственного retry/repair, повторного prompt по полям или fallback после schema/
  policy error. Usage, reservation, latency и fallback reason видимы в metadata.
  Отмена распространяется без fallback; budget не возмещается после ошибки.

### 4. Определить strict `SemanticMappingDecision` и проверить ответ

Все перечисленные поля обязательны, включая nullable. `FrozenContract`,
`extra='forbid'` на каждом объекте, finite bounded collections, strict JSON
validation без coercion/default filling; внешние `$ref` запрещены.

| DTO / поля | Контракт |
|---|---|
| Root `SemanticMappingDecision` | `schema_version` literal `1.0.0`, `group_id`, `candidate_set_fingerprint` безопасной проекции, `tables`, `columns`, `relations`, `review_required` strict bool |
| Каждый элемент tables/columns/relations | `source_id`, `status` из `selected / ambiguous / unmapped`, required nullable `selected_candidate_id`, полный `assessments` переданных этому source candidates, `reason_code` |
| Каждый assessment | `candidate_id`, `semantic_score`, `reason_code`; ID уникален в своём наборе |
| `semantic_score` | Decimal wire string ровно с шестью дробными знаками от `0.000000` до `1.000000`; bool/JSON number/exponent/NaN/Infinity запрещены. После проверки SDK переводит строку в Decimal |
| `reason_code` | Закрытый vocabulary v1: `SEMANTIC_MATCH`, `ENTITY_CONTEXT`, `RELATION_CONTEXT`, `INSUFFICIENT_EVIDENCE`, `MULTIPLE_PLAUSIBLE_TARGETS`, `NO_MATCH`. Нет свободного reasoning, SQL или иных текстовых полей |

Для каждого source требуется ровно один элемент, даже при пустом candidate set.
`assessments` содержит каждый переданный candidate ровно один раз (0–10);
selected требует ID из этого списка, ambiguous/unmapped требуют null.
Размеры tables/columns/relations ограничены 8/32/32; IDs — короткие opaque tokens
из локального lookup. Output не содержит target names, новых source refs,
transformations, operation, SQL, credentials, tools или final SDK confidence.

Порядок локальной проверки: response byte/depth/numeric bounds → JSON parser,
отвергающий duplicate keys/trailing content/NaN → strict schema → request/schema/
prompt/provider bindings M6 → exact group/candidate-set membership → семантические
инварианты. Последние применяются также к `FakeLLMProvider` и custom providers,
которые не обещают JSON Schema validation.

SDK сверяет selected columns с выбранной table для entity type, uniqueness
source/target assignments, scope/type/writability, FK direction и **весь** ordered
composite mapping. Неполный parent/key evidence не становится доказанным FK;
identity hints не разрешают upsert. Cross-group target collisions проверяются
после всех групп. DB catalog может содержать stale snapshot: сверка hash здесь
не заменяет будущий повторный inspection перед load.

Невалидный ответ отклоняется целиком для группы, без использования его отдельных
удачных полей. Schema error сохраняет `LLM_SCHEMA_VIOLATION`; membership/binding
error получает `MappingError` с `SEMANTIC_MAPPING_DECISION_INVALID` и закрытой
причиной. Security violation даёт `REJECTED_SECURITY`; прочие failures не
запускают repair и оставляют группу для review. Raw JSON/exception cause,
Pydantic input fragments и model reasoning не попадают в logs, repr или reports.

### 5. Вычислить confidence, ambiguity и исход run

- Для каждого column candidate взять шесть **исходных** сигналов M9, а не уже
  оштрафованный `MappingCandidate.confidence`. Пусть D — их взвешенная сумма
  `base_score`, L — проверенный semantic score модели. M10 default λ=0.20,
  разрешённый диапазон 0–0.30; сумма семи весов равна 1: старые веса × (1−λ),
  плюс λ для L. Финальный score = clamp((1−λ)D + λL − A − V − S, 0, 1).
  Нет L — нет перенормировки/фиктивного нуля в M9: deterministic result хранится
  отдельно, semantic score отсутствует. Все веса/пороги версионируются.
- Для table/relation candidate D берётся из шага 1, используется та же bounded
  смесь. Это отдельные scores; они не прибавляются повторно к field score.
  Group confidence — минимум scores всех выбранных tables, fields и required
  relation contexts. В M10 все входные поля/parent_child contexts обязательны
  для полного proposal: отсутствующий выбор даёт group confidence 0 и review.
  Это консервативная мера полноты, а не вероятность и не confidence MappingPlan.
- A/V/S — явные penalties с reason codes: default ambiguity 0.10, validation 0.10
  при сохраняющихся blockers, security 0.20 при allowed scan с warning,
  confusable name или неполной классификации (0 при отсутствии risk evidence).
  Collision перепроверяется по assignment и при подтверждении отклоняет ответ.
  Hard security/type/policy/FK violation не
  превращается в компенсируемый штраф. Сохранить structural/type blockers M9;
  `AMBIGUOUS_TARGET` и `TARGET_COLLISION` можно снять только после повторной
  проверки полного relevant candidate set/assignment, не по просьбе модели.
  Штрафы не вычитать дважды. Decimal context/rounding повторяют M9: precision 80,
  ROUND_HALF_EVEN, contributions до 12, опубликованные scores/gaps до 6 знаков.
- SDK ранжирует **все** assessments, считает gap до порогового решения и применяет
  stable tie-break. Несогласие выбора модели с SDK winner, tie, gap < 0.10,
  `ambiguous` или `review_required` модели означает `NEEDS_REVIEW` даже при 1.00.
  Лексикографический tie-break обеспечивает порядок, не снимает ambiguity.
- Pruning не доказывает отсутствие конкурента. Для хвоста M9 использовать
  консервативную верхнюю границу U=min(1, (1−λ)×min(1, D_k+0.000001)+λ):
  D_k — опубликованный base score последнего M9 candidate **до** table pruning,
  добавка покрывает округление M9, скрытый L максимум 1, штрафы минимум 0.
  U округлять вверх до шести знаков. Если winner score−U < margin, auto
  запрещён. При k=1 исходную скрытую ambiguity обязательно сохранить. Дополнительное
  table/relation pruning без доказанной границы также сохраняет review; отсутствие
  числа/покрытия не подменяется нулём. Не менять full-scan алгоритм M9 ради этого.
- Пороги SDK в choice/group/run: score ≥0.90 без blockers и с достаточным margin
  → `auto`; 0.70≤score<0.90 → `confirm`; <0.70 → `reject`.
  Required unmapped → reject; ambiguous без выбора → confirm с confidence 0.
  Hard blocker
  запрещает auto при любом score. Required rejected/unmapped, unresolved relation,
  collision, неполная классификация или непроверенный competitor переводят общий
  proposal в `NEEDS_REVIEW`; blocked security завершает вызов typed error
  `LLM_POLICY_DENIED` без proposal.
- `no_llm` возвращает неизменные M9 candidates с явной причиной disabled и без
  semantic decision. Детерминированный auto_candidate сохраняет свой смысл;
  unresolved группы остаются `NEEDS_REVIEW`. Provider policy/capability/budget/
  availability failure сохраняет typed code и M6 attempts, semantic success нет.
  Ошибка любой группы, cancellation и invalid/over-budget входы завершают вызов
  без частичного return; уже использованные reservations и attempts router
  сохраняются.

Реализованная retention policy: `validated_decision` сохраняет проверенный DTO,
`metadata_only` — только SDK choices/scores и metadata. Raw text не хранится
в обоих режимах, включая error paths. Retention применяется после validation
и не отключает cross-group collision checks. Source values маскируются
необратимо; для ID-only ответа обратная подстановка не требуется.
Security regression suite: `tests/security/mapping/test_semantic_policy.py`
проверяет source name/value injection, PII, retention/candidate escape,
allowed security warnings и confidential/restricted cloud fallback violations.

### Вертикальная последовательность реализации и проверки

Ниже сохранена первоначальная последовательность разработки с предложенными
именами файлов и командами. Это не журнал выполненных проверок; фактические
тестовые файлы и результаты приведены в матрице приёмки и разделе готовности.
Команды выполняются из корня; `uv run --locked --no-sync pytest` — общий префикс.

| Шаг | Файлы и наблюдаемое поведение | Тест и команда после изменения |
|---|---|---|
| A. Контракты | `src/contracts/semantic_mapping.py`, exports в `contracts/__init__.py`; proposed ADR `docs/adr/0020-llm-semantic-mapping-proposals.md`: строгий decision отделён от proposal и существующего MappingPlan | Новый `tests/unit/contracts/test_m10_semantic_mapping.py`: closed/required schema, Decimal limits, legacy roundtrip; `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/contracts` |
| B. Candidates и группы | `src/mapping/_semantic_candidates.py`: bounded M8/M9 composition, table/column/relation top-k и private lookup без LLM | Новый `tests/unit/mapping/test_semantic_candidates.py`: stable grouping, disconnected/oversized components, k=1, composite FK, SCC, join scope; `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/mapping` |
| C. Prompt и policy | `src/mapping/_semantic_prompt.py`: versioned projection, masking, budgets, exact scanner approval | Новые `tests/unit/mapping/test_semantic_prompt.py`, `tests/security/mapping/test_semantic_prompt.py`: wire payload capture, PII/credentials/injection canaries, no egress при отказе; `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/mapping/test_semantic_prompt.py packages/structuraguard/tests/security/mapping/test_semantic_prompt.py` |
| D. Вызов и ответ | `src/mapping/semantic.py`, `_semantic_validation.py`, exports в `mapping/__init__.py`; reuse M6 registry/router, no new provider abstraction | Новые `tests/unit/mapping/test_semantic_mapper.py`, `tests/unit/llm/test_semantic_mapping_contract.py`; общий `tests/contract_suites/llm.py` и existing fakes. Команда: `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/llm packages/structuraguard/tests/unit/mapping` |
| E. Confidence и review | `src/mapping/_semantic_confidence.py`: contributions, pruning bound, recomputed conflicts, group/run outcomes | Новый `tests/unit/mapping/test_semantic_confidence.py`: boundaries 0.70/0.90, margin equality, low D+L=1, hidden tail, отсутствующий L, contradiction, cross-group collision; `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/mapping/test_semantic_confidence.py` |
| F. Сквозная приёмка | Новый `tests/integration/test_semantic_mapping.py`, `tests/security/mapping/test_semantic_decisions.py`; `docs/llm-database-mapping.md`, `mkdocs.yml`, исправление строки M10 в `docs/codex/SPEC_INDEX.md` | M7 SQLite inspection → реальный M8 profile → M9 → fake M6 → M10 с unchanged DB; `make test-integration`, `make test-security`, `make docs`, затем все gates ниже |

### Обязательная матрица provider contract и security tests

| Граница | Проверяемые сценарии |
|---|---|
| Provider contract | Один `semantic_mapping` fixture через `assert_llm_provider_contract`: Fake success/typed failures, NoLLM denial, OpenAI-compatible MockTransport native schema и JSON object mode. Identity/prompt/schema/usage, malformed JSON, extra/missing fields, wrong scalar, Unicode и response limits; semantic membership всегда проверяет mapper |
| Router | fixed/no_llm/local_only/privacy_first/fallback; несколько групп с одним budget; timeout/rate-limit/unavailable, shared reservation/deadline, отмена, concurrent request refusal. RESTRICTED local→cloud и CONFIDENTIAL privacy_first cloud fallback запрещены; UNKNOWN locality/capability/purpose mismatch дают ноль egress |
| Input/privacy | PII и instructions в source field/entity names, labels, extrema, raw samples, aliases, DB names/comments/descriptions/enum labels; email/phone/person/паспорт/ИНН/СНИЛС/card и credential/custom-pattern canaries. Отсутствие этих значений в captured prompt, headers кроме собственного auth transport, errors/logs/repr/history; forged/changed approval, payload, route и masking fingerprints отвергаются |
| Output authority | SQL/DDL/DML/tools/operation/transformations и свободный reasoning как extra fields; SQL/чужие имена в ID; неизвестные/повторные/cross-group IDs, пропуск кандидата, stale candidate set, смена DB target/policy/scope; ни один не расширяет candidate set |
| Relations/constraints | Table-column mismatch, source/target collision, generated/non-writable/system target, partial/reordered composite FK, denied parent component, ложный join hint, self-cycle, unresolved parent identity. LLM score=1 не снимает blockers; отсутствие реального FK evidence остаётся review |
| Resources/no writes | Shape/bytes/depth/числа/operations до allocation/serialization; oversized component без silent split, truncation counters, bounded history, cancellation до/после scan/call. После подготовки SQLite fixture mapper не получает connection; DB write/execute spies не вызваны, schema/data snapshot неизменён |

Обязательные gates **реализации** M10: `make lint`, `make typecheck`, `make test`,
`make test-integration`, `make test-security`, `make docs`, review diff через
`structuraguard-review` и trust boundaries через `structuraguard-security`.
Новых PostgreSQL dialect operations нет: default M10 acceptance достаточно SQLite
и catalog fixtures; при изменении M7 adapter/graph обязательно также
`make test-database` с Docker. Live provider checks — отдельный opt-in, offline
contract suite не подтверждает совместимость конкретного внешнего deployment.
Первоначальная задача ограничивалась планом; последующие задачи включили
реализацию mapper, policy/confidence layer и перечисленные security regressions.

## Готовность к ручному commit и PR {#m10-handoff}

Дата проверки: 2026-09-11. Ветка `feat/m10-llm-database-mapping`, исходный
HEAD `1b12f55`; версия пакета `0.3.0` не менялась. Diff содержит **35 файлов**:
8 изменённых tracked и 27 новых. На этапе подготовки изменена только документация;
production-код и тесты совпадают с состоянием полного прогона после review fixes.

- [x] Все восемь критериев сопоставлены с наблюдаемыми тестами.
- [x] Security review и повторный review исправлений завершены; существенных
  нерешённых findings не осталось. Последние два Medium — deadline последнего
  синхронного этапа и schema provenance — имеют 17 regression cases.
- [x] Ruff, mypy, unit/property/smoke, integration, security и package verification
  выполнены после последних изменений кода; результаты приведены ниже.
- [x] Offline lock check выполнен; production dependencies и `uv.lock` не изменены.
- [x] Проверены все tracked/untracked файлы diff: credential patterns, опасные
  debug-вызовы в production AST, generated/temp paths и whitespace новых файлов.
  Совпадений нет; synthetic canaries остаются частью security tests.
- [x] `PROJECT_STATE.md`, руководство, ADR и ссылки на каноническое ТЗ актуальны;
  история ранних прогонов отделена от результатов финальных исправлений.
- [x] Финальные strict docs и `git diff --check` после правки документации.
- [ ] Ручной commit пользователем.
- [ ] Ручное создание PR в `main` и удалённые CI/review.

### Фактически выполненные команды

Узкие pytest, lint/typecheck и docs запускались с
`UV_CACHE_DIR=/private/tmp/structuraguard-m10-uv`; полный прогон и offline build —
с `UV_CACHE_DIR=/Users/katana/.cache/uv` вне sandbox: существующим тестам нужны
localhost и `/bin/ps`, сборке — уже установленный build backend. Все provider
tests используют Fake/MockTransport; платные API не вызывались.

| Команда из корня репозитория | Фактический результат на текущем коде |
|---|---|
| `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/mapping/test_m10_review_fixes.py -q` | 17 passed; до исправлений 14 failed, 3 passed |
| Узкий состав M10/M9, полная команда в [PROJECT_STATE](../codex/PROJECT_STATE.md#m10-review-fix-checks) | 300 passed |
| `make lint typecheck` | Ruff: 358 файлов, checks passed; mypy: 354 файла, без ошибок |
| `make test test-integration test-security test-build` | 3092 passed / 24 passed / 719 passed; wheel/sdist и isolated install: distribution verification OK |
| `uv run --locked --no-sync pytest packages/structuraguard/tests/docs -q` | 121 passed при подготовке; четыре сценария копируемого M10 example, сеть запрещена audit hook |
| `UV_OFFLINE=1 UV_CACHE_DIR=/Users/katana/.cache/uv make lock-check` | 105 packages, OK; повтор после sandbox panic macOS SystemConfiguration, сеть отключена |
| `make docs` | Финальный strict build: OK; первоначальные пять неверных anchors в handoff-ссылках исправлены без изменения strict validation |
| `git diff --check` | OK при передаче; дополнительно проверен whitespace всех 27 untracked файлов |
| `git status --short`, `git branch --show-current`, `git rev-parse --short HEAD` | 35 файлов, ветка/HEAD указаны выше; commit/PR не создавались |
| Локальная Python-проверка состава diff и AST, `rg` результатов сохранённых прогонов | 37 test references разрешаются; secrets/debug/generated/новые dependencies не найдены; результаты gates сверены с выводом команд |

В default `make test` исключены 84 database tests; у integration marker —
3152 deselected. Пять существующих SWIG deprecation warnings не подавлялись.
Отдельного secret-scanner target в проекте нет: выполнена проверка diff по
шаблонам credentials/private keys/DSN и ручной review; это не гарантия полноты DLP.

### Состав изменений

Все пути относительно корня репозитория. Каталог production —
`packages/structuraguard/src/structuraguard/`, каталог тестов —
`packages/structuraguard/tests/`.

| Каталог | Изменённые или новые файлы |
|---|---|
| Production `contracts/` | `__init__.py`, `semantic_mapping.py` |
| Production `llm/` | `_content.py` |
| Production `mapping/` | `__init__.py`, `_semantic_candidates.py`, `_semantic_confidence.py`, `_semantic_prompt.py`, `_semantic_validation.py`, `semantic.py` |
| Production `structure/` | `plan_compilation.py` |
| Tests `docs/`, `fakes/`, `integration/`, `property/mapping/`, `smoke/` соответственно | `test_m10_examples.py`, `semantic_mapping.py`, `test_semantic_mapping.py`, `test_semantic_properties.py`, `test_mapping_boundaries.py` |
| Tests `security/mapping/` | `test_m10_security_acceptance.py`, `test_m10_security_review.py`, `test_semantic_boundary.py`, `test_semantic_policy.py` |
| Tests `unit/contracts/`, `unit/llm/` соответственно | `test_m10_semantic_mapping.py`, `test_semantic_mapping_contract.py` |
| Tests `unit/mapping/` | `test_m10_acceptance.py`, `test_m10_review_fixes.py`, `test_semantic_confidence.py`, `test_semantic_mapper.py`, `test_semantic_routing.py` |
| `docs/` | `adr/0020-llm-semantic-mapping-proposals.md`, `llm-database-mapping.md`, `plans/M10_llm_database_mapping.md`, `plans/M10_acceptance.md`, `plans/M10_security_review.md`, `codex/PROJECT_STATE.md`, `codex/SPEC_INDEX.md` |
| Корень и `scripts/` | `mkdocs.yml`, `scripts/verify_distribution.py` |

### Пропущенные проверки и причины

- `make test-database`: 84 PostgreSQL cases не запускались. M10 не меняет
  M7 SQL/dialect adapter; согласованный plan допускает SQLite/catalog fixtures.
- Live local/cloud deployments, auth и vendor retention: внешние платные LLM API
  запрещены задачей; contract tests проверяют Fake/MockTransport.
- Production DLP/custom PII recall и model-quality holdout: M10 не реализует
  внешний trusted scanner и не калибрует confidence. Canary/formula/property tests
  проверяют границы SDK, а не качество конкретного scanner/model deployment.
- Полный performance benchmark и все предельные объёмы: выполнялся только bounded
  fixture 32 поля × 1000 columns, k=1 (4.684 s, 35 858 bytes prompt payload).
  Это не нагрузочная гарантия; масштабная калибровка/benchmark отложены.
- MappingPlan validation/execution, live drift/grants/parent rows, DB writes,
  staging/rollback и generated-key propagation исключены из scope M10.
- Удалённый CI и его платформенные матрицы не запускались: commit/PR оставлены
  пользователю. Локальные прогоны не заменяют проверки будущего PR.

## Риски

- Top-k ограничивает recall: правильного target может не быть в prompt. Верхние
  границы и review предотвращают ложную уверенность, но не восстанавливают
  отсутствующего кандидата. Embeddings, reranking вне M9 и unlisted suggestions
  отложены; проверять recall отдельно на offline fixtures.
- M8 PII evidence не является production DLP. Без trusted scanner и classification
  DB metadata облачный вызов не разрешается; masking может убрать полезную
  семантику и увеличить долю review. Универсальный scanner/PII engine не входит в M10.
- Default λ/пороги — начальные эвристики, а не калиброванные вероятности.
  Conservative minimum и неполный FK/profile evidence снижают automatic coverage;
  calibration/benchmark на holdout — последующее улучшение без runtime обучения.
- FK graph и identity hints не подтверждают строки, grants, cycle strategy или
  актуальность живой схемы. Полноценный MappingPlan validator, schema-drift check
  перед load и транзакционные гарантии остаются обязательными будущими границами.
- Proposal поддерживает entity split по нескольким связанным таблицам, но не
  изменяет normalized entities и не восстанавливает отсутствующие relations.
  Такое преобразование/merge, обратная подстановка PII, cache ответов,
  quality-first routing и дополнительные provider adapters отложены.
- Deadline кооперативный: SDK проверяет его между синхронными операциями и перед
  возвратом результата, но не прерывает Python-код посередине операции.
