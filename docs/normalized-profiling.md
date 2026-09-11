# Профилирование нормализованных данных M8

Передайте `NormalizedBatch` stream из `SemanticParsingSession.parse_semantically()`
или `ParsePlanExecutor.execute()` в `NormalizedDataProfiler.profile()`. Профиль
строится после semantic parsing и не зависит от исходного CSV/JSON/PDF/DOCX.
Значения не изменяются, DB/LLM/сеть profiler не использует.

Функция ниже принимает уже подготовленный поток и минимальный класс исходных
данных. Как получить поток: [детерминированный parsing M5](structure.md) или
[семантический parsing M6](semantic-parsing.md). Класс источника передаёт caller:
`NormalizedBatch` не переносит его автоматически в profiler.

<!-- example:m08-profile:start -->
```python
from collections.abc import AsyncIterable

from structuraguard.contracts import (
    DataClassification,
    LocalePolicy,
    NormalizedBatch,
    NormalizedProfileContext,
    NormalizedProfilingOptions,
    SafeProfileSummary,
)
from structuraguard.profiling import NormalizedDataProfiler


async def summarize_normalized(
    batches: AsyncIterable[NormalizedBatch],
    *,
    source_classification: DataClassification,
) -> SafeProfileSummary:
    profiler = NormalizedDataProfiler(
        NormalizedProfilingOptions(locale=LocalePolicy.RU_RU)
    )
    profile = await profiler.profile(
        batches,
        context=NormalizedProfileContext(data_classification=source_classification),
    )
    return profile.safe_summary()
```
<!-- example:m08-profile:end -->

Для исследования конкретного поля используйте `profile.field(entity_type,
field_name)`. Полный DTO сериализуется в JSON, но может содержать PII в labels,
именах и extrema. Для logs используйте только `safe_summary()`: он не содержит
raw values, identifiers/labels, examples, extrema или fingerprints. `repr`
профильных DTO также не раскрывает их содержимое.

Канонические требования: [FR-013][spec-fr-013],
[§11.4 — совместимость типов][spec-types], [§11.5 — анализ значений][spec-values],
[§11.6 — структурный контекст][spec-context],
[§12 — оценка достоверности][spec-confidence] и [M8][spec-m8]. M8 возвращает
признаки и неоднозначность; scoring сопоставлений и DB mapping здесь не реализованы.

## Завершённый поток и ошибки

Поддерживаются normalized schema 1.1.0 и 1.2.0. Один batch допустим только как
полный terminal dataset с index 0. Последний batch многочастного stream не
заменяет остальные batches. Non-terminal preview из M6 не даёт готового профиля.
Пустой terminal dataset без объявленных полей возвращает `fields=()`.
Если schema содержит поля, они сохраняются с нулевыми counts и неопределёнными
ratios. Пустой iterable — ошибка.

Проверяются payload hashes, последовательность, lineage, summaries, schema,
counts и точная глобальная уникальность IDs. Полученный iterator закрывается
при успехе, ошибке и отмене; внешними ресурсами управляет caller. Результат
не публикуется при данных после terminal, незавершённом EOF или cleanup failure.

`NormalizedProfilingError` импортируется из `structuraguard.exceptions`.
Поле `error_code` различает причины отказа:

| Код | Условие |
| --- | --- |
| `NORMALIZED_PROFILE_INVALID_STREAM` | Ошибка чтения, нарушенный контракт batch/manifest, lineage или context binding |
| `NORMALIZED_PROFILE_UNSUPPORTED_SCHEMA` | Строковая версия normalized schema вне 1.1.0/1.2.0 |
| `NORMALIZED_PROFILE_LIMIT_EXCEEDED` | Исчерпан бюджет входа, удерживаемого состояния или результата |
| `NORMALIZED_PROFILE_TIMEOUT` | Истёк общий deadline обработки |
| `NORMALIZED_PROFILE_CLASSIFICATION_FAILED` | Ошибка PII adapter или недопустимый результат, включая понижение baseline |
| `NORMALIZED_PROFILE_CLEANUP_FAILED` | Не удалось закрыть полученный iterator за cleanup budget |

