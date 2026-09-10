# ADR 0008. Bounded StructuralProfiler

Статус: принято для этапа A M5. Дата: 2026-09-10.

Документ фиксирует границу profiler. Реализованные этапы B/C описаны отдельно
в [ADR 0009](0009-deterministic-structure-analysis.md) и
[ADR 0010](0010-verified-parse-plan-execution.md).

## Контекст

Технические parsers M4 выдают raw physical batches с terminal manifest.
Profile M2 описывает только общую форму, требует непустой evidence и не
выражает coverage. Новый profiler должен сохранять несколько гипотез,
контролировать память и возвращать профиль также для пустого источника.

## Решение

- `structuraguard.structure.StructuralProfiler` реализует port с async
  `profile(ExtractedBatch | AsyncIterable[ExtractedBatch]) -> StructureProfile`.
  Один batch должен быть terminal; поток потребляется полностью и закрывается.
  Profiler не получает parser, source reader, DB, LLM или network capability.
- Профиль schema `1.1.0` добавляет обязательный `ProfileCoverage`, typed
  observations четырёх семейств и field hints. `StructureCandidate.observation_ids`
  связывает гипотезу с конкретными observations, а не только с общим table ref.
  Confidence — эвристическая оценка, не статистически калиброванная вероятность.
- В отличие от предварительного плана A1, пустой source возвращает пустой
  `StructureProfile` с нулевым coverage. Это следует текущему требованию единого
  результата profiler. Профиль без достаточного evidence также может быть
  пустым, но содержит ненулевой `seen_items` и причины неполного покрытия.
  Такой профиль не удовлетворяет непустому analysis request M2 сам по себе.
- Legacy profile `1.0.0` сохраняет прежнюю сериализацию: новые optional поля
  исключаются, пока не заданы. Rich observations и candidate links требуют
  `1.1.0`. Новый profile fingerprint вычисляется и перепроверяется канонически.
  Версии physical extraction и ParsePlan не меняются; миграции данных нет.
- Каждый batch проходит preflight до deep validation/serialization. Для
  extraction `1.1.0` проверяются фактические hashes, progressive index,
  summaries и continuity; для legacy `1.0.0` каталог реальных refs ограничен
  10 000 объектами. Общая проверка graph depth/cycles не зависит от sampling.
- Default sample — ограниченный физический префикс с хвостовыми окнами таблиц.
  Хвост резервирует до четверти бюджета items/bytes; общий бюджет действует
  на все семейства вместе. Большие значения не обрезаются: sample пропускается
  с coverage reason. Counts таблиц вычисляются полным проходом, остальные
  distributions и clusters относятся только к выборке.
- Хвостовая строка может иметь provenance через ранее indexed table anchor и
  проверенный физический row index. Новые raw source refs вне manifest index
  не выдумываются. Text/block samples требуют собственных indexed refs.
- Template clustering использует конечные lexical classes и literal tokens,
  а не generated regex. Строки остаются данными. Неоднозначные headers,
  regions, roots и boundaries сохраняются отдельными кандидатами; top-K
  overflow отражается в coverage и не является выбором победителя.
- Память ограничена input batch budget, sample budgets, числом tables,
  patterns, observations, candidates и manifest summaries. Между вызовами
  instance не хранит source state. Deadline cooperative: синхронная работа
  ограничена размером batch, checkpoints находятся между batches/семействами.

## Последствия

Analyzer получает проверяемые structural hypotheses с явным покрытием, но не
готовый ParsePlan. Profiler не выполняет semantic conversions и plan execution;
validator/executor реализованы отдельно в M5-C. Uniform random/reservoir sampling
и replay store не реализованы.
Sparse/неоднозначные источники могут давать неполный профиль. При изменении
batch size меняются refs/fingerprints; полная semantic projection проверяется
отдельно от byte equality одного extraction run.

Связанные документы: [ADR 0003](0003-two-stage-parsing-contracts.md),
[ADR 0004](0004-lossless-physical-extraction.md),
[план M5](../plans/M05_parse_plan.md),
[публичный API](../public-api.md#structuralprofiler-m5-a).
