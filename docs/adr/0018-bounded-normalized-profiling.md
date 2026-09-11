# ADR 0018 — Bounded normalized profiling и content fingerprint

Статус: принято для M8.

## Контекст

После semantic parsing нужен профиль полей независимо от формата источника.
Normalized manifest подтверждает lineage и batches, но его fingerprint зависит
от runtime IDs и границ batches. Полная проверка глобальных IDs требует памяти;
ни samples, ни вероятностный distinct sketch не заменяют эту проверку.

## Решение

- Новый `profiling.NormalizedDataProfiler` потребляет проверяемые normalized
  schema 1.1.0/1.2.0. Результат возвращается после terminal, EOF и cleanup;
  legacy 1.0.0 и preview без manifest отклоняются новым API. Старые DTO не меняются.
- Counts/extrema/lengths считаются полным проходом. Отсутствующее поле считается
  null position; unique ratio имеет знаменатель non-null occurrences. Samples
  независимы от статистик, ограничены occurrence bottom-k и canonical byte cap.
- KMV-128 хранит K минимальных digest. После overflow distinct явно estimated;
  exact означает точность при принятом предположении отсутствия hash collision.
  Сильный identity hint требует точных counts, не округлённого ratio/estimate.
- `LocalePolicy` задаёт `ru_RU`, `en_US`, `en_GB` или `unspecified`. Конфликты
  сохраняются ranked/ambiguous без конверсии исходных значений. Деньги — Decimal;
  несовместимые currencies не объединяются в candidate extrema.
- Локальный `PIIClassifier` получает bounded агрегаты. Профиль может содержать
  чувствительные extrema/labels, а `safe_summary()` — только allowlisted counters
  и classification. Default examples masked; local raw — явный режим, закрытый
  при findings/unknown. Классификация не выдаёт SecurityApproval.
- `normalized_data_fingerprint` — versioned SHA-256 canonical framed content:
  порядок records/entities, sorted fields, tagged values, schema и topology
  через ordinals. Run IDs, physical provenance и batch markers исключены.
  Manifest и profile hashes сохраняются отдельно. Hash не является анонимизацией.
- Точные sets глобальных IDs, summaries, поля, context и output имеют budgets.
  Repeated cancellation не бросает owned cleanup до отдельного deadline.
  Event-loop/RSS isolation процессом не обещается.

## Уточнения плана по результатам реализации

Default `max_batch_bytes` повышен с предложенных 4 до **8 MiB**, hard ceiling
остаётся 16 MiB. Preflight учитывает консервативную стоимость контейнеров/строк:
1000 records × 2 поля дают JSON 1250085 bytes, но оценку 5814688 bytes. При 4 MiB
стандартный benchmark отклонял корректный batch до validation. Общий retained
budget остался 64 MiB, hard ceiling 128 MiB. Это осознанное уточнение default,
не отключение контроля. Размер sample ограничивается canonical tagged scalar,
поэтому overhead учитывается и пример иногда пропускается раньше, чем raw UTF-8.

Внутренние distinct/sampling helpers объединены в `_bounded.py`, а field
statistics/inference/identity — в `_statistics.py`; отдельные дополнительные
абстракции для единственной реализации не вводились. Candidate extrema содержат
собственный count: неполная или неоднозначная интерпретация не выдаётся за метрику
всех occurrences. Defaults и ограничения перечислены в
[руководстве](../normalized-profiling.md).

## Последствия

Память ограничена ценой максимального размера допустимого run. Очень большой
логический iterator останавливается у cap, а не материализуется. Полный pipeline
может допускать больше записей, чем profiler. External replay/ID store,
composite key discovery, RFC-complete validators, production DLP и automatic
mapping/import остаются за границами M8.

Связанные решения: [двухэтапная модель](0003-two-stage-parsing-contracts.md),
[physical profiling](0008-bounded-structural-profiling.md),
[terminal semantics M6](0014-hybrid-semantic-parsing.md).
