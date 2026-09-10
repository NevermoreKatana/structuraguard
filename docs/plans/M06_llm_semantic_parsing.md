# M06 — Provider-neutral LLM-assisted semantic parsing

Статус: bounded A–D подготовлены к ручному commit и Draft PR в `main`.
Полная приёмка исходного M6 не завершена: K5/K7 подтверждены частично.
Дата: 2026-09-10.

Актуальный [checklist передачи, состав diff и команды](#checklist-commit-pr).
Commit/push/PR автоматически не создавались.

Проверка текущей рабочей копии: [матрица критериев и тестов](M06_acceptance.md).
Исходные K5/K7 подтверждены частично: production redaction и безопасный aggregate
report целиком не реализованы; зелёные gates не закрывают отложенный scope.

[Security review текущего diff](M06_security_review.md): три Medium проблемы
в error sanitization и run budgets исправлены с security regressions.
Последующие findings финального review и их локальные исправления:
[saved-plan review/deadline и terminal issues](#m06-final-review-fixes).

Фактический API: [providers и routing](../llm.md),
[ADR 0011](../adr/0011-llm-provider-foundation.md),
[ADR 0012](../adr/0012-policy-aware-llm-routing.md). Реализованы общий contract suite,
scripted fake/disabled provider, typed capabilities, prompt metadata, HTTP adapter,
закрытый response schema registry, safe history, пять routing modes и run budgets.
C добавляет [LLMStructureAnalyzer](../llm-semantic-parsing.md), closed proposal schema,
compiler и обязательную physical source validation. Конкретизация grammar, replay
и scoring: [ADR 0013](../adr/0013-validated-llm-structure-analysis.md). Production
PII scanner/redaction и общая SDK/router facade остаются отдельными этапами.
Фактический D API, budgets, score и ограничения: [semantic parsing](../semantic-parsing.md),
[ADR 0014](../adr/0014-hybrid-semantic-parsing.md). Ниже сохранён исходный design;
при расхождении defaults применяется документированный runtime contract. Fake объявляет
`json_schema=False`; реализация registry сама по себе не завершает semantic parsing.

## Цель

Получать из physical extraction M4 проверяемый `ParsePlan` и semantic entities
в трёх режимах, подключая LLM через существующий port, с ограниченными затратами,
проверяемым provenance и явным отказом от автоматического решения при неоднозначности.

## Основание и текущее поведение

Прочитаны `PROJECT_CONTEXT.md`, `SPEC_INDEX.md`, только относящиеся к задаче
разделы ТЗ: FR-014/FR-015, 19.1–19.4, 19.6–19.8, 20.2, 20.9–20.11,
parsing-часть 23.1, 23.5–23.8 и M6. Канонические требования:
[ParsePlan][spec-plan], [режимы][spec-modes], [LLM][spec-llm], [M6][spec-m6].
Строка `Source Profiler … M6` в индексе устарела; scope определён текущей
задачей и заголовком M6 самого ТЗ. Разделы DB mapping не используются.

| Основа | Проверенная реализация и ограничение для M6 |
| --- | --- |
| M2 | `ports/llm.py`: `LLMProvider.generate_structured(LLMRequest) -> LLMResponse`. В `contracts/reports.py` уже есть canonical JSON, byte limits, provider identity и `SecurityApproval`, связанный с payload/classification/routing/redaction. Это не реализация scanner или router. |
| M2 | `ports/semantic.py`, `contracts/parsing.py`: analyzer возвращает недоверенный результат; только независимый validator выдаёт `ValidatedParsePlan`. `SemanticParseReport` уже существует. Не создавать параллельные DTO с теми же ролями. |
| M4 | `parsers/builtin/`: physical lines/blocks/tables/trees и terminal manifest. PDF сохраняет page/block/line/bbox, DOCX — part/path/order, HTML — source DOM; повторные представления одного текста нельзя считать разными сущностями. Binary extraction, detection, parser registry и sandbox остаются ответственностью M4. |
| M5-A | `structure/profiling.py`, `_samples.py`: bounded prefix/tail sampling, coverage и evidence. Raw samples не являются публичным результатом `profile()`; tails могут иметь table anchor, а не отдельный indexed cell ref. |
| M5-B | `structure/analysis.py`, `planning.py`: physical replay обязателен; confidence policy `structural_min_v1`, порог 0.85 включительно. Несколько кандидатов, неполный sampling или неподдержанный scope не дают автоматического плана; business semantics остаются `unresolved`. |
| M5-C | `structure/validation.py`, `execution.py`, `_plan_check.py`, `_runtime.py`: проверка полного replay и повторная проверка при execution. Success manifest появляется после EOF и cleanup; промежуточные batches не подтверждают успех. |

Проверены M2 contract/security tests, общий parser contract suite, M4/M5
исполняемые примеры, `test_analysis.py` и `test_m05_acceptance.py`.
Унаследовать ограничения [ADR 0003](../adr/0003-two-stage-parsing-contracts.md),
[0008](../adr/0008-bounded-structural-profiling.md),
[0009](../adr/0009-deterministic-structure-analysis.md) и
[0010](../adr/0010-verified-parse-plan-execution.md).

После M6 неоднозначный разрешённый источник сможет получить LLM-предложение,
независимую проверку и детерминированное применение. XML element matching,
несколько JSONL roots, optional-missing policy, произвольные LOG grammars и
document table-column selectors не становятся поддержанными автоматически.

## Критерии приёмки

- **K1:** три режима дают результаты по таблице ниже; `deterministic` и
  `no_llm` выполняют ноль provider calls и не требуют HTTP dependencies.
- **K2:** один provider contract suite проверяет fake и HTTP adapter; disabled
  provider имеет проверяемый отказ. Замена разрешённого provider не меняет
  parsing contracts и validation boundary.
- **K3:** повторяющаяся таблица получает один общий план на выбранный scope;
  рост числа строк и изменение batch size не создают вызов на каждую строку.
- **K4:** JSON/schema/reference/plan validation отвергает malformed output,
  неизвестные операции и references, неподдержанную grammar и поддельную lineage.
  Каждый выданный value воспроизводится по physical source; LLM не исполняет plan.
- **K5:** до каждого сетевого запроса проверяются classification, masking,
  разрешённый destination и общие budgets, включая retries и fallback.
  Forbidden cloud fallback, prompt injection и secret leakage имеют regressions.
- **K6:** bounded document chunks дают проверенные entity spans, сохраняют
  отношения и не дублируют overlap. Непрочитанный/неразрешённый остаток виден в report.
- **K7:** `NEEDS_REVIEW`, security rejection, незакрытый scope после exhaustion,
  cancellation и поздняя ошибка не получают успешный normalized fingerprint. Report объясняет routing,
  usage, покрытие и fallback без raw PII, prompts или ответов модели.
- **K8:** старые DTO round-trip и M2/M4/M5 tests сохраняются; новые публичные
  parsing exports и offline examples проверяются в установленном wheel.

## Режимы и момент вызова

Общие prerequisites: завершённый и проверенный extraction, доступный replay
того же snapshot, profile и доверенная immutable policy. Saved/user template
сначала проходит повторную source/schema validation; его применение не вызывает LLM.
Повреждённый source, отсутствие replay или security veto нельзя исправить моделью.

| Режим `ParsingPolicy.mode` | Алгоритм | Когда вызывается LLM | Исход без пригодного LLM-ответа |
| --- | --- | --- | --- |
| `deterministic` | User template или M5 analyzer → validator → executor. | Никогда, даже при настроенном provider. | Проверенный deterministic plan либо `NEEDS_REVIEW`; низкоуровневый `NEEDS_SEMANTIC_ANALYSIS` M5 сохраняется в diagnostics. |
| `llm_assisted` | Режим нового orchestration по умолчанию: profile → deterministic attempt. Один кандидат с достаточным confidence, полным допустимым scope и успешной validation завершает выбор. | Только при структурной неоднозначности, недостаточных правилах/confidence либо явно запрошенных semantic targets, оставшихся unresolved. Передаются profile projection, candidates и sample. | Policy-safe provider fallback; затем пригодный deterministic plan, иначе `NEEDS_REVIEW`. |
| `llm_first` | После обычного technical parser и profiler модель первой предлагает структуру/извлечение для prose, нестандартных LOG и документов. | Один bounded plan request либо по одному request на bounded document chunk; для повторяющихся таблиц всё равно общий plan. | Та же validation и deterministic fallback; неоднозначность или незакрытый scope дают `NEEDS_REVIEW`. |

Достаточный структурный результат M5 с `unresolved` business hints сам по себе
не требует LLM: caller должен явно задать обязательные semantic extraction targets.
Не менять default `StructureAnalysisRequest.mode=deterministic` и поведение M5;
default `llm_assisted` вводится только в новой `ParsingPolicy`.
Без настроенного provider эта policy не создаёт cloud client автоматически.
Для tabular/tree `llm_first` означает приоритет предложения модели, но не
построчное semantic extraction; предложение применяется существующим executor.
В `llm_assisted` document chunk extraction разрешено после неудачного
deterministic attempt для явно запрошенных document targets; это та же policy
с deterministic-first порядком, а не смена режима во время run.

## Затронутые контракты

Пути исходников ниже относительны к `packages/structuraguard/src/structuraguard/`,
тестов — к `packages/structuraguard/tests/`. Новые файлы обозначают работу M6,
а не уже доступные exports.

| Контракт | Изменение и совместимость |
| --- | --- |
| `ProviderCapabilities` | Добавить JSON Schema/tool-calling capabilities, `execution_environment` (`local`, `cloud`, `unknown`, `disabled`), конечные input/output/context token limits и разрешённые schema versions. Tool-calling capability не даёт права передавать tools. Local — доверенная deployment configuration, не догадка по URL. Unknown не считается local. Старые byte limits сохранить. |
| `NoLLMProvider` | Явная disabled capability: `structured_output=False`, пустые purposes только при `disabled`. Для действующего provider прежние требования сохраняются. Прямой вызов даёт typed `LLM_POLICY_DENIED` с reason `llm_disabled`, а не пустой успешный JSON. Router вообще не вызывает disabled provider. |
| `LLMRequest` / `LLMResponse` | Сохранить port и purpose `semantic_parsing`. Добавить versioned metadata token reservation/usage origin, prompt identity, schema fingerprint и attempt identity. Legacy constructors/serialization не получают лишних пустых полей. Отсутствующие usage нельзя представлять как известный ноль. |
| `ParsingPolicy`, `LLMPolicy`, budgets | Новые immutable DTO в `contracts/llm.py`, публичный re-export `ParsingPolicy` из `parsing`. Parsing mode и routing mode — разные axes. Per-run настройки только сужают ограничения trusted owner. |
| Analysis | `LLMStructureAnalyzer` и `HybridStructureAnalyzer` реализуют существующий `SemanticStructureAnalyzer`. Replay приходит через явно внедрённый port, а не через provider request. Пустой source обрабатывается до создания обязательного непустого `StructureAnalysisRequest`. |
| `SemanticParseReport` | Versioned extension: mode, profile fingerprint, attempts, budgets, coverage, unresolved scope, fallback и prompt/schema metadata. В новой версии `parse_plan_fingerprint=None` допустим до создания плана; legacy 1.0.0 и completed outcomes по-прежнему требуют настоящий plan fingerprint. |
| Document spans | Новая закрытая selector operation и provenance extension требуют ParsePlan/normalized schema 1.2.0; 1.0.0/1.1.0 продолжают читаться без новых полей. Старые consumers явно отвергают 1.2.0. Нет миграций БД или rewriting сохранённых plans. |

`StructureNeedsReview` сейчас требует непустых candidates. Не выдумывать кандидата
для пустого source: сохранить `StructureNeedsSemanticAnalysis` на низком уровне,
а новый итоговый report переводит его в `NEEDS_REVIEW` с причиной и без plan hash.
`PlanDerivation` M5 требует `AnalysisScore` и непустых unresolved fields:
не помещать туда фиктивный deterministic score для LLM. Ввести отдельную versioned
LLM derivation с source/profile/request/schema/prompt/generation fingerprints.

### Structured responses и независимая проверка

Две локально зарегистрированные JSON Schemas, версия `1.0.0`; обе имеют
discriminator результата, `additionalProperties=false` на каждом объекте,
конечные длины строк/списков, enum operations и confidence в `[0, 1]`.

| Schema ID | Содержимое |
| --- | --- |
| `semantic_parse_plan` | `plan_proposal` либо `needs_review`; family (`tabular`, `tree`, `log`, `document`), candidate/scope IDs, fields, entities/parent links, selector choices, bounded boundary parameters, evidence IDs, semantic/locale hints, unresolved IDs и enum reason codes. Review не содержит исполняемого plan. |
| `document_entity_extraction` | `entities` либо `needs_review`; chunk ID, bounded entities/fields/relations, для каждого value — список span references, sanitized exact quote, confidence и evidence; отдельно unresolved regions. Нет поля для свободно сгенерированного normalized value. |

Выбрать provider wire projection существующей ParsePlan grammar: модель возвращает
строгую декларативную структуру с opaque IDs, локальный compiler восстанавливает
канонический `ParsePlan`. Это нужно для минимизации PII и совместимости с M2:
его JSON boundary запрещает, например, ключ `token_index`. Wire использует
`selector_id`/закрытые безопасные параметры, не ослабляет forbidden-key checks.
Raw source keys передаются безопасными descriptors после masking, не JSON keys.
Schema/domain parity проверяется contract tests, второй executable DSL не создаётся.

Compiler устанавливает source/extraction/profile fingerprints, producer, revision
и plan fingerprint из проверенного контекста, не доверяя таким значениям модели.
Возможности выбора ограничены переданным source catalog: anchors, columns,
literal paths, selectors, candidate regions и допустимые диапазоны. Динамические
spans разрешены только внутри реально переданных chunks. Semantic names —
bounded data, не target DB identifiers. DatabaseCatalog/MappingPlan здесь не нужны.

Последовательность: ограничить HTTP bytes и JSON depth/items → разобрать ровно один JSON object без
duplicate keys/NaN/Infinity/trailing text → strict schema → проверить request,
schema, prompt и фактический provider/model → проверить catalog membership и
значения по replay → скомпилировать plan → `ParsePlanValidator.validate_source`
→ `ParsePlanExecutor`. Truncated/refusal/tool-call responses не считаются успехом.
Модель не выдаёт `ValidatedParsePlan` и не подтверждает собственный ответ.
Self-confidence — advisory; acceptance также требует единственного непротиворечивого
решения, закрытых required targets, coverage и всех deterministic checks.
Locale/type hints не запускают conversions, timezone guessing или money coercion.

## Bounded samples, chunks и provenance

1. Переиспользовать M5 проверяющий проход; добавить явный bounded replay port,
   открывающий новый stream того же extraction. Caller владеет snapshot lifetime;
   каждый consumer закрывает свой iterator. Нет `list(all_batches)`, `tee` с
   неограниченным буфером или повторного parser run с новыми IDs вместо replay.
   Default tests используют маленький bounded in-memory replay; storage backend
   и полноценный source lease orchestration не входят в M6.
2. Сделать отдельный fingerprinted `SemanticSample`/`DocumentChunk` projection,
   не выдавая его за прежний `PhysicalSample`. Публично переиспользовать проверенный
   snapshot builder M5 без зависимости analyzer от приватного `_inspect`.
   Связать source, extraction, исходный и derived profile, batch hashes и sample policy.
3. Для tabular выбирать headers, начало/хвост data region, repeated headers,
   ragged/type variants и по представителю каждого сохранённого candidate scope.
   Для tree — repeated roots и parent/child paths; для LOG — начала, continuation
   и варианты records. Использовать retained prefix/tail M5 и один bounded
   дополнительный replay для адресных окон, без RNG и неограниченного reservoir.
   Если top-k/byte budget исключил scope, это видно в coverage и блокирует его
   автоматическое принятие. Нельзя выбрать произвольного победителя при top-k overflow.
4. Для больших таблиц передавать physical schema, counts, bounds и минимальные
   примеры, затем проверять выбранный конечный scope на всём replay. Anchor +
   row/column разрешён лишь после проверки реальной координаты и batch ownership;
   новый indexed cell ID не выдумывается. Это допускает LLM-план при неполном
   sampling, но не отменяет M5 запрет deterministic auto-plan при sampling gaps.
5. Документы делить в source order по sections/blocks/pages и целым LOG records.
   Oversized prose block делить по предложениям, затем bounded character spans;
   сохранять исходные offsets, не резать surrogate/encoding boundaries и PII placeholders.
   LOG record сверх лимита не разрывать в выдуманные records — unresolved/review.
   Overlap — до одного соседнего блока и не более 256 tokens, входит в бюджет.
6. Каждый chunk содержит opaque segment ID, physical ref, original location,
   batch hash, диапазон Unicode code points `[start, end)` и mapping sanitized
   offsets → original offsets. Original reference обязан существовать в replay,
   даже если его нет в ограниченном manifest index. Такой selective proof/index
   — явное versioned расширение request/validation, не ослабление legacy membership.

После ответа quote обязан точно совпасть с sanitized span; внутри trust domain
проверяется обратное отображение на оригинал. Placeholder нельзя разрезать или
подставлять из другого chunk/run. Значение восстанавливает executor из original
source; raw parent, все `origins`, page/block/line/cell/path и span сохраняются.
PDF lines проецируются по правилам M5 с отдельными origins, DOCX сохраняет part/path;
повторные HTML DOM/block views сводятся по physical ownership.

Для prose выбрать компиляцию в `DocumentParsePlan` с закрытым `document_span`
selector: конечные refs/ranges, операция копирования/соединения spans с явным LF,
без вычисляемых выражений. Selector связывает каждый record group с его spans;
отсутствующая/дублирующаяся привязка отклоняется, spans другого record не копируются.
Группы и parent links конечны, без циклов и cross-source
ссылок; единственный plan может содержать все ограниченные chunk groups.
Exact duplicates overlap устраняются по original refs/ranges + field/entity role;
похожие значения в разных местах сохраняются. Конфликт labels, неоднозначная
cross-chunk relation или entity за пределами доступного контекста дают review.
Модель не сливает сущности отдельным неограниченным merge-call.

## Context, token, call и time budgets

Ниже предлагаемые defaults M6, а не обещание параметров текущего кода. Эффективный
лимит — минимум trusted security policy, run policy, provider capabilities и
существующих parser/profiler/executor caps. Размеры считаются до materialization.

| Лимит | Default M6 и правило |
| --- | --- |
| На run | 10 provider attempts, 50 000 input + output tokens суммарно, 300 секунд общей обработки; caller может уменьшить. |
| Одновременно | 1 запрос на run; provider adapter также имеет конечный instance concurrency limit. Нет глобального mutable ledger. |
| Plan sample | 100 values, 65 536 UTF-8 bytes с refs; top-k candidates 8, максимум 32 при явной policy. Oversized values пропускаются с coverage reason. |
| Один request | До 8 192 input tokens, до 2 048 output tokens; до 131 072 input bytes с prompt/schema и 65 536 response bytes. Все transport/envelope overhead учитываются отдельно от sample bytes. |
| JSON envelope/output | Глубина до 32, до 10 000 nodes; bounded decoder отвергает превышение до создания глубокого object graph, включая HTTP error body. |
| Document chunks | До 8 chunks, до 2 048 input tokens текста на chunk вместе с overlap; до 64 spans на value, 32 entity types и 1 000 record groups на plan, также действуют меньшие M5 caps. |
| Context window | `prompt + response schema + sanitized payload + protocol overhead + reserved output + 256 safety tokens <= effective_context_limit`. Ни schema, ни system message не бесплатны. |
| Retries/fallback | Одна transient retry и не более одного следующего provider на логический request; каждый attempt расходует общий calls/tokens/deadline budget. Default repair — 0. |
| Один attempt | До 30 секунд в пределах оставшегося run deadline; backoff с jitter и bounded Retry-After входит в общий deadline. Cancellation не запускает retry. |

До отправки резервировать input estimate + полный output cap и одну попытку.
Использовать точный tokenizer либо проверенную консервативную верхнюю оценку для
настроенного endpoint; неизвестный context limit/непроверяемая оценка запрещает
вызов (`LLM_CONTEXT_LIMIT`/capability mismatch), а не означает бесконечный context.
Не доверять оценке `len(text)/4`. После ответа сохранять actual usage; если usage
неизвестен, удерживать reservation и отмечать `usage_origin=reserved`. Timeout
после отправки тоже не возвращает потраченный call/token reservation.

Основной tabular/tree plan request — один на scope. Retry — повтор попытки того
же запроса, не обработка следующей строки. Executor не имеет provider dependency.
Run-local memo по source/profile/scope/policy/prompt/schema исключает повторный
анализ одинакового scope. Budget chunk loop общий, не обнуляется на batch/chunk
или смене provider. Exhaustion прекращает новые calls; остаток учитывается как
unresolved. Нельзя объявить весь документ обработанным по первым восьми chunks.

## Prompt versioning и trust boundaries

- `llm/prompts/` содержит неизменяемые шаблоны `semantic_parse_plan/v1` и
  `document_entity_extraction/v1` и локальные response schemas. Prompt ID/version,
  hash текста, schema hash, sampling/redaction policy и generation parameters
  входят в request derivation/report. Изменение semantics шаблона меняет version.
  Исполнение сохранённого проверенного plan не требует прежней доступности модели.
- System instructions принадлежат SDK; source, headings, field labels, profile
  descriptions и model output — недоверенные data sections. Escaping/delimiters
  не заменяют deterministic controls. Provider получает только минимальный
  sanitized projection, без полного source, paths к файлам, DB handles и tools.
- Перед сетью trusted `SecurityScanner` проверяет точный payload. `SecurityApproval`
  перепроверяется вместе с payload/classification/routing/redaction fingerprints;
  routing fingerprint включает provider/model/destination. Hash сам по себе не
  доказывает полномочия caller: scanner/policy/provider внедряет trusted owner.
- Искать injection также в profile labels, HTML hidden/active content и ответах.
  Detected injection не превращается в allowed approval: это запрещено M2.
  Подтверждённая опасная попытка даёт `REJECTED_SECURITY`; неопределённый риск
  даёт `NEEDS_REVIEW` без вызова. Security events содержат codes и bound hashes.
- Модель не меняет routing, budgets, prompt versions или response schema. SQL,
  code и URL в тексте не исполняются и не открываются. Никаких tool definitions,
  function calling, shell, filesystem или DB capability, даже если endpoint их умеет.

### PII routing и redaction

Классификация до минимизации — максимум trusted caller classification и локально
обнаруженной чувствительности. Маскирование не понижает её автоматически.
Проверять и значения, и названия колонок/paths/headings; source identifiers в wire
заменять opaque aliases. Локальные правила покрывают ФИО, email, телефон, паспорт,
ИНН, СНИЛС, карту, API keys/tokens/password/private key; uncertainty не означает PUBLIC.
Scanner расширяется trusted adapter через существующий port. Пользовательские
patterns допускаются только с ограниченным безопасным matcher и scan budgets.

| Classification | Default route при явно настроенных providers |
| --- | --- |
| `PUBLIC` | Разрешённый local/cloud destination после scan и удаления secrets. |
| `INTERNAL` | Local либо явно одобренный для INTERNAL cloud; не любой cloud. |
| `CONFIDENTIAL` | Local в разрешённом trust domain; cloud по умолчанию запрещён. |
| `RESTRICTED` | LLM запрещён; отдельная trusted policy может разрешить конкретный local trust domain. Неявного выхода наружу нет. |

PII заменяется typed placeholders (`[PERSON_1]`, `[EMAIL_1]` и т. п.) с отдельной
run-local bounded map. Map не попадает в provider/log/report, очищается при
завершении/cancellation/expiry (не дольше run deadline); persistent map/store не
входит в M6. Secrets не отправляются ни cloud, ни local и не восстанавливаются
из model output. Для extraction PII восстанавливает trusted executor по spans,
а не глобальная текстовая замена в ответе.

Router поддерживает `fixed`, `local_only`, `privacy_first`, `fallback`,
`quality_first`, `no_llm`. Сначала фильтр classification/destination/capabilities,
затем fixed choice или явный порядок подходящих providers; quality order задаёт
owner, без benchmark/model-based reranking. `fixed` не переключает provider;
`no_llm` не вызывает ни scanner egress, ни provider. Для каждого fallback заново
проверяются destination, redaction, exact payload approval и оставшиеся бюджеты.
Допуск primary local не разрешает cloud fallback.

## Ошибки, deterministic fallback и report

Нормализовать `LLM_TIMEOUT`, `LLM_RATE_LIMIT`, `LLM_AUTHENTICATION_FAILED`,
`LLM_UNAVAILABLE`, `LLM_INVALID_RESPONSE`, `LLM_SCHEMA_VIOLATION`,
`LLM_CONTEXT_LIMIT`, `LLM_POLICY_DENIED`, `LLM_UNKNOWN_SOURCE_REFERENCE`;
добавить `LLM_CAPABILITY_MISMATCH` и `LLM_BUDGET_EXCEEDED` как typed codes.
`LLM_UNKNOWN_TARGET_IDENTIFIER` относится к последующему DB mapping, не к M6.

| Ситуация | Реакция |
| --- | --- |
| Timeout, rate limit, transient transport/5xx | Bounded retry, затем только policy-approved fallback; отмену caller распространять после cleanup. |
| Authentication, capability/context mismatch | Не повторять тот же запрос; отдельный разрешённый provider возможен по routing policy и бюджету. |
| Malformed JSON/schema, unsupported operator, unknown ref, fabricated quote | Не исполнять, не принимать частично, не делать repair по умолчанию; сохранить безопасную issue. Допустим следующий разрешённый provider, если нет security veto. |
| LLM недоступна/запрещена или budget исчерпан | Уже проверенный deterministic/template plan пригоден только при том же snapshot, полном требуемом scope и закрытых required targets; иначе `NEEDS_REVIEW`. Порог не снижается. |
| Несколько допустимых трактовок, missing required target, непроверенный required scope после replay, conflicting entities | `NEEDS_REVIEW`, ranked alternatives и безопасные причины, без автоматического execution. |
| Security veto, повреждённый source/fingerprint, operational source limit | `REJECTED_SECURITY` либо typed `FAILED` по исходной причине. Deterministic fallback не обходит run-level veto. |

В `llm_first` deterministic fallback при необходимости впервые запускает M5
analyzer на том же replay и независимо проверяет результат. В `llm_assisted`
можно использовать сохранённый результат предыдущей попытки, если его lineage
и validation всё ещё действительны. Fallback всегда входит в общий run deadline.

Report строится вне модели. Содержит source/extraction/profile/plan/normalized
fingerprints по доступным этапам, mode, provider/model/version каждого attempt,
prompt/schema/generation hashes, latency, actual/reserved usage, fallback reason,
выбранную policy, sample/chunk coverage, excluded/unresolved scope и issues.
`llm_calls` считает реальные attempts, включая неуспешные; blocked-before-send
отмечается отдельно. Empty document: `NEEDS_REVIEW` с `SOURCE_EMPTY`, ноль calls,
ноль records и provenance coverage 0, без выдуманного plan/normalized hash.

Разделять sample coverage, examined source coverage, semantic target coverage и
provenance coverage. Для каждого принятого value provenance coverage обязана
быть 1; отсутствие returned values не доказывает полноту parsing. Unresolved
required scope по умолчанию блокирует завершение. `COMPLETED_WITH_WARNINGS`
допустим только для явно необязательных/excluded targets при полном учёте scope.
Raw samples, quotes, prompts, responses, PII, headers авторизации и exception body
в report/log не сохраняются. Physical issue refs допустимы только в DTO с bound
allowlist; агрегатный report хранит безопасные scope IDs/hashes согласно M2.

Partial output остаётся предварительным: только terminal manifest после полного
execution и cleanup разрешает completed report с normalized fingerprint.
Review не запускает loading; выбор/изменение плана человеком создаёт новую revision
и требует повторной validation. Поздний failure/cancellation не выглядит успехом.

## Шаги реализации

Порядок зависимостей: A → B → C → D. Каждый этап сначала добавляет наблюдаемый
contract/negative test, затем реализацию. Команды ниже запускаются из корня.

### A. Provider contract suite, FakeLLMProvider и NoLLMProvider

- **Файлы:** расширить `contracts/reports.py`, `contracts/common.py`,
  `contracts/__init__.py`, `exceptions.py`; добавить `contracts/llm.py`,
  `llm/__init__.py`, `llm/fake.py`, `llm/no_llm.py`, `llm/schemas.py`.
  Port `ports/llm.py` сохранить; документировать расширенные guarantees.
- **Поведение:** immutable capabilities и policies; fake с заданным сценарием
  success/error/usage/latency, без сети и общего mutable state. Неожиданный request
  даёт явную ошибку; request recorder bounded и по умолчанию хранит metadata,
  raw payload разрешён только явно тестовым fixture. NoLLM реализует disabled branch.
  Зафиксировать report-before-plan и backward serialization до dependent этапов.
- **Тесты:** `contract_suites/llm.py`, `unit/llm/test_provider_contract.py`,
  `unit/llm/test_fake_no_llm.py`, `unit/contracts/test_m06_llm_contracts.py`.
  Проверить static/runtime protocol, canonical JSON, actual identity/schema binding,
  approval tampering, unknown usage, N/N+1 byte limits, timeout/cancellation,
  независимые instances, отсутствие HTTP import/I/O и legacy DTO round-trip.
- **Проверка:** `uv run --locked --no-sync pytest -q packages/structuraguard/tests/unit/llm packages/structuraguard/tests/unit/contracts packages/structuraguard/tests/unit/ports`.

### B. OpenAICompatibleProvider и policy-aware router

Минимальный provider/router реализован. Фактические файлы: `llm/openai_compatible.py`,
`llm/_http.py`, `llm/_structured.py`, `llm/_boundary.py`, `llm/router.py` и typed
policy/budget DTO в `contracts/llm.py`. Budget принадлежит run-local router, отдельный
`budgets.py` пока не нужен. `security/llm_input.py` и `security/redaction.py` ниже —
оставшаяся работа. Approval связывает полный immutable route set; каждый fallback
повторно проверяет тот же payload/policy и concrete capabilities. Router не создаёт
новые approvals. Реализованы пять запрошенных modes, transient-only переключение
без повторов одного provider; `quality_first` отложен. См. ADR 0012.

- **Файлы:** `llm/openai_compatible.py`, `llm/router.py`, `llm/budgets.py`,
  `security/llm_input.py`, `security/redaction.py`; существующий
  `ports/security.py`. `packages/structuraguard/pyproject.toml`, `uv.lock`:
  отдельный optional extra `llm` с HTTPX и проверенными constraints, без vendor SDK
  в core. HTTPX уже используется optional Tika adapter; проверить лицензию,
  поддержку и транзитивные риски перед изменением dependencies.
- **Поведение:** явно настроенный endpoint/model/SecretStr credential,
  один документированный Chat Completions-compatible transport profile, native
  JSON Schema response mode; endpoint без требуемой capability отклоняется.
  Не имитировать structured output tool calling. Timeout, streamed byte cap,
  строгий envelope parser, typed errors и ownership `AsyncClient`/`aclose`.
  Не читать env/proxy config автоматически, не следовать redirects; HTTPS для
  cloud, HTTP local только по trusted explicit policy. Не угадывать locality.
  Router выполняет описанный privacy filter, reservations/retries/fallback.
- **Тесты:** `unit/llm/test_openai_compatible.py`, `test_router.py`,
  `test_budgets.py`; `security/llm/test_egress.py`, `test_redaction.py`,
  `test_injection.py`; `integration/test_llm_http.py`. Общий suite A применить
  к adapter через mock transport и локальный fake HTTP server. Негативы:
  401/403/429/5xx, malformed/truncated/refusal/tool_calls, wrong model/schema,
  oversized streaming body, stalled body, concurrent reservations, forbidden
  cloud fallback, secret-bearing errors, approval reuse на другом destination.
- **Проверка:** `uv run --locked --no-sync pytest -q packages/structuraguard/tests/unit/llm packages/structuraguard/tests/security/llm packages/structuraguard/tests/integration/test_llm_http.py`.
  Реальные облачные credentials/сеть не нужны для default suite; live smoke —
  отдельная opt-in проверка настроенного пользователем endpoint.

### C. LLMStructureAnalyzer и strict ParsePlan generation

Реализовано в пределах существующей grammar M5. Фактический contract:
`LLMStructurePolicy`/`LLMAnalysisContext`/`LLMStructureSuggestion` в
`contracts/semantic.py`; compiler в `structure/plan_compilation.py`.
Два replay passes подтверждают sample до egress и весь ParsePlan после ответа.
Одна LLM попытка; ambiguity/low score/incomplete scope требуют review.
`ParsePlan.semantic_analysis` и `ParseField.locale_hint` — optional поля 1.1.0.
Актуальные limits/ограничения и test mapping: [API C](../llm-semantic-parsing.md).
Ниже исходное разбиение плана; новые tests сосредоточены в `test_llm_analysis.py`
и `test_llm_plans.py`, отдельные property files не создавались.

- **Файлы:** `structure/llm_analysis.py`, `structure/semantic_samples.py`,
  `structure/plan_compilation.py`, `llm/prompts/`; расширить
  `contracts/parsing.py`, `contracts/analysis.py`, `ports/semantic.py`,
  `structure/profiling.py`, `structure/_plan_check.py`, `structure/validation.py`.
  Общий replay/snapshot helper выделять из M5, не дублировать его проверки.
- **Поведение:** проверенный source catalog и samples → exact approved payload
  → schema-bound suggestion → compiler → независимая physical validation.
  Проверить все четыре families в уже исполняемом M5 subset. Большая таблица
  использует один общий конечный plan; evidence-only samples не подменяют полный
  replay. Source identifiers вне catalog запрещены даже при high confidence.
- **Тесты:** `unit/structure/test_llm_analysis.py`, `test_semantic_samples.py`,
  `test_plan_compilation.py`; `security/structure/test_llm_plans.py`,
  `property/structure/test_llm_sampling.py`. Fixtures: ambiguous header/footer,
  nested parent/child paths, multiline LOG variants, document key/value targets;
  forged refs/hashes, executable/unknown selectors, conflicting scope,
  Unicode/byte boundaries и omitted candidates. Для N и 10N строк при одинаковом
  scope — одинаковое число logical plan calls, максимум по общему budget;
  executor выполняется с provider, который падает при любом вызове.
- **Проверка:** `uv run --locked --no-sync pytest -q packages/structuraguard/tests/unit/structure packages/structuraguard/tests/property/structure packages/structuraguard/tests/security/structure`.

### D. HybridStructureAnalyzer, document entities и semantic parse report

- **Файлы:** `structure/hybrid.py`, `structure/chunking.py`,
  `structure/document_entities.py`, `parsing/__init__.py`, `parsing/session.py`,
  `parsing/registry.py`; расширить `contracts/parsing.py`, `contracts/execution.py`,
  `contracts/normalized.py`, `contracts/reports.py`, `structure/_runtime.py`,
  `structure/_plan_check.py`, `structure/validation.py`, `structure/execution.py`.
- **Поведение:** таблица трёх режимов, bounded prose extraction schema,
  exact span validation и compilation в document plan; тот же executor создаёт
  normalized values. Реализовать report для success/review/rejection/failure,
  early close/cancellation и budget-exhausted partial chunk loop.
- **Публичный API:** `structuraguard.llm` exports трёх providers/router;
  `structuraguard.structure` exports двух analyzers;
  `structuraguard.parsing.ParsingPolicy` и async-closeable `SemanticParsingSession`
  принимают завершённый extraction и explicit replay port. Session предоставляет
  `analyze_structure`, `create_parse_plan`, `validate_parse_plan`,
  `parse_semantically(plan=...)`, итоговый report и instance-local
  `structure_analyzers.register(...)` с snapshot на run. Saved plan повторно
  сверяет source/extraction/schema fingerprints и проходит validation, без LLM.
  `ParserRegistry` и Parser protocol не меняются. Сейчас `sdk.py` — scaffold;
  полный `AsyncStructuraGuard.inspect_source`/source lease и facade wiring из
  примера §23.1 остаются отдельной orchestration работой. Документация явно
  различает исполняемый API M6 и целевой SDK facade, без заявления о его готовности.
- **Тесты:** `unit/structure/test_hybrid.py`, `test_document_entities.py`,
  `unit/parsing/test_session.py`, `test_registry.py`,
  `unit/contracts/test_m06_semantic_report.py`,
  `property/structure/test_document_spans.py`,
  `security/structure/test_document_provenance.py`,
  `integration/test_semantic_parsing.py`, `docs/test_m06_examples.py`.
  Matrix: trusted synthetic TXT/LOG/CSV/JSON/HTML/DOCX/PDF после M4; zero-call
  deterministic/high-confidence paths; no provider; empty/oversized input;
  overlap duplicates, same-value/different-ref, malicious quote/offset,
  redaction offset drift, cross-chunk conflicts, unknown refs beyond index,
  late malformed batch/cleanup failure. Batch-size property tests сравнивают
  semantic projection, не равенство extraction hashes разных runs.
- **Проверка:** `uv run --locked --no-sync pytest -q packages/structuraguard/tests/unit/parsing packages/structuraguard/tests/unit/structure packages/structuraguard/tests/unit/contracts packages/structuraguard/tests/property/structure packages/structuraguard/tests/security packages/structuraguard/tests/integration/test_semantic_parsing.py packages/structuraguard/tests/docs`.

### Документация, ADR и итоговые gates

Перед реализацией C/D оформить предложенные долгоживущие решения в
`docs/adr/0012-llm-semantic-parsing-boundaries.md`: wire projection → canonical
ParsePlan, selective physical proof, span selector/schema 1.2.0, privacy-bound
attempt ledger. Выбран детерминированный compiler/executor вместо прямого
LLM→NormalizedBatch: больше явной grammar, зато одна validation boundary.
Выбран bounded caller-owned replay вместо нового storage subsystem: меньше scope,
но приложение обязано обеспечить повторное чтение snapshot и его lifetime.
Текущие принятые ADR планом не изменяются; ADR создаётся как часть реализации.

Обновить `docs/public-api.md`, `docs/structure.md`, `docs/security.md`,
`docs/codex/PROJECT_STATE.md`, parsing-строку `docs/codex/SPEC_INDEX.md` и MkDocs nav.
Проверяемые offline examples используют `FakeLLMProvider`; указать ограничения
M5 grammar, target coverage и deployed endpoint capabilities. Новые модули и
prompt/schema assets внести в `scripts/verify_distribution.py` и packaging tests.

После каждого этапа: узкие tests, `make lint`, `make typecheck`, review diff через
`structuraguard-review`; на trust boundary — `structuraguard-security`.
Перед завершением M6: `make lint`, `make typecheck`, `make test`,
`make test-integration`, `make test-security`, `make docs`, `make lock-check`,
`make test-build`, `git diff --check`. Отчёт приёмки сопоставляет K1–K8 с реальными
test names и свежим выводом; зелёный fake suite не доказывает качество live model.

## Риски и отложенный scope

- Различия OpenAI-compatible endpoints в JSON Schema, usage и context limits:
  declared capabilities и contract suite обязательны; несовместимость — явный отказ.
- Provenance доказывает происхождение, но не правильность business label и не
  полноту извлечения: конфликт/нехватка evidence требует review, отдельно измерять
  semantic quality на размеченных fixtures.
- PII detection несовершенно; caller classification и deny-by-default routing
  остаются обязательными. Redaction может уничтожить нужный контекст и снизить coverage.
- Replay и document spans — основные compatibility/resource risks. Без точной
  lineage, bounded selective index и проверенного offset mapping этап D не принимается.
- Большие неоднородные документы могут исчерпать десять attempts или восемь chunks:
  это `NEEDS_REVIEW`, а не скрыто урезанный successful dataset.
- В M6 **не входят** DB inspection/catalog/candidate mapping, `MappingPlan`,
  DB semantic mapper, DDL/DML, staging/load и schema proposal. `LiteLLMProvider`
  обязателен для полного §19.2, но не включён в перечисление M6: отложен вместе
  с дополнительными vendor adapters. Также отложены OCR, fine-tuning, embeddings,
  vector stores, persistent response cache, learned routing, automatic repair,
  произвольные normalization/conversion rules и полный SDK facade.

[spec-plan]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#fr-014-structural-profiling-и-parseplan
[spec-modes]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#fr-015-llm-assisted-semantic-parsing
[spec-llm]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#19-llm-агностичность
[spec-m6]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m6-llm-assisted-semantic-parsing

## Review M6-C

Проверены schema/source/egress boundaries, cleanup, cancellation, deadlines,
legacy wire compatibility и packaging exports. Исправлены подтверждённые cases:

- **High — неполный physical scope:** bounded index мог скрыть вторую таблицу,
  и plan первой таблицы принимался как полный. Теперь cardinality считается
  по полному replay независимо от indexed refs. Regression:
  `test_unindexed_second_table_cannot_be_hidden_by_bounded_catalog`.
- **Medium — cancellation/deadline перед egress:** immediate in-memory replay
  мог не отдавать управление event loop. Между batches добавлен checkpoint;
  absolute deadline проверяется перед scanner/provider и перед возвратом результата.
  Regressions: `test_cancellation_during_immediate_replay_prevents_provider_call`,
  `test_expired_replay_deadline_prevents_scan_and_provider_call`.
- Внешние scanner/replay factory exceptions очищаются до typed error, без raw
  messages; regression `test_external_open_and_scanner_errors_do_not_disclose_details`.

Подтверждённых незакрытых Critical/High/Medium findings в scope C не осталось.
Ограничения: реальные model quality/backend compatibility не проверяются offline;
production PII scanner/redaction внедряется host application; Hybrid, chunk entity
extraction и semantic report остаются D. Regex veto не является sandbox — защиту
обеспечивают также закрытая grammar, literal source membership и mandatory validator.

## Локальная приёмка M6-C {#m06-c-acceptance}

После всех code/security fixes: узкий analyzer/security/docs набор `73 passed`;
полный `make test` — `2086 passed`, integration — `16 passed`, security —
`387 passed`. Ruff (201 файл), strict mypy (199 файлов), MkDocs strict,
lock-check, isolated wheel/sdist verification и `git diff --check` прошли.
Команды и timings: [состояние проекта](../codex/PROJECT_STATE.md#m06-c-checks).
Реальные LLM backends не вызывались. Связанные ограничения C/D перечислены выше;
эти результаты не объявляют завершённым весь M6.

## Реализация D — выполненный scope

До изменения C создаёт проверенный LLM proposal; объединённого flow, document
span execution и итогового report нет. После изменения доступны три режима,
zero-call deterministic fast path, один structural LLM call на повторяющийся
scope и bounded document chunks с проверенными spans.

1. `contracts/document_semantics.py`, `contracts/semantic.py`, `reports.py`:
   closed span grammar, ParsingPolicy, versioned score/report; contract tests.
2. `structure/hybrid.py`, `parsing/session.py`: M5 candidates → bounded proposal →
   validator/executor, explicit fallback/review, run budgets и lifecycle; mode tests.
3. `structure/chunking.py`, `document_entities.py`, `_runtime.py`: bounded chunks,
   exact source spans, stable merge/dedup и детерминированное применение; fixtures
   prose/HTML/PDF/DOCX, conflicts/unknown refs/injection/cancellation regressions.
4. Literal nested XML paths, end-to-end CSV/LOG/JSON/XML/document tests,
   документация/ADR, review и обязательные quality gates. DB mapping отсутствует.

Команды: узкие `pytest .../unit/structure .../security/structure`, затем
`make lint typecheck test test-integration test-security docs lock-check test-build`.

## Исправления финального review {#m06-final-review-fixes}

Scope исправлений ограничен тремя findings. Публичные signatures, wire schemas,
provider contract и утверждённый bounded scope не менялись; новых dependencies нет.

| Finding | Исправление | Regression test в `tests/security/structure/test_m06_final_review.py` |
| --- | --- | --- |
| High: saved document plan снимал исходный review | Положительный `final_assessment.penalty` сохраняет `NEEDS_REVIEW`; доступный document scope консервативно unresolved, terminal/normalized fingerprint отсутствуют. Без penalty успешный replay сохраняет прежний output. | `test_saved_document_keeps_review_after_round_trip`: три режима, positive/review responses, два последовательных serialization/replay без LLM |
| Medium: chunk coverage saved plan не имел deadline | Один absolute deadline охватывает M5 evidence, validation, coverage и cleanup; проверяется также завершение после deadline без своевременной cancellation. | `test_saved_document_deadline_covers_coverage_replay`: controlled clock/event, задержка coverage и накопление времени между проходами; FAILED, закрытые iterators и ноль LLM calls |
| Medium: terminal issue переполнял report | При насыщении списка сохраняются security veto, затем terminal причина в пределах `max_issues`; FAILED/CANCELLED report больше не заменяется ValidationError. | `test_terminal_report_survives_saturated_issue_limit`: лимиты 1/1000, закрытие preview и поздний cleanup failure; status, issues, usage и отсутствие fingerprint/canary |

Точные прежние blockers не входят в saved plan: исправление High сохраняет
консервативный запрет, не вводя новый manual approval API. Новые операции с DB,
PII runtime и router/session composition не добавлялись. Повторный
`structuraguard-review`/`structuraguard-security` нового fix diff не выявил других
существенных findings. Фактические команды, результаты и ограничения:
[состояние проекта](../codex/PROJECT_STATE.md#m06-review-fix-checks).

## Подготовка ручного commit и Draft PR {#checklist-commit-pr}

Проверен локальный uncommitted diff ветки `feat/m06-llm-semantic-parsing`.
База — `97c3154` (merge M5); локальные `main` и `HEAD` совпадают:
`git rev-list --left-right --count main...HEAD` возвращает `0 0`.
Это сравнение локальных refs, без fetch и утверждений об актуальности remote `main`.
На этом шаге изменены только этот план и `docs/codex/PROJECT_STATE.md`.

Готовность относится к передаче реализованного bounded scope на review. Она не
подменяет полную приёмку исходных K5/K7. Рекомендуется Draft PR; перед готовностью
к merge следует закрыть эти требования либо явно согласовать изменение scope.
Самостоятельное изменение критериев этим checklist не выполняется.

### Сверка приёмки

Наблюдаемые tests и ограничения каждого критерия: [матрица K1–K8](M06_acceptance.md).
Проверены существование всех 50 указанных test functions и
[regressions финального review](#m06-final-review-fixes).

| Критерий | Фактический статус |
| --- | --- |
| K1 | Подтверждён: три режима, default `llm_assisted`, deterministic/disabled/saved plan без LLM calls. |
| K2 | Подтверждён: общий provider contract для fake/disabled/HTTP, typed errors и замена provider при сохранении validation. |
| K3 | Подтверждён: один план для повторяющихся records; рост строк и смена batch size не дают per-row LLM. |
| K4 | Подтверждён в закрытой grammar: schema, identifiers/refs, lineage, span reproduction, обязательный validator и security veto. |
| K5 | Частично: classification/destination/approval/budgets/fallback проверены. Production masking и единый router → session flow отсутствуют; retry одного provider не реализован. |
| K6 | Подтверждён в bounded scope: document chunks, grounded values/relations, dedup, stable merge и явный unresolved остаток. |
| K7 | Частично: terminal-only success, review/failure/cancellation, usage и safe attempt metadata проверены. Полный plan/report может содержать PII и не подходит для публикации как audit payload. |
| K8 | Подтверждён: legacy DTO и M2/M4/M5 suites, public exports, import isolation и installed offline examples. |

### Checklist

- [x] A, минимальный B, C и bounded D реализованы; DB mapping не добавлен.
- [x] K1–K4, K6 и K8 сопоставлены с наблюдаемыми tests; K5/K7 не помечены завершёнными.
- [x] Исправлены три Medium security findings и последующие один High / два Medium финального review; behavioral fixes имеют regressions.
- [x] Повторный review исправлений не выявил новых существенных findings; ограничения сохранены.
- [x] Public API, русские docstring, examples и ADR 0011–0014 отражают исполняемое поведение.
- [x] `PROJECT_STATE.md` актуален; команды и результаты предыдущих этапов не выданы за новые запуски.
- [x] Состав всех tracked/untracked файлов просмотрен; secrets/debug/generated artifacts не обнаружены, synthetic canaries сохранены как tests.
- [x] `git diff --check` и whitespace check новых файлов прошли.
- [x] Production dependency diff ограничен optional extra `llm`: уже используемые HTTPX/httpcore, без новых package versions в lockfile.
- [x] Свежие полные локальные gates передачи успешны; команды и результаты ниже.
- [ ] Production PII detection/redaction и redaction offset mapping: отсутствует runtime, часть K5.
- [ ] Безопасный aggregate report целиком: часть K7; source refs/semantic names могут содержать PII.
- [ ] Analyzer registry и router/session composition из исходного design D: не реализованы.
- [ ] Remote CI на Python 3.12/3.13/3.14: проверить после ручного push/PR.
- [ ] Ручной commit, push и Draft PR в `main`: выполняет пользователь.

### Файлы передачи

В diff **82 файла: 22 tracked изменения и 60 untracked новых файлов**.
Новые каталоги нельзя пропустить при ручном staging. Полный список дают
`git diff --name-only HEAD` и `git ls-files --others --exclude-standard`;
обычный `git diff --stat` показывает только tracked часть.

| Область | Файлов | Содержание |
| --- | ---: | --- |
| `packages/structuraguard/src/structuraguard/` | 34 | LLM contracts/ports/providers/router; structure analyzers, chunks и entities; semantic session; расширения validator/executor/report. |
| `packages/structuraguard/tests/` | 29 | Provider contract, fakes, unit/property/security, examples и packaging; в том числе 12 regressions последнего review. |
| `docs/` | 15 | Provider/analyzer/semantic guides, ADR 0011–0014, plan/acceptance/security records, public API, architecture, state/index. |
| `mkdocs.yml` | 1 | Навигация новых руководств и ADR. |
| `packages/structuraguard/pyproject.toml`, `uv.lock` | 2 | Optional HTTP extra `llm`; core не требует HTTP client. |
| `scripts/verify_distribution.py` | 1 | Проверка новых exports и offline examples установленного package. |

### Команды и фактические результаты передачи

После runtime fixes выполнены **12 новых / 351 узкий / 2219 полный / 18 integration /
433 security** tests, lint (225 файлов), mypy (223 файла), strict docs, lock-check и
wheel/sdist verification. Точные команды и времена:
[предыдущий полный запуск](../codex/PROJECT_STATE.md#m06-review-fix-checks).

Свежий запуск **перед ручным commit/PR**, после обновления checklist:

```bash
UV_OFFLINE=1 UV_CACHE_DIR=/Users/katana/.cache/uv uv run --locked --no-sync pytest -q \
  packages/structuraguard/tests/docs

UV_OFFLINE=1 UV_CACHE_DIR=/Users/katana/.cache/uv PYTEST_ADDOPTS=-q \
  make lint typecheck test test-integration test-security docs lock-check test-build
```

| Проверка | Фактический результат |
| --- | --- |
| Documentation/example tests, запущены первыми | **109 passed**, 2.48 s |
| `make lint` | **225 файлов**, format/Ruff без замечаний |
| `make typecheck` | Strict mypy: **223 файла**, без ошибок |
| `make test` | **2219 passed**, 76.98 s |
| `make test-integration` | **18 passed / 2201 deselected**, 4.61 s |
| `make test-security` | **433 passed**, 19.63 s |
| `make docs` | Strict MkDocs, **exit 0**, локальные links/anchors прошли |
| `make lock-check` | **100 packages**, exit 0 |
| `make test-build` | Offline wheel/sdist rebuild/install и installed examples, **distribution verification OK** |

Полная команда завершилась с **exit 0**. Для local parser watchdog, loopback tests
и packaging использовано разрешение вне sandbox; внешние LLM не вызывались.
После записи результатов повторён
`UV_CACHE_DIR=/private/tmp/structuraguard-m06-uv-cache make docs`: **exit 0**.
Финальный diff audit не выявил новых файлов или изменений runtime/tests;
повторный docs/review не выявил новых существенных findings.

В текущем шаге дополнительно выполнены:

- `git status --short`, `git diff --stat`, `git log -1 --format='%h %s'`,
  `git branch --show-current`, `git rev-list --left-right --count main...HEAD`:
  состав и локальная база подтверждены, staged changes отсутствуют.
- `.venv/bin/python /private/tmp/m06_commit_audit.py`: read-only audit объединения
  `git diff --name-only HEAD -z` и `git ls-files --others --exclude-standard -z`.
  Проверены token/private-key/credential-URL patterns, AST production debug calls,
  filenames/binary artifacts и whitespace untracked файлов через
  `git diff --no-index --check /dev/null <file>`. Признаков реальных secrets нет;
  два URL совпадения — intentional negative fixtures в `test_http_boundary.py:27`
  и `test_provider_boundary.py:53`. В diff нет debug calls или случайных artifacts.
  AST-сверка ссылок матрицы с test functions: **50 references, 0 missing**.
  Python окружения — **3.12.9**.
  Audit script, SHA-256 inventory и command logs хранятся в `/private/tmp`, вне diff.
- `git check-ignore site/index.html dist/structuraguard-0.3.0-py3-none-any.whl
  .pytest_cache .mypy_cache .ruff_cache .venv`: все build/cache paths игнорируются.
- `git diff --check`: **exit 0**.
- `command -v gitleaks trivy pip-audit bandit`: **exit 1**, инструменты отсутствуют;
  scoped pattern/AST audit выше не заменяет полный SCA/secret scanner.

### Непроверенные сценарии и причины

- Remote CI и Python 3.13/3.14 не запускались: текущая локальная среда — 3.12.9,
  а push/PR пользователь выполняет вручную. После изменения remote base повторить gates.
- Live LLM, semantic quality, реальный rate limit, TLS/proxy не проверялись:
  по условию применяются fake providers/HTTP transport, реальные платные API запрещены.
- Production PII/redaction drift, aggregate audit privacy, analyzer registry и
  router/session E2E не проверены как готовые функции: соответствующего runtime нет.
- Gitleaks/Bandit/Trivy/pip-audit и полный CVE/SCA audit не запускались: инструменты
  не установлены, отдельные targets не настроены; новые production packages не добавлены.
- Максимальные объёмы и длительные нагрузочные прогоны не измерялись: текущие suites
  проверяют bounded limits, batch invariance и controlled deadline, а не throughput SLA.
- Внешний HTTP link checker не настроен; strict MkDocs проверяет локальные links/anchors.
- OCR, новые LOG/XML grammars, downstream DB permissions/load и реальный Tika deployment
  не входят в этот diff/M6; неизменённые parser backends повторно не сертифицировались.

Saved document draft с penalty сохраняет `NEEDS_REVIEW` консервативно для всего
доступного document scope; точные прежние blockers не сериализуются отдельно.
Пять предупреждений full/integration suite относятся к upstream SWIG PDF types.

### Рекомендуемый commit и PR

Conventional Commit: `feat(parsing): add provider-neutral semantic parsing`.

Draft PR title: `feat(parsing): add provider-neutral semantic parsing (M6)`.
Base: `main`.

Краткое body:

> Добавляет provider-neutral LLM parsing для неоднозначных источников: fake/disabled/HTTP
> providers, policy-aware router, strict ParsePlan validation и Hybrid/session в трёх режимах.
> Повторяющиеся records используют общий plan; document chunks сохраняют provenance,
> а unresolved scope и поздние failures не подтверждают успешный результат.
>
> K5/K7 закрыты частично: production PII/redaction, безопасный aggregate report,
> analyzer registry и router/session composition остаются открытыми. DB mapping вне M6.
>
> Проверки: 2219 tests, 18 integration, 433 security; lint, mypy, strict docs,
> lock-check и offline wheel/sdist verification. Реальные LLM API не вызывались.