`details["reason"]` содержит статическую безопасную причину без текста исходного
исключения. Если cleanup завершился ошибкой после другой ошибки, сохраняется
первичная ошибка со статической note `NORMALIZED_PROFILE_CLEANUP_FAILED`.
Отмена сохраняет `asyncio.CancelledError`; частичный профиль не возвращается.

Некорректные options/context при создании DTO дают Pydantic `ValidationError`
(подкласс `ValueError`), до начала обработки потока. Не сериализуйте произвольные
validation errors в безопасные logs: это отдельная граница от очищенных runtime
ошибок profiler. `profile.field()` ищет точные, чувствительные к регистру имена;
при отсутствии поля поднимает `KeyError("semantic_field_not_found")`.

## Статистики и неоднозначность

Поля разделены по `(entity_type, field_name)`; несколько entities в record
учитываются отдельно. Для поздно появившихся полей пропуски восстанавливаются
из общего количества entities этой type, без повторного прохода.

| Метрика | Значение |
| --- | --- |
| `present_count` | Число фактически переданных значений, включая NullScalar |
| `missing_count` | Entity occurrences без поля |
| `null_count` | Missing + explicit NullScalar; `""`, whitespace и `"null"` остаются строками |
| `null_ratio` | null_count / entity_count |
| `unique_count`, `unique_ratio` | Distinct non-null и distinct / non_null_count; mode exact/estimated |
| `minimum`, `maximum` | Общие extrema только для одного совместимого семейства; иначе None |
| `extrema` | Extrema и counts по семействам; integer/Decimal вместе, float/bool/date/datetime отдельно |
| `min_length`, `max_length`, `mean_length` | Длина строк в Unicode codepoints; bytes ограничиваются отдельно |
| `candidate_extrema` | Отдельные интерпретированные Decimal/date/datetime метрики со своим count |

Нулевой знаменатель даёт `None`; при отсутствии non-null значений `unique_count`
также равен `None`. Ratios фиксированы до 6 знаков с ROUND_HALF_EVEN
и precision 28; вывод не зависит от глобального Decimal context. Решения об
уникальности используют counts, не округлённые ratios. KMV хранит K минимальных
128-bit SHA-256 prefixes. До overflow count exact при предположении отсутствия
hash collision; затем estimate `(K−1)/u_K`, ограниченный `[K+1, non_null_count]`.
Estimate не доказывает UNIQUE и не обещает гарантированный confidence interval.

`inference` содержит ranked candidates/support, status
`resolved`/`ambiguous`/`insufficient_evidence`, inferred type только при resolved
и список причин. Значения и заявленный `semantic_type` не переписываются.
Специализированная интерпретация строк требует минимум 20 observations,
support 0.95, margin 0.10 и полного scan coverage; обычный тип `string` может
разрешиться сразу. Конфликт tagged kinds, locale, currency или declared
semantic type сохраняется явно. Для однородного tagged scalar минимальная
выборка не требуется. `0/1` сохраняют конкуренцию integer/boolean.

| Locale | Числа | Даты |
| --- | --- | --- |
| `ru_RU` | `125 000,50`, NBSP и narrow NBSP grouping | `01.09.2026` → 1 сентября; ISO |
| `en_US` | `125,000.50` | `01/09/2026` → 9 января; ISO |
| `en_GB` | `125,000.50` | `01/09/2026` → 1 сентября; ISO |
| `unspecified` (default) | Допустимые RU/EN интерпретации; `1,234` ambiguous | Slash DMY/MDY alternatives; dotted DMY и ISO |

Несоответствующий policy формат не преобразуется. Money candidate требует
денежного имени/semantic type либо одной currency prefix/suffix; binary float
не переводится в денежный Decimal. `$` не назначает страну/валюту. Naive datetime
не получает timezone автоматически. Typed DateTimeScalar уже хранит UTC.
Если перевод ISO-строки в UTC выходит за диапазон datetime, она не становится
datetime candidate и сохраняется как исходная строка.
Для offset `±HH:MM` допустимы часы 00–23 и минуты 00–59; `+00:99`
не нормализуется в другой offset и не даёт datetime evidence.

