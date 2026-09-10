# ADR 0009 — Детерминированный анализ проверенного physical snapshot

Статус: принято. Дата: 2026-09-10. Этап M5-B.

## Контекст

FR-014 требует декларативные планы четырёх семейств, проверяемые source paths,
confidence и сохранение неоднозначности. Hash профиля и manifest index связывают
DTO с extraction, но не доказывают, что заявленные row ranges и paths совпадают
с содержимым. `PhysicalSample` также не является криптографическим доказательством
включения произвольного scalar в batch. Нельзя выдавать plan на основании одного
самосогласованного профиля.

## Решение

`DeterministicStructureAnalyzer` использует общий с `StructuralProfiler`
проверяющий проход. Его внутренний snapshot содержит только bounded samples,
manifest и профиль; исходные batches не удерживаются. Переданный профиль
сравнивается с заново построенным профилем при тех же options. Без physical
replay результат — `NEEDS_SEMANTIC_ANALYSIS` с `STRUCTURE_REPLAY_REQUIRED`.
Это явная нехватка evidence, не автоматический вызов LLM.

Компиляция использует существующие rows, nodes и blocks проверенного snapshot.
При неполном sampling или отсечённых observations/candidates автоматический
plan запрещён даже при пороге ноль. Source index membership проверяется также
через `ParsePlanValidationRequest`. Это не заменяет независимый validator M5-C
и не создаёт capability на execution.

Policy `structural_min_v1` выбирает confidence как минимум boundary support,
regularity и coverage. Числа и формула сохраняются в `candidate.assessment`,
вместе с evidence IDs, options fingerprint и blocking reasons. Порог по умолчанию
0.85 включительно. Несколько гипотез или независимых scopes возвращаются как
ranked candidates; сортировка по убыванию confidence, kind и canonical candidate
ID не выбирает победителя. В отличие от предварительного плана M5, margin и
weighted scoring пока не нужны: автоматического разрешения конкуренции нет.

Результат содержит новый derived `StructureProfile` с producer analyzer и своим
fingerprint. Исходный profile не меняется; `plan.analysis` сохраняет его fingerprint,
policy, candidate ID и unresolved fields. Все business semantics остаются
`unresolved`; primitive hints остаются evidence и не запускают conversions.

Расширения ParsePlan schema 1.1.0:

- `TreeStep`: только enum `key` с literal raw name/occurrence и `item` для
  array. Entity `record_steps` задаётся от physical root, field steps — от
  entity record. Parent relation задаёт принадлежность дочерней коллекции;
  sibling arrays не перемножаются. Legacy paths несовместимы с новыми steps.
- `ExplicitRecordGrouping`: enum `group_refs` и конечные упорядоченные records
  из уникальных physical refs. Только LOG/document; каждый scope ref включён
  ровно один раз, selectors ограничены длиной каждого record.
- `LogRecordSelector`: исходные тексты строк record с сохранением boundaries
  и whitespace; никакого динамического tokenizer/regex. Преобразование в
  NormalizedModel определяется executor из [ADR 0010](0010-verified-parse-plan-execution.md).
- `PlanDerivation`: воспроизводимость scoring и явные unresolved fields.

Расширения запрещены в schema 1.0.0. Старые DTO не получают новые пустые поля
в serialized payload. Unknown operators/версии/extra fields запрещены. Literal
с SQL или Python остаётся данными; интерпретатора этих языков нет.

## Реализованные правила и ограничения

CSV: header/data type contrast, стабильные columns, exact repeated headers;
последний summary признаётся footer при совпадении числовых сумм с data rows.
Ragged/empty/internal summary и неоднозначные merged regions требуют внешнего
решения. Несколько header candidates не сворачиваются в один.

JSON/YAML object/array: literal nested paths, родительские поля, child entities.
Optional fields требуют отдельной missing-value policy; XML element matching
и несколько physical tree roots остаются явной нехваткой правил.

LOG: начала на неотступных строках, продолжения на отступных/пустых строках;
конечные группы по shape последовательности. Несколько shapes — кандидаты.
Orphan continuation и нераспознанный участок не исчезают из решения.

Документы: heading sections либо явно заявленная physical per-block policy;
группы одинаковой последовательности block kinds. Raw block text сохраняется,
значения key/value и business meaning не угадываются. Таблицы анализируются
отдельным tabular scope. Неравные группы сохраняются как candidates.

## Последствия

Планы конечны и консервативны. Большой источник можно прочитать bounded способом,
но sampling gaps запрещают автоматически распространять plan на непроверенные
координаты. Потоковая проверка конечных scopes и execution реализованы отдельно в
[ADR 0010](0010-verified-parse-plan-execution.md); analyzer их не заменяет.
LLM/network в analyzer отсутствуют.
