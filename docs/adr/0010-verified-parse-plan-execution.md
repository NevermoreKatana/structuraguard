# ADR 0010 — Проверка ParsePlan по physical replay и потоковое execution

Статус: принято. Дата: 2026-09-10. Этап M5-C.

## Контекст

FR-014 запрещает несуществующие references и исполняемые инструкции в ParsePlan.
FR-012.2 требует raw values и physical provenance в NormalizedModel. Существующий
синхронный port validator получает manifest/profile, которые сами по себе не
доказывают существование каждого row/path. Сериализуемый `ValidatedParsePlan`
можно подделать; он не является security capability.

## Решение

`ParsePlanValidator.validate(request, batches=iterable)` проверяет closed schema,
fingerprints, versions, kinds, limits и policy, затем полностью проходит physical
source. Для async stream доступен `validate_source(request, batches)`. Source не
удерживается целиком. Вызов существующего port без replay возвращает
`REJECTED / PARSE_PLAN_REPLAY_REQUIRED`; unsafe/unknown payload никогда не получает
wrapper. Decoded JSON допускается как обычный dict с теми же полями request.
Preflight размера/depth/items выполняется до serialization/Pydantic validation.

Обе реализации используют `PlanRuntime`: он проверяет input batch hash/summary
против заранее bound manifest, source index, table/tree continuation, реальные
ranges/paths/fields и record boundaries. Field evidence должно принадлежать scope
соответствующего selector, а не просто встречаться где-нибудь в source.
При acceptance wrapper связывает plan, profile, extraction и options fingerprint.
Timestamp проверки не входит в стабильный validation fingerprint.

Executor повторяет статическую проверку и проверку wrapper/context binding;
`ParseExecutionContext.profile` обязателен. Затем каждый selection проверяется
повторно на реальном input batch до создания NormalizedRecord. Поэтому forged
wrapper не разрешает несуществующий путь или недопустимую операцию.

Для streaming нельзя доказать EOF до чтения хвоста: все выданные non-terminal
batches промежуточны. Единственный успешный terminal batch с manifest выдаётся
после EOF, проверки всех обязательных refs/ranges, закрытия незавершённых групп
и успешного cleanup источника. Поздняя ошибка не выдаёт success marker. Downstream
нужны staging/rollback; запись в БД в M5 не реализуется.

## Поддерживаемая policy

- Plans schema 1.0.0/1.1.0, extraction/profile schema 1.1.0 с проверяемыми hashes.
- Tabular: один record на data-row, конечный inclusive диапазон, exact repeated
  header comparison, footer start existence и реальные cell coordinates.
- Tree: literal raw key/occurrence, array item, legacy literal node names,
  parent/child collections и одиночные field paths. Parent record и его children
  находятся в одном NormalizedRecord; sibling collections не перемножаются.
  Root entity через несколько segments буферизуется только в record budget.
- LOG/document: finite explicit record refs; legacy one-line/fixed/blank-line
  grouping с EveryLineStart; document per-block/fixed contiguous groups.
- Selectors: copy cell/node/block, node name, bounded token по закрытому delimiter,
  literal document key/value, raw log record. PDF block с вложенными lines
  проецируется детерминированно с сохранением каждой physical line.
- Значения не преобразуются по semantic hint: нет автоматического locale repair,
  timezone guessing или string→money. Money требует native Decimal.
- Отдельные `ParseRule` (включая overlapping ranges), identity rules,
  include-descendants, ambiguous legacy log variants и document table-column
  targets отклоняются явно как неподдержанные. Для таблицы нужен TabularParsePlan.
  Это ограничение исполнения существующей grammar, а не silent no-op.

User-provided regex, SQL, Python, shell и callbacks отсутствуют в grammar.
Подобный текст в literal key/raw value остаётся данными и никогда не исполняется.

## Provenance и совместимость

Normalized schema 1.1.0 добавляет `origins` и `selection` в NormalizedValue:
каждый origin хранит настоящий physical ref, raw parent value и SourceLocation,
включая sheet/cell, JSON/XML path, page/block/bounding box. Для token/key/value
`raw_value` сохраняет целый parent text, `normalized_value` — выбранный фрагмент.
Для multiline сохраняются отдельные origins; агрегированный raw текст соединён
LF. Это не реконструкция исходных encoding/line endings. Allowlisted selection
operation и selector fingerprint объясняют преобразование.

Batch и terminal manifest получают canonical hashes schema 1.1.0. IDs зависят
от run/plan и порядка records, а не от времени проверки. Raw source не попадает
в IDs, diagnostics или logging. Поля extensions исключены из legacy serialization;
существующие schema 1.0.0 DTO продолжают round-trip без новых пустых полей.

## Ресурсы и отказы

`ParsePlanOptions` задаёт source limits, bytes/items record, bytes output batch,
plan bytes, total output records и количество output summaries. Между batches
хранятся finite scope/index refs, counters, незавершённый record и bounded summaries;
все просмотренные cells/values не накапливаются. Input units/columns проверяются
независимо от числа выбранных records. Большой явный tabular scope можно проверить
за пределами samples profiler; sampling не заменяет физическую проверку.

`ParseExecutionError.issue` содержит code, stage, безопасный reason, batch index
при наличии и emitted_batches. Ошибки source/selection/limits/timeout/cleanup
фатальны. Cancellation распространяется; iterator закрывается при ошибке,
`aclose` и нормальном завершении. Active deadline не включает паузу consumer:
между yield нет активного timeout context. Sync replay имеет cooperative timeout,
который не может прервать блокирующий пользовательский `next()`.

LLM, network, SQL execution, transformations из произвольного кода и storage
backend в этом этапе отсутствуют.
