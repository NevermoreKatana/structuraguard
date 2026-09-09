# ADR 0004. Lossless physical extraction

Статус: принято для M4.

Дата: 2026-09-04.

## Контекст

Контракт `ExtractedBatch` версии `1.0.0` описывал базовые physical objects, но
не позволял без потерь выразить окончания строк, bounded metadata, колонки raw
captures и число логических записей формата. Runtime также не мог отличить
полный набор physical references от малого downstream-индекса. Это мешало
потоковым TXT, LOG, Markdown, CSV, TSV и JSON-family adapters сохранять исходную
структуру без semantic догадок и без роста manifest пропорционально всему
source.

## Решение

- Wire-контракт `1.0.0` сохраняется. Новые optional поля исключаются из
  serialization, пока не заданы, поэтому legacy canonical JSON не меняется.
- Technical parsers M4 создают `ExtractedBatch` и
  `ExtractedDatasetManifest` версии `1.1.0`. Для этой версии `record_count`
  обязателен в batch, summary и manifest; aggregate обязан совпадать с суммой
  batch counts. Manifest также требует связанный с parser identity
  `ProducerMetadata` и canonical `parser_options_fingerprint`.
- `LineRangeLocation` MAY содержать совместно заданные zero-based,
  end-exclusive `column_start` и `column_end`. Они адресуют raw capture внутри
  одной строки и не назначают бизнес-поле.
- `ExtractedLine` и `ExtractedBlock` MAY содержать не более 64 уникальных
  scalar metadata entries. Metadata описывает только физические свойства,
  например `line_ending`, `physical_kind` и имя фиксированного recognizer.
- `ExtractedCell` MAY хранить bounded physical metadata, а `ExtractedTable` —
  stable continuation: `segment_index`, inclusive `row_start_index`/
  `row_end_index`, `is_last_segment` и bounded technical observations. Все
  continuation fields задаются совместно; cells обязаны находиться внутри row
  range своего segment.
- CSV/TSV сохраняет существующие cells как raw decoded strings с zero-based
  `TabularCellLocation`. Missing ragged coordinate не превращается в
  синтетическое значение, explicit empty остаётся cell, а blank row учитывается
  row range и `record_count`. Duplicate values и header tokens не
  переименовываются.
- Header detection является только bounded observation в table metadata. Она
  MUST NOT создавать field names, schema или окончательный `ParsePlan`.
  Dialect candidates и caller-provided immutable override входят в parser
  options fingerprint. Одинаково правдоподобные delimiters отклоняются
  fail-closed, а не разрешаются через MIME/extension.
- JSON family принимает только strict UTF-8 и UTF-8 с BOM. Он не применяет
  charset auto-detection и отклоняет другой `detected_encoding` до parsing;
  malformed byte sequence не заменяется replacement characters.
- JSON object members сохраняют исходный key в `raw_name`, включая duplicate,
  empty и unusual keys. Число сохраняется как исходный lexical token в
  `StringScalar` с отдельным technical hint; преобразование в `int`, `float` или
  бизнес-тип на technical parsing не выполняется. Object/member order, array
  indices, empty containers и nested collections остаются физическим деревом.
- `JsonPointerLocation.pointer` следует RFC 6901. Duplicate members имеют один
  pointer, но различаются `occurrence_path`; JSONL/NDJSON дополнительно сохраняет
  zero-based `record_index` и one-based physical line span. Whitespace-only lines
  не являются records и не создают tree nodes, однако учитываются в физической
  нумерации следующих записей и malformed diagnostics.
- Top-level JSON array MAY передаваться несколькими batches. Каждый segment
  повторяет только continuation root с общим `tree_id` и задаёт совместно
  `segment_index`, `child_start_index`, `child_count` и `is_last_segment`;
  complete child subtrees принадлежат ровно одному segment. Object и scalar root
  не разрезаются destructive способом.