Пороги `type_support`/`type_margin` ограничены 4096 digits coefficient и
абсолютным stored Decimal exponent 4096. Более широкая запись отвергается при
создании options до `as_integer_ratio()`; это предотвращает неограниченную
арифметику и не зависит от process Decimal context.

Распознаются bounded syntactic email, phone, UUID, HTTP(S) URL, date/datetime,
boolean, money/currency и ИНН 10/12 с checksum. Это не DNS/mailbox/телефонная
валидация и не проверка регистрации ИНН. Добавлены categorical/free-text и
identity hints; они не назначают PK/FK, mapping score и не разрешают импорт.
Context из доступных origins и bound `NormalizedProfileContext.labels` не требует
повторного чтения source. Недоступные headings/source names не выдумываются.
Email pattern требует непустых DNS-меток до 63 символов без дефиса на краях
и домена до 253 ASCII bytes. Malformed domain не даёт email/identity evidence;
более широкий защитный PII-сигнал для похожего на email текста сохраняется.

## Samples и PII

Samples — bottom-k **occurrences** поля, с фиксированным seed, field ref и ordinal.
Batch/value IDs не участвуют в выборе. Default: 8 examples на поле, до 512 bytes
canonical tagged scalar, суммарно до 1 MiB. Слишком большой example пропускается
целиком; `sample_eligible`, `sample_skipped`, reasons показывают покрытие.
Sampling не влияет на counts/extrema/lengths/content hash.

`ExamplePolicy.MASKED` — default. `OMIT` не удерживает samples, но оставляет
чувствительные extrema и labels в полном профиле. Явный `LOCAL_RAW`
допускается только для PUBLIC/INTERNAL без findings/unknown; поздняя PII находка
маскирует также ранее выбранные samples. Caller может повысить минимальный класс
через `NormalizedProfileContext(data_classification=...)`; default — INTERNAL.

`LocalPIIClassifier` реализует port `PIIClassifier` и получает bounded aggregates,
labels и категории, без raw examples. Классы упорядочены PUBLIC < INTERNAL <
CONFIDENTIAL < RESTRICTED; adapter не может понизить минимум или локальные findings.
Имя поля и каждый context label проверяются отдельно, включая phone, ИНН-12
и URL с credentials; объединение labels не меняет границы распознавания.
`complete` означает `checked_count > 0` и `skipped_count == 0`. Без находок
полный scan даёт `not_detected`, иначе — `unknown`; находки дают `detected`
даже при `complete=False`. ИНН-12 даёт personal tax ID
signal; ИНН-10 организации сам по себе не является PII. Это эвристика, не DLP:
отсутствие находок не разрешает egress. Перед внешней отправкой по-прежнему нужен
существующий SecurityScanner и точный fingerprint-bound payload approval.

Собственный adapter передаётся через `NormalizedDataProfiler(classifier=...)`.
Это доверенный исполняемый код: проверка результата не изолирует его побочные
эффекты. `PIIClassificationRequest` содержит field/input binding, bounded
counts/categories и labels; labels остаются чувствительными. Adapter обязан
сохранить binding, полноту scan и минимум классификации. Его ошибка или
понижение локального baseline дают `NORMALIZED_PROFILE_CLASSIFICATION_FAILED`.

## Три fingerprints

- `normalized_manifest_fingerprint` — прежний hash normalized manifest: source,
  extraction/plan/producer, summaries, run IDs и границы batches.
- `normalized_data_fingerprint` — `normalized_content_v1`, стабильное содержимое.
- `profile_fingerprint` — canonical DTO профиля без самого hash-поля; включает
  lineage, options/context, classification и версии алгоритмов.