- `indexed_refs` — bounded allowlist references для downstream sampling. Он не
  обязан перечислять все physical objects. Runtime проверяет существование
  ссылок в соответствующем batch, ограничивает ordered cumulative index 10 000
  элементами и для `1.1.0` требует его точного совпадения с terminal manifest.
  Built-in adapters включают deterministic prefix реальных line/block/value
  либо table/cell/value references, поэтому downstream может построить bounded
  `PhysicalSample`.
- `ParseContext.batch_options` задаёт положительные `batch_size` и
  `max_batches`; `max_physical_objects` независимо ограничивает полный
  physical output. Проверки bytes, lines, records и format blocks выполняются
  до неограниченного накопления. Caller overrides ограничены hard caps:
  `batch_size <= 1_000_000`, `max_batches <= 10_000` и
  `max_physical_objects <= 10_000_000`. Выбранный registry adapter получает
  immutable `detected_encoding` из проверенного probe result без mutable parser
  cache.
- В schema `1.1.0` batch fingerprint канонически покрывает все поля batch,
  кроме собственного hash и terminal manifest; extraction fingerprint покрывает
  весь manifest кроме собственного hash. Builder вычисляет их, а runtime
  независимо пересчитывает до выдачи consumer.
- Malformed encoding/input и unsupported feature проходят parser boundary
  только как allowlisted `PARSER_ENCODING_UNSUPPORTED`,
  `PARSER_MALFORMED_INPUT` и `PARSER_UNSUPPORTED_FEATURE`. Неизвестные parser
  codes остаются `PARSER_OUTPUT_INVALID`; security limits остаются
  `SecurityPolicyError`.
- Для malformed delimited row allowlisted diagnostics MAY содержать только
  one-based logical `record_number`, physical `line_number` и стабильный reason;
  raw row/cell в exception не включается. Публичная cell provenance в schema
  `1.1.0` гарантирует row/column, но пока не обещает byte offset или lexical span
  quoted token. Плановое расширение lexical/source-span provenance требует
  отдельного schema decision и не входит в Group B.
- Stateful escape encodings с zero-output shift sequences не входят в safe
  codec allowlist: это сохраняет независимость terminal batching от границ
  чтения и исключает скрытое состояние между physical lines.
- Parser сохраняет raw values, порядок и provenance. Он MUST NOT создавать
  окончательный `ParsePlan`, присваивать бизнес-сущности или target field
  names, вызывать LLM, анализировать БД либо выполнять destructive flatten.

## Последствия

Downstream может отличить логические records от числа line/block/value DTO и
проверить точные text spans Group A. Для CSV/TSV schema `1.1.0` гарантирует
row/column coordinates и continuity table segments, но не lexical byte span
исходного token. JSON-family consumers получают lossless member order, raw keys,
number lexemes и однозначную duplicate-key provenance без flatten. Manifest
остаётся bounded выбранными references, при этом terminal batch подтверждает
полный aggregate count. Потребителям, которые используют только `1.0.0`,
миграция не требуется; потребитель расширений `1.1.0` обязан явно поддержать
новую schema version. Другие версии physical extraction schema отклоняются
fail-closed.

Отдельные format adapters сохраняют собственные detection и physical grouping
rules. Общие bounded utilities не являются универсальным parser и не получают
доступ к DB, LLM или semantic analyzer.

Delimited adapter остаётся отдельной format family. Он использует
instance-local incremental tokenizer, не изменяет process-global
`csv.field_size_limit`/locale и не исполняет formula-like content. Внешний
табличный runtime не требуется; Polars не добавляется в dependencies. Row-count
batch target дополняется format-specific пределами cells и decoded chars;
segment закрывается только между complete logical rows. Dialect candidates
сканируются bounded slices с cooperative cancellation.

JSON document и JSONL/NDJSON остаются отдельными adapters с общим bounded
tokenizer, но разными record boundaries. Реализация не строит full-DOM, не
использует внешний JSON runtime и не добавляет unconditional либо optional
dependency. Parser не выполняет destructive flatten, не назначает child arrays
бизнес-сущностями и не создаёт semantic names.

Связанные документы: [ADR 0003](0003-two-stage-parsing-contracts.md),
[план M4](../plans/M04_technical_parsers.md),
[архитектура](../architecture.md), [публичный API](../public-api.md).