Content hash: SHA-256 с prefix `structuraguard:normalized_content_v1` и нулевым
байтом, затем canonical UTF-8 JSON frames с **8-byte big-endian length prefix**.
Frame sequences: `record` с parent и sorted related global ordinals; `entity`
с entity type и local parent ordinal; `value` с field name, declared semantic
value type и парой `(scalar kind, scalar value)`; `end_entity`; `end_record`.
Последний `schema` frame содержит sorted `(entity_type, field_name, semantic_type)`
и counts records/entities/values. Поля сортируются, records/entities идут в
исходном порядке. Отсутствующее поле отличается от NullScalar.

Canonical Decimal не зависит от scale (`1.00` = `1.0`), datetime — UTC;
string/integer/decimal/bool различаются, строки не strip/casefold/Unicode-normalize.
Physical refs/raw values, runtime IDs, producer, samples, locale/inference и
classification в content hash не входят. Связи выражены ordinals, не удалены.
Re-batching сохраняет hash при прежнем порядке и целых связанных record groups:
текущая модель запрещает cross-batch record links. Изменение schema, topology,
значения или порядка records меняет hash. Равенство unordered datasets не обещано.

Hash не доказывает authenticity и не анонимизирует значения. Downstream cache,
idempotency или import approval не должны опираться только на content hash:
нужны lineage и policy. Старое поле manifest не переопределено.

## Ограничения ресурсов

| Ресурс | Default / hard ceiling |
| --- | --- |
| Preflight batch estimate | 8 / 16 MiB, включая консервативную стоимость вложений |
| Batch items / depth | 100000 / 200000; 64 / 128 |
| Scalar bytes / digits / absolute Decimal exponent | 64 KiB / 1 MiB; 1024 / 4096; 1024 / 4096 |
| Pattern scan bytes | 4096 / 16384; большие строки дают unknown coverage |
| Batches / manifest estimate | 10000 / 100000; 8 / 16 MiB |
| Exact global IDs / ID bytes | 250000 / 1000000; 16 / 64 MiB |
| Entity types / fields | 64 / 256; 256 / 1024 |
| Distinct K / examples per field | 1024 / 4096; 8 / 16 |
| Names/context | 8 labels на поле, по 256 UTF-8 bytes |
| Relationship pairs / pair operations | 4096; 1000000 / 10000000 |
| Retained state ledger / serialized profile | 64 / 128 MiB; 4 / 16 MiB |
| Run / cleanup | 30 / 120 s; 2 / 5 s |

Ранний output budget консервативен и учитывает JSON-разделители; перед публикацией
дополнительно проверяется полный canonical JSON с metadata и fingerprints.
Результат сверх `max_profile_bytes` не возвращается.

Общий бюджет может сработать раньше отдельного cap. Exact IDs/summaries растут
до конечного лимита; profiler не обещает обработать бесконечный поток с O(1)
storage. В памяти текущий bounded batch, summaries/IDs, field sketches/counters,
context и output, но не весь dataset. Уже созданный caller batch не может быть
отменён profiler. Ledger не равен process RSS; transient validation/serialization
copies имеют отдельную bounded стоимость. Cooperative checkpoints каждые
256 values и между batches; некооперативный источник требует process isolation.

Benchmark: `uv run --locked --no-sync python scripts/benchmark_normalized_profiler.py
--records 50000 --batch-size 1000`. `--scenario distinct` меняет cardinality,
`--scenario unicode --batch-size 100` проверяет длинные строки; `--allocations`
добавляет tracemalloc и явно расширяет deadline до 120 s для инструментированного
прогона. Сравнивать timing следует без tracemalloc на одной машине.

Архитектурные причины и уточнение default batch budget:
[ADR 0018](adr/0018-bounded-normalized-profiling.md).

[spec-fr-013]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#fr-013-профилирование-нормализованных-данных
[spec-types]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#114-совместимость-типов
[spec-values]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#115-анализ-значений
[spec-context]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#116-структурный-контекст
[spec-confidence]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#12-оценка-достоверности
[spec-m8]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m8-normalized-data-profiler
