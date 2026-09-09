# План M04 «Независимые technical parser adapters»

Статус на 2026-09-09: реализация A–E и отдельного opt-in F подготовлена к ручному
commit и Draft Pull Request в `main`. Приёмка частичная: подтверждены 8 из 12
критериев в документированном scope; AC-06/07/11/12 остаются открытыми.
Это не разрешение на merge и не выпуск завершённого M4.

Дата плана: 2026-09-04.

Дополнение 2026-09-10: после ручного commit M4 исправлен выявленный CI
HTML compatibility defect; [новая локальная matrix](#m04-html-ci-fix) не заменяет
повторный remote CI. Срез передачи от 2026-09-09 ниже сохранён как история.

Ниже сохранён исходный design и последовательность реализации. Фактические
отклонения, команды и незакрытые gates перечислены в разделе
[передачи на ручной commit](#m04-manual-handoff).

## Цель

Реализовать независимо регистрируемые technical parser adapters для форматов
M4, которые по bounded source snapshot детерминированно выдают lossless physical
`ExtractedBatch` с точным provenance, не создавая `ParsePlan`, business entities
или DB mapping.

## Основание и принятые уточнения

План опирается на текущую задачу, `AGENTS.md`, `PROJECT_CONTEXT.md`,
`SPEC_INDEX.md`, ADR 0002/0003 и точечно извлечённые разделы ТЗ:
`FR-004`–`FR-012`, `NFR-006`, `20.1`, `20.5`–`20.9`, `20.13`–`20.14` и
`M4`. Полное ТЗ не загружалось.

Требования трактуются так:

- technical parser определяет формат, кодировку и физическую структуру, но не
  принимает решение о бизнес-сущностях, target field names или схеме БД;
- header, locale, scalar type, log token и document style допустимы только как
  технические observations/hints рядом с неизменённым raw value;
- parser не создаёт и не исполняет `ParsePlan`, не вызывает LLM, не получает DB
  ports и не выполняет flatten/unflatten;
- formula-like CSV/XLSX values, HTML active content, document formulas и source
  instructions остаются недоверенными inert data: parser не исполняет и не
  «исправляет» их; sink-specific escaping относится к output boundary;
- lossless здесь означает source-native raw scalar values, empty/missing,
  duplicate/order/hierarchy distinctions и точный physical address. Это не
  byte-for-byte CST для эквивалентной markup пунктуации; неизменные source bytes
  связывает `source_fingerprint`;
- `TXT`, `LOG`, `MD`, delimited text, JSON documents/lines, XML, HTML, YAML,
  XLSX, PDF и DOCX реализуются отдельными adapters либо узкими format-family
  adapters. Единого switch-based universal parser не будет;
- default `ParserRegistry` и facade остаются пустыми. Built-ins регистрируются
  явно существующим `ParserRegistry.register(...)`; import/construction facade
  не выполняет auto-discovery, I/O или загрузку optional dependencies.

## Наблюдаемое состояние до M4

- `structuraguard.ports.Parser` уже задаёт правильную неизменяемую границу:
  `probe(...) -> ProbeResult` и
  `parse(...) -> AsyncIterator[ExtractedBatch]` без LLM/DB authority.
- M3 registry instance-local, требует strong content evidence, считает MIME и
  extension advisory signals, детерминированно разрешает совместимые candidates
  и повторно валидирует physical output/terminal manifest.
- `contracts/source.py` уже содержит raw tagged scalars, line/table/sheet,
  JSON Pointer, XPath, CSS и document locations, physical lines/blocks/tables/
  trees, manifest и bounded `ExtractedSourceIndex`.
- `ParseContext` передаёт только `max_bytes`, `max_records` и
  `max_nesting_depth`; wrapper централизованно применяет только `max_records`.
- `tests/contract_suites/parser.py` проверяет один valid happy path, identity,
  sequence и terminal manifest. Реальных format fixtures/adapters и property
  suite пока нет.
- Extras `pdf`, `excel`, `office` и `tika` уже объявлены. Прямых dependencies
  для encoding, incremental JSON, hardened XML и YAML нет.

До format adapters необходимо закрыть следующие gaps:

| Gap | Почему блокирует M4 | Решение M4.0 |
| --- | --- | --- |
| Tree DTO не различает object/array/element/attribute/text/scalar, а `name` не сохраняет произвольный raw key | Пустые containers, duplicate JSON keys, XML/YAML roles и unusual names теряются | Versioned node kind, raw source name/token и bounded typed physical metadata |
| Нет identity продолжения таблицы/дерева между batches; parent tree обязан находиться в том же batch | Большой CSV/XLSX или nested source нельзя честно разбить без потери структуры | Stable physical container/segment identity и правило batch only at complete row/block/subtree boundaries |
| `blocks` и `tables` разнесены по разным tuples | Теряется смешанный порядок DOCX/HTML content | Ordered document-item references поверх batch-local blocks/tables |
| Location не различает duplicate JSON key, YAML mark, text span и DOCX part/order | Provenance недостаточно точен для round-trip/debug | Additive location variants/fields с occurrence, document index, line/column, part и block order |
| Обычный adapter `ParserError` превращается registry wrapper в `PARSER_OUTPUT_INVALID` | Malformed input, unsupported feature и `PARSER_NO_TEXT_LAYER` не видны как typed outcomes | Allowlisted reconstruction и redacted pass-through built-in format errors |
| `max_bytes`/depth не enforce core, batch size отсутствует, wrapper хранит все refs | Возможны over-read и O(total objects) memory до terminal batch | Budgeted reader, bounded batch builder, progressive indexed refs и max batches/deadline |
| Fingerprints принимает DTO, но core их не пересчитывает | Adapter может выдать самосогласованный, но неверный manifest | Общая pure hash projection: builder считает, а `ValidatedParserStream` независимо пересчитывает batch/run fingerprints |
| M3 static test запрещает любой класс с `probe` и `parse` внутри `parsers/` | Любой concrete adapter сломает suite | Заменить запрет на architecture/security boundary assertions M4 |

## Security review и trust boundary

Attacker-controlled input — source bytes, declared MIME/name, encoding markers,
column/key/tag names, archive members, formulas, document relationships и parser
library output. Активы — availability host process, filesystem/network/secrets,
raw data, integrity physical model и reproducibility evidence.

Обязательные controls:

- **High — resource exhaustion:** cumulative reads, decoded chars, records,
  columns, nodes, aliases, archive expansion, pages, batches, CPU/deadline и
  indivisible object size ограничиваются до накопления output. Частичный stream
  остаётся provisional и после failure/cancellation закрывается.
- **High — parser/container exploit:** DTD/XXE, external relationships/resources,
  archive traversal/symlink/executable entries, YAML object construction,
  macros/JavaScript/formulas и Tika auto-download запрещены deny-by-default.
- **High — отсутствующий sandbox:** ADR 0002 сохраняется. M4 реализует adapters,
  но не объявляет strict/untrusted in-process execution безопасным. До M12 такой
  run обязан завершаться `SECURITY_SANDBOX_REQUIRED`; Tika не допускается в
  обязательный M4 scope. Текущий `Parser`/`ParseContext` не несёт trust mode,
  поэтому M4 документирует эту deployment boundary, но не имитирует enforcement
  внутри adapter; реальный gate принадлежит runner/orchestrator M12.
  Уточнение optional F: отдельный opt-in HTTP client разрешён с caller-owned
  network/container isolation согласно ADR 0007, без заявления о SDK sandbox.
- **Medium — error disclosure/type confusion:** только известные format/security
  outcomes проходят boundary; details содержат adapter ID, resource, numeric
  limit и safe location, но не raw value, path, relationship URL, parser stderr
  или original exception text.
- **Medium — physical data loss:** raw values, empty/missing distinctions,
  duplicate keys/headers, physical order, hierarchy and exact locations
  сохраняются; normalization и destructive flatten запрещены.

## Scope, порядок и merge gates

Сначала выполняется общий contract slice M4.0. После него группы A–E зависят
только от M4.0 и могут реализовываться/merge независимо. Общий selection matrix
запускается после каждого merge. Core M4 готов только когда завершены A–E.
Группа F начинается отдельным решением только после зелёного core gate.

Планируемая карта production modules и основных tests:

| Группа | Production modules | Основные test modules |
| --- | --- | --- |
| A | `packages/structuraguard/src/structuraguard/parsers/builtin/text.py`, `log.py`, `markdown.py` | `packages/structuraguard/tests/unit/parsers/builtin/test_text.py`, `test_log.py`, `test_markdown.py`; `tests/property/parsers/test_text_properties.py`; `tests/security/parsers/test_text_security.py` |
| B | `packages/structuraguard/src/structuraguard/parsers/builtin/delimited.py` | `packages/structuraguard/tests/unit/parsers/builtin/test_delimited.py`; `tests/property/parsers/test_delimited_properties.py`; `tests/security/parsers/test_delimited_security.py` |
| C | `packages/structuraguard/src/structuraguard/parsers/builtin/json_document.py`, `json_lines.py` | `packages/structuraguard/tests/unit/parsers/builtin/test_json.py`; `tests/property/parsers/test_json_properties.py`; `tests/security/parsers/test_json_security.py` |
| D | `packages/structuraguard/src/structuraguard/parsers/builtin/xml.py`, `html.py`, `yaml.py` | `packages/structuraguard/tests/unit/parsers/builtin/test_markup.py`; `tests/property/parsers/test_markup_properties.py`; `tests/security/parsers/test_markup_security.py` |
| E | `packages/structuraguard/src/structuraguard/parsers/builtin/xlsx.py`, `pdf.py`, `docx.py` | `packages/structuraguard/tests/unit/parsers/builtin/test_documents.py`; `tests/property/parsers/test_document_properties.py`; `tests/security/parsers/test_document_security.py`; `tests/integration/parsers/test_documents.py` |
| F | `packages/structuraguard/src/structuraguard/parsers/tika.py`, `_tika_http.py` после core gate | `packages/structuraguard/tests/unit/parsers/builtin/test_tika.py`; `tests/property/parsers/test_tika_properties.py`; `tests/security/parsers/test_tika_security.py`; `tests/integration/test_tika_fake_server.py` |

Во всех строках `tests/...` означает путь относительно
`packages/structuraguard/`. Общая cross-format matrix располагается в
`packages/structuraguard/tests/unit/parsers/builtin/test_selection.py`.

### M4.0. Contract hardening без universal parser

1. Сохранить signature `Parser` и текущую explicit registration model.
2. Принять ADR `docs/adr/0004-lossless-physical-extraction.md` для публичного
   wire-contract:
   - использовать и обобщить существующий bounded
     `ExtensionMetadataEntry` для technical physical metadata; новый
     параллельный metadata DTO не создавать, при необходимости дать старому
     class name совместимый public alias;
   - `PhysicalNodeKind`, raw source name/token и container identity для tree;
   - table shape, stable continuation ID, segment index/terminal marker;
   - ordered refs для смешанной последовательности blocks/tables;
   - exact text span, JSON record/key occurrence, YAML mark, HTML occurrence,
     document part/block position;
   - progressive per-batch indexed refs, чтобы runtime хранил не более 10 000
     selected refs, а не все physical refs;
   - `ProducerMetadata` и canonical `parser_options_fingerprint` в extraction
     manifest согласно ADR 0003; raw regex/options payload в manifest не входит.
3. Изменить `Extracted*` аддитивно и ввести exact version policy:
   - omitted `schema_version` продолжает означать legacy `1.0.0`;
   - новый builder всегда явно emits `1.1.0`;
   - `1.0.0` принимает только legacy shape и default/empty новые fields;
     non-default node/continuation/order metadata требует `1.1.0`;
   - canonical projection `1.0.0` исключает новые default fields, поэтому
     legacy JSON/fingerprint сохраняется; projection `1.1.0` включает их;
   - новый reader round-trips canonical legacy fixtures. Старый reader не обязан
     принять `1.1.0` и получает documented `CONTRACT_VERSION_UNSUPPORTED`; lossy
     down-conversion запрещён.
   Новые union variants и continuation fields являются versioned wire expansion.
   `Parser` signature и root exports не меняются.
4. Оставить current common limits и добавить immutable
   `BatchOptions(batch_size=1000)` и конечные общие caps: `max_batches`,
   `max_columns`, `max_text_chars`, `max_scalar_chars`,
   `max_physical_objects`, `max_processing_seconds`. `max_records` означает
   logical format records и применяется adapter; wrapper отдельно считает
   `max_physical_objects`. Format-specific immutable options принадлежат
   конкретному adapter, а не одному universal config.
5. Добавить shared internal utilities, но не parser dispatch:
   `parsers/_shared/_bounded_source.py`, `_encoding.py`, `_batch_builder.py`,
   `_container_guard.py` и `_hashing.py`. Они не знают format/business
   semantics и не выбирают adapter; registry core не импортирует built-in
   adapter package.
6. Одна pure canonical hash projection исключает собственные fingerprint fields
   и terminal manifest из batch hash input. Builder назначает deterministic
   physical IDs и считает batch/run hashes из фактического output, а
   `ValidatedParserStream` независимо пересчитывает и сравнивает их до yield/
   aggregate success — в том числе для стороннего или будущего sandbox parser.
   Каждый batch несёт bounded `indexed_refs`; core сразу доказывает их наличие в
   текущем batch, хранит только их capped union и на terminal сверяет exact
   equality с `manifest.source_index`. Все physical IDs хранить не требуется.
   Builder всегда выпускает terminal manifest, включая empty source. При
   одинаковых source/parser/options результат byte-stable. При другом
   `batch_size` IDs/fingerprints MAY отличаться, но ordered physical projection
   raw values/locations/container order обязана быть эквивалентна.
7. Добавить built-in codes `PARSER_MALFORMED_INPUT`,
   `PARSER_UNSUPPORTED_FEATURE`, `PARSER_ENCODING_UNSUPPORTED` и
   `SECURITY_INPUT_REJECTED`; сохранить существующие
   `PARSER_DEPENDENCY_UNAVAILABLE`, `PARSER_NO_TEXT_LAYER`,
   `SECURITY_LIMIT_EXCEEDED`, `PROCESSING_TIMEOUT`. Probe и parse boundaries
   reconstruct только allowlisted built-in errors с fixed public messages и
   sanitized details; unexpected exception остаётся `PARSER_PROBE_FAILED` либо
   `PARSER_OUTPUT_INVALID`.

Common parser baselines берутся из `20.9`: `max_file_size_mb=50`,
`max_records=1_000_000`, `max_columns=500`, `max_nested_depth=30`,
`max_text_chars=5_000_000`, `max_processing_seconds=300`. LLM-specific limits
маппятся соответственно на existing `max_bytes`/`max_nesting_depth`, а
LLM-specific limits из того же общего security contract в `ParseContext` не
переносятся, потому что parser не получает LLM authority. Format-specific
defaults фиксируются frozen options и boundary tests; caller override не может
превысить hard cap.

Zero-byte source имеет единственную content-only политику: `PlainTextParser`
считает его пустым TXT и выдаёт empty terminal batch, остальные adapters
возвращают normal unsupported probe. Пустой suffix/MIME не превращает zero-byte
source в CSV, JSON, XML или document format.

### Dependencies/extras policy

| Scope | Dependency/extra | Решение |
| --- | --- | --- |
| Textual core | `charset-normalizer` как direct bounded dependency | BOM и strict UTF сначала; detector применяется только к bounded sample, confidence/encoding входят в probe evidence |
| LOG custom regex | новый extra `logs` с timeout-capable `regex` | Built-in templates работают без extra; user regex без timeout backend даёт `PARSER_DEPENDENCY_UNAVAILABLE`, а не stdlib ReDoS fallback |
| CSV/TSV | stdlib; собственный bounded incremental tokenizer | Не менять process-global `csv.field_size_limit`; dialect detection отдельна от row tokenizer |
| JSON family | incremental event backend (`ijson`) как direct core dependency, если spike подтверждает duplicate-key и numeric-lexeme fidelity | Если fidelity не подтверждена, заменить backend bounded in-house tokenizer до merge C; full-document materialization не является fallback |
| XML | lazy optional extra `xml`: `defusedxml>=0.7.1,<1` (уточнение M4-D) | Hardened incremental events, counters depth/nodes/text/attributes и bounded Expat buffer; XPath/provenance contract см. ADR 0005; network resolver отсутствует |
| HTML | stdlib `html.parser` | Incremental, `convert_charrefs=False`, никакого renderer/fetcher/JS runtime |
| YAML | новый extra `yaml` с `PyYAML` safe event loader | Никакого object construction; aliases/tags/depth считаются до expansion |
| XLSX | существующий `excel`: `openpyxl>=3.1.5,<4` | ZIP/XML preflight выполняет SDK; optional import lazy |
| PDF | существующий `pdf`: `PyMuPDF>=1.26,<2` | Native parser считается high-risk и требует sandbox для strict/untrusted run |
| DOCX | существующий `office`: `python-docx>=1.1.2,<2` | ZIP/XML preflight выполняет SDK; optional import lazy |
| Tika | extra `tika`: HTTPX и defusedxml (уточнение F, ADR 0007) | Запрещены auto-download, implicit server startup и unrestricted network; установка extra не активирует adapter |
| Tests | `hypothesis` в dev group и отдельная parser-test dependency group со всеми extras A–E | Lock, metadata verifier, distribution tests и CI обновляются вместе |

Каждая новая runtime dependency до lock проходит license/maintenance/API/import
side-effect review и получает bounded compatible range. Optional dependency не
импортируется при `import structuraguard` или `import structuraguard.parsers`.

## Группа A. TXT / LOG / MD

Отдельные classes: `PlainTextParser`, `LogParser`, `MarkdownParser`. Они могут
использовать общие encoding/line helpers, но не ветвятся на другие formats.

### Dependencies/extras

- `PlainTextParser`/`MarkdownParser`: textual core dependency для encoding;
- `LogParser`: stdlib fixed Apache/Nginx, timestamp, level, key-value и JSON-log
  recognizers; extra `logs` только для caller-provided regex с per-match timeout.

### Detection/probe signals

- BOM, strict decode result, NUL/control ratio и bounded text-likeness дают
  content MIME evidence; extension/MIME остаются hints.
- Markdown требует устойчивых structural markers (heading/list/fence/link), а
  не только `.md`; ambiguous prose остаётся TXT.
- LOG требует повторяемую структуру нескольких bounded lines. Одна похожая
  строка не вытесняет TXT. Syntactically valid JSON-per-line всегда принадлежит
  `JsonLinesParser`: `LogParser` распознаёт этот overlap и возвращает normal
  unsupported, не выдавая competing strong evidence.
- TXT — последний textual fallback: его evidence слабее подтверждённого
  CSV/JSON/XML/HTML/Markdown/LOG internal structure.

### Physical extracted representation

- TXT: ordered `ExtractedLine`; paragraphs допускаются как physical blocks без
  semantic grouping.
- LOG: исходные lines плюс event block с точным line range; timestamp/level/
  key-value/regex captures сохраняются как raw strings с technical hints.
- MD: ordered paragraph/heading/list/fenced-code blocks и их source lines;
  inline code/HTML никогда не исполняется. Fences и markers сохраняются metadata.

### Streaming/batching

- Читать fixed-size chunks, декодировать incremental decoder, сохранять split
  multibyte/newline state; не читать source целиком.
- Batch boundary допустима только между lines/paragraphs/events/fenced blocks.
  Oversized multiline event/fence fails before unbounded accumulation.
- Первый batch появляется до EOF после `batch_size`, terminal batch содержит
  manifest; empty file даёт один empty terminal batch.

### Exact provenance

- Line/block: one-based `line_start/line_end` и exact text span.
- Capture: line plus start/end columns; multiline event — enclosing range и
  span каждого raw value. Newline convention хранится как technical metadata.

### Configurable limits

`max_line_chars`, `max_block_chars`, `max_event_lines`, `max_event_chars`,
`max_regex_pattern_chars`, `max_capture_groups`, `regex_timeout_ms`, common
bytes/text/records/batches/deadline и `batch_size`.

### Typed errors

- invalid/ambiguous encoding → `PARSER_ENCODING_UNSUPPORTED`;
- malformed configured template/regex → `PARSER_UNSUPPORTED_FEATURE`;
- line/event/regex/deadline overflow → `SECURITY_LIMIT_EXCEEDED` или
  `PROCESSING_TIMEOUT`;
- unexpected decoder/backend defect → `PARSER_OUTPUT_INVALID` на registry
  boundary.

### Fixtures

- Valid/boundary: UTF-8/BOM/UTF-16/legacy Cyrillic, mixed newline and no final
  newline, empty/whitespace, Markdown headings/lists/fences, Apache/Nginx,
  key-value, multiline stack trace, JSON logs, exact batch boundary.
- Malformed/malicious: invalid byte sequence, NUL-heavy binary spoofed as TXT,
  overlong line/event/fence, catastrophic user regex, excessive captures,
  ANSI/control/log-forging text and secret canary in a failing line.

### Contract/property/security tests

- Каждый class проходит expanded parser contract и all-builtins selection test.
- Property tests варьируют read chunk/batch boundary, newline, Unicode split и
  multiline grouping; ordered raw projection/provenance не меняются.
- Security tests доказывают regex timeout, bounded reads/first yield before EOF,
  cancellation cleanup, inert fenced content и отсутствие raw canary в error.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/parsers/builtin/test_text.py
  packages/structuraguard/tests/unit/parsers/builtin/test_log.py
  packages/structuraguard/tests/unit/parsers/builtin/test_markdown.py
  packages/structuraguard/tests/property/parsers/test_text_properties.py
  packages/structuraguard/tests/security/parsers/test_text_security.py
  packages/structuraguard/tests/unit/parsers/builtin/test_selection.py`.

## Группа B. CSV / TSV

Один узкий `DelimitedTextParser` определяет CSV/TSV dialect и возвращает
соответствующий canonical `format_id`; это один format family, а не universal
parser. Header и type/locale guesses остаются observations.

### Dependencies/extras

Textual core encoding helper; внешнего parser extra нет. Incremental tokenizer
реализуется instance-local и не меняет `csv.field_size_limit`, locale или другой
process-global state.

### Detection/probe signals

- Bounded sample проверяет согласованность delimiter, quote, escape и row width
  на нескольких records, включая quoted newlines.
- Tab даёт TSV, выбранный иной delimiter — CSV. Одноколоночный/неустойчивый
  sample не является strong delimited evidence и остаётся TXT.
- Content wins over spoofed MIME/extension; conflicting equally plausible
  dialects fail closed как format ambiguity, а не выбираются случайно.

### Physical extracted representation

- Одна logical table делится на `ExtractedTable` segments с stable container ID.
  Каждая source cell остаётся raw decoded string; header row не превращается в
  field names, duplicates не переименовываются.
- Missing cell, explicit empty quoted/unquoted cell, extra cell, blank row и
  trailing empty columns различимы. Dialect/header/locale/type candidates и
  physical row count/shape хранятся bounded technical metadata.

### Streaming/batching

- Incremental tokenizer поддерживает quoted newline across chunks и выдаёт
  row batches без materialization whole table.
- Batch граница только между logical rows; table segment order/terminal marker
  непрерывны. Header probing использует bounded prefix, затем те же bytes
  replay через budgeted reader без повторного небounded чтения.
- `batch_size` задаёт target rows; adapter делает более ранний flush по bounded
  числу cells/decoded chars (включая line terminators и blank rows) и даёт
  cancellation выполниться между probe slices и emitted batches.

### Exact provenance

Zero-based physical row/column в `TabularCellLocation`, stable logical table ID
и непрерывный segment/row range. Header/data rows используют те же координаты;
provenance не зависит от deduplication, потому что её нет. Lexical/byte span
исходного record/cell не входит в physical schema `1.1.0` и требует отдельного
schema decision; adapter не синтезирует неточные offsets из decoded value.

### Configurable limits

`max_columns`, `max_records`, `max_field_size` (decoded code points),
`max_record_chars`, `max_batch_cells`, `max_batch_chars`,
`max_header_probe_rows`, `max_dialect_candidates`, common bytes/text/batches/
deadline и `batch_size`.

### Typed errors

- unterminated quote/invalid escape/inconsistent record beyond configured policy
  → `PARSER_MALFORMED_INPUT`;
- unsupported dialect or encoding → `PARSER_UNSUPPORTED_FEATURE`/
  `PARSER_ENCODING_UNSUPPORTED`;
- columns/cell/record/records overflow → `SECURITY_LIMIT_EXCEEDED`.

### Fixtures

- Valid/boundary: CSV/TSV, BOM/legacy encoding, custom delimiter/quote/escape,
  quoted newline, duplicate headers, no header, empty vs missing, extra cells,
  blank row, locale-looking numbers/dates, exact row/batch boundary.
- Malformed/malicious: unterminated quotes, delimiter ambiguity, binary spoof,
  too many columns, huge cell/record, newline storm, formula-like values starting
  with `=`, `+`, `-`, `@` and secret canary in malformed row. Formula-like raw
  text must remain unchanged and inert.

### Contract/property/security tests

- Contract asserts raw cell equality, all physical coordinates, continuation
  metadata and deterministic probe dialect.
- Hypothesis generates dialect-valid tables and adversarial chunk boundaries;
  parse projection equals reference rows and is invariant to batch size.
- Security tests cover caps before allocation, no global CSV/locale mutation,
  cancellation, spoofed extension/MIME and redacted errors.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/parsers/builtin/test_delimited.py
  packages/structuraguard/tests/property/parsers/test_delimited_properties.py
  packages/structuraguard/tests/security/parsers/test_delimited_security.py
  packages/structuraguard/tests/unit/parsers/builtin/test_selection.py`.

## Группа C. JSON / JSONL / NDJSON

Classes: `JsonDocumentParser` для object/top-level array и `JsonLinesParser` для
JSONL/NDJSON aliases. Они используют общий JSON event representation, но имеют
разные record-boundary/probe rules.

### Dependencies/extras

Incremental event backend входит в textual core после fidelity/security spike.
Backend обязан сохранить duplicate object members, scalar token lexeme и order;
иначе группа использует собственный bounded tokenizer. `orjson`/full-DOM
fallback не добавляется.

### Detection/probe signals

- Strict JSON token grammar и complete bounded prefix/document confirm JSON;
  comments, NaN/Infinity and trailing garbage do not.
- JSONL/NDJSON требуют несколько independently complete JSON values separated
  by physical lines; blank-line policy deterministic.
- Syntactically valid JSONL, включая JSON logs со timestamp/level keys, всегда
  выбирает `JsonLinesParser`. `LogParser` declines этот content; expected winner
  и physical tree projection не зависят от score/priority.

### Physical extracted representation

- Object/array/scalar/container kinds, ordered members/items, empty containers,
  duplicate keys и arbitrary raw keys сохраняются в tree without flatten.
- Numbers keep original lexical string plus technical numeric hint; strings,
  booleans and null remain tagged source scalars. Child arrays/entities не
  назначаются parser-ом.
- JSONL record — отдельный complete tree/subtree с global record index.

### Streaming/batching

- JSONL читается line/event incrementally; monolithic top-level array выдаёт
  complete item subtrees по мере parsing. Один огромный indivisible object не
  разрезается посередине token/subtree и fails по scalar/object limits.
- Parent/continuation identity сохраняет top-level container across batches;
  terminal manifest не требует materialization всех nodes/refs.

### Exact provenance

RFC 6901 pointer плюс document/record index и duplicate-key occurrence. Для
JSONL добавляется line/text span; escaping `~0`/`~1`, empty keys и array indices
contract-tested.

### Configurable limits

`max_nesting_depth`, `max_nodes`, `max_object_members`, `max_array_items`,
`max_scalar_chars`, `max_number_chars`, `max_record_chars`, common bytes/text/
records/batches/deadline и `batch_size`.

### Typed errors

- invalid token/trailing content/broken JSONL record → `PARSER_MALFORMED_INPUT`;
- unsupported encoding or optional backend → соответствующий encoding/dependency
  code;
- depth/node/member/scalar/record overflow → `SECURITY_LIMIT_EXCEEDED`.

### Fixtures

- Valid/boundary: root scalar/object/array, empty containers, nested members,
  arrays of child objects, duplicate/empty/escaped keys, exact numeric lexemes,
  JSONL/NDJSON with and without final newline, blank-line policy and boundaries.
- Malformed/malicious: truncated token, duplicate separators, trailing garbage,
  NaN/Infinity, invalid surrogate/UTF-8, huge number/string, deep nesting,
  massive single record, injection strings and secret canary near syntax error.

### Contract/property/security tests

- Contract verifies lossless ordered tree, pointer occurrence and no flattened
  keys/business names.
- Property tests generate JSON trees/JSONL, vary reader chunks/batches and check
  deterministic projection, pointers and incremental first yield.
- Security tests prove depth/token/node budgets before growth, cancellation
  cleanup and safe malformed context.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/parsers/builtin/test_json.py
  packages/structuraguard/tests/property/parsers/test_json_properties.py
  packages/structuraguard/tests/security/parsers/test_json_security.py
  packages/structuraguard/tests/unit/parsers/builtin/test_selection.py`.

## Группа D. XML / HTML / YAML

Реализовано 2026-09-09. Точные гарантии и уточнения исходного плана закреплены
в [ADR 0005](../adr/0005-safe-markup-extraction.md): XML dependency стала
optional extra по текущему требованию пользователя; XPath использует URI
predicates; HTML CSS относится к source-event DOM и start positions; YAML
batching выполняется по bounded documents. Ни один adapter не определяет schema.

Separate `XmlParser`, `HtmlParser`, `YamlParser`; shared code ограничено bounded
text input, batch builder и generic counters.

### Dependencies/extras

- XML: lazy `xml` extra (`defusedxml`) plus stdlib primitives;
- HTML: stdlib incremental parser only;
- YAML: lazy `yaml` extra with safe event loader. Missing extra produces
  `PARSER_DEPENDENCY_UNAVAILABLE` only after YAML content evidence.

### Detection/probe signals

- XML: declaration/root/name grammar and safe bounded parse; DTD presence is
  security rejection, not evidence.
- HTML: HTML doctype/root/tag-soup structure. XHTML namespace is classified by
  one documented rule so XML/HTML adapters never claim incompatible maximal
  formats.
- YAML: directives/document markers, mapping/sequence/scalar event grammar;
  strict JSON-shaped source is declined so JSON (a YAML subset) wins.

### Physical extracted representation

- XML: ordered elements, attributes, text/tail, namespace URI/local name and
  empty elements as typed tree nodes; repeated elements remain siblings.
  Namespace prefix в provenance генерируется канонически из URI. Исходный
  lexical prefix сохраняется optional metadata только если hardened event
  backend отдаёт его без реконструкции; `<x/>` и `<x></x>` считаются одной
  physical structure, но raw text/attribute values не нормализуются.
- HTML: DOM/order, tables, lists, headings, metadata, `data-*`, text blocks and
  forms; scripts/styles/iframe/resource attributes remain inert flagged raw
  nodes and are never fetched/rendered.
- YAML: document/mapping/sequence/scalar/anchor/alias/tag/style events with raw
  scalar lexemes; no Python object construction and no destructive merge-key
  expansion.

### Streaming/batching

- XML/HTML use event parsing and release completed subtrees. Batch boundaries
  occur at complete repeated element/DOM block; one oversized subtree fails.
- YAML streams by complete document/top-level item from safe events; aliases are
  represented, not recursively expanded. No format builds an unbounded DOM.

### Exact provenance

- XML: namespace-aware absolute XPath с canonical URI-bound prefix, sibling
  occurrence и attribute/text/tail suffix; line/column только когда backend
  отдаёт их точно. Backend spike обязан подтвердить эти свойства; иначе D не
  merge до supplementary bounded lexical capture.
- HTML: deterministic CSS selector with `:nth-of-type`/attribute occurrence plus
  original line/column span.
- YAML: document index, safe path, start/end line+column, anchor/alias occurrence.

### Configurable limits

Common depth/text/bytes/records/batches/deadline plus `max_nodes`,
`max_attributes_per_node`, `max_name_chars`, `max_text_node_chars`,
`max_namespace_count`, `max_yaml_documents`, `max_aliases`, `max_anchors` and
`max_dom_block_chars`.

### Typed errors

- malformed markup/YAML → `PARSER_MALFORMED_INPUT`;
- unsupported YAML tag/merge semantics or encrypted/custom feature →
  `PARSER_UNSUPPORTED_FEATURE`;
- DTD/entity/external resolver, unsafe YAML tag/object construction →
  `SECURITY_INPUT_REJECTED`;
- node/depth/text/alias/deadline overflow → limit/timeout codes.

### Fixtures

- XML: namespaces, attributes, mixed content/tail, repeated/empty elements,
  malformed close tags, internal/external DTD/XXE, entity expansion, deep/wide/
  long-text boundary.
- HTML: tables/lists/headings/meta/data/forms, malformed tag soup, duplicate
  attributes, script, iframe, external link/image/style, deep DOM and huge attr.
- YAML: mapping/sequence/multi-doc, anchors/aliases/tags/styles, duplicate/empty
  keys, malformed indentation, `!!python/object`, custom constructors, alias
  bomb, recursive alias, deep nesting and JSON-subset source.

### Contract/property/security tests

- Contract asserts node roles/order/raw names, exact XPath/CSS/YAML marks and
  cross-batch container continuity.
- Properties vary namespaces, sibling counts, HTML chunk boundaries and YAML
  event/batch boundaries; parse projection remains deterministic.
- Network/file/open/constructor/JavaScript sentinels prove no XXE, fetch or code
  execution; alias/entity bombs hit limits and errors redact source content.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/parsers/builtin/test_markup.py
  packages/structuraguard/tests/property/parsers/test_markup_properties.py
  packages/structuraguard/tests/security/parsers/test_markup_security.py
  packages/structuraguard/tests/unit/parsers/builtin/test_selection.py`.

## Группа E. XLSX / PDF / DOCX

Separate lazy optional `XlsxParser`, `PdfParser`, `DocxParser`. Installation of
one extra does not import or activate the others.

### Dependencies/extras

Используются существующие `excel`, `pdf`, `office`. Common bounded seekable
spool and container guard остаются SDK code. Native/binary parser versions are
locked and tested per supported Python/platform matrix.

### Detection/probe signals

- XLSX/DOCX: ZIP signature alone insufficient; required `[Content_Types].xml`,
  relationships and format-specific parts confirm internal structure. Generic
  ZIP, XLSM/DOCM and spoofed extension do not become valid XLSX/DOCX.
- PDF: `%PDF-` signature в probe; structural validation выполняется в worker
  до extraction и обозначена warning. Declared MIME/name — hints.
  Encrypted/malformed files дают typed error при parsing.

### Physical extracted representation

- XLSX: bounded direct OOXML extraction сохраняет workbook/sheet order and
  visibility, worksheet/table segments, blank-vs-absent cells, merged ranges,
  raw `<v>`/shared-string lexeme, style/type hints and formula `<f>` separately.
  Cached value и formula source являются двумя tagged physical entries; ни одно
  не перезаписывает другое. `openpyxl` используется только для supplementary
  validated table/style interpretation, не как source of raw fidelity. Parser
  никогда не вычисляет formula; header candidates остаются technical
  observations, date serial не нормализуется в `datetime`.
- PDF: ordered page/block/line text layer, tables, page metadata and bounding
  boxes. OCR/images are absent by design.
- DOCX: ordered paragraphs/headings/lists/tables/properties with explicit mixed
  document order. Runs/style/list level are technical metadata, not semantics.

### Streaming/batching

- Source is copied through cumulative byte/deadline limits to bounded seekable
  spool; ZIP preflight completes before library open. Decompressed members are
  streamed where backend permits; any materialized XML part/page is bounded by
  preflight caps and measured separately, а не называется fully streaming.
- XLSX batches complete rows/table segments; PDF batches complete pages/blocks;
  DOCX batches complete ordered blocks. Sync library work runs outside the event
  loop through a bounded subprocess/pipe. Cancellation/timeout уничтожают worker
  и закрывают spool; это lifecycle isolation, не OS sandbox M12. Платформенные
  memory guarantees и strict fail-closed policy описаны в ADR 0006.

### Exact provenance

- XLSX: sheet raw name plus zero-based row/column, A1 coordinate in metadata,
  table/merged-range/segment identity.
- PDF: one-based page, block/line ID and normalized bounding box; table cell
  references include page/table/row/column.
- DOCX: package part, zero-based block order, paragraph/run or table row/cell
  path; mixed item refs preserve interleaving.

### Configurable limits

- Container: `max_archive_size`, `max_extracted_size`, `max_file_count`,
  `max_archive_nesting`, `max_compression_ratio`, `max_member_size`.
- XLSX: `max_sheets`, `max_rows_per_sheet`, `max_columns`, `max_cells`,
  `max_shared_string_chars`, `max_merged_ranges`.
- PDF: `max_pages`, `max_objects`, `max_blocks_per_page`, `max_text_chars`,
  `max_tables_per_page`.
- DOCX: `max_parts`, `max_blocks`, `max_runs_per_paragraph`, `max_table_cells`,
  `max_relationships`; плюс common batches/deadline/spool caps.

### Typed errors

- malformed ZIP/XML/PDF structure → `PARSER_MALFORMED_INPUT`;
- encrypted PDF, unsupported workbook/document feature →
  `PARSER_UNSUPPORTED_FEATURE`; PDF without text layer →
  `PARSER_NO_TEXT_LAYER`;
- missing optional library → `PARSER_DEPENDENCY_UNAVAILABLE`;
- traversal/symlink/executable/macro/external relationship/forbidden action →
  `SECURITY_INPUT_REJECTED`; expansion/pages/objects/deadline → limit/timeout.

### Fixtures

- XLSX: multi/hidden sheets, headers, blanks, merged cells, dates/serials,
  cached/uncached formulas, tables and exact row boundary.
- PDF: multi-page text, blocks/lines/tables/bboxes, metadata, empty page,
  scanned/no-text, encrypted and malformed xref/object stream.
- DOCX: interleaved paragraphs/tables, headings/lists, properties, runs and
  exact block boundary.
- Malicious containers: traversal/absolute/symlink entries, duplicate names,
  ZIP/compression bomb generated small in tests, oversized part/shared strings,
  macro-enabled package, embedded executable/OLE, external relationship, PDF
  JavaScript/launch action/embedded file and secret canary in corrupt metadata.

### Contract/property/security tests

- Contract checks exact raw cell/text values, document order, provenance,
  formula non-execution and terminal manifest for every adapter.
- Properties generate small XLSX/DOCX packages/physical tables and vary output
  batch sizes; PDF geometry uses deterministic golden fixtures rather than
  renderer-dependent text guessing.
- Security tests execute container preflight before optional library, patch
  network/process/formula/macro hooks with sentinels, enforce spool/expansion/
  page limits, cancellation cleanup and error redaction.
- Integration tests run each real optional dependency; missing-extra behavior
  remains a separate base-environment unit test.
- Проверка: base unit/property/security files запускаются обычным `pytest`;
  real-library matrix — новым `make test-integration`; aggregate security corpus
  — новым `make test-security`.

### Реализованный slice E

Независимые `XlsxParser`, `PdfParser`, `DocxParser` реализованы с optional extras,
bounded document worker и fixture-based contract/security/property tests.
Нормативные детали и ограничения реализации: [ADR 0006](../adr/0006-bounded-document-adapters.md).
Именованные Excel tables представлены definitions поверх worksheet cells;
DOCX inherited layout/styles и PDF image/OCR extraction не обещаются. Полный
M12 sandbox не реализован, strict mode отказывает до чтения. Новые targets
`make test-integration` и `make test-security` выделяют соответствующие suites.

## Группа F. Tika fallback — только после core gate

F не входит в обязательное завершение M4. Решение о реализации принимается,
только если A–E, all-builtins conflicts, security suite, docs и package gates
зелёные.

Реализовано по отдельному запросу пользователя после зелёного прогона A–E
(`1380 passed` на том этапе), но не полной приёмки всех AC: отдельный opt-in
adapter. Network/container isolation обеспечивает caller;
прежнее ожидание SDK sandbox M12 заменено этим явным deployment contract
([ADR 0007](../adr/0007-opt-in-tika-egress.md)).

### Dependencies/extras

Extra `tika` содержит HTTPX и defusedxml; прежний reserved bundle `tika-python`
заменён клиентом без управления Java runtime. Нельзя
автоматически скачивать JAR, стартовать service, обращаться к произвольному URL
или импортировать/активировать Tika при package/facade construction. Допустим
только caller-provisioned pinned service с явным endpoint и allowlist.
Изоляция сервера не предоставляется SDK; `all` не означает запуск.

### Detection/probe signals

Tika не подтверждает ни один format A–E и не участвует в их ranking. Он получает
только explicit allowlist fallback media types. Composition owner вызывает его
в отдельной fallback-only registry/session после `PARSER_UNSUPPORTED_FORMAT`
core selection; M4 не меняет orchestrator. Local bounded signature probe не
обращается к Tika service. Priority не используется для маскировки
`PARSER_FORMAT_CONFLICT`.

Начальные signatures: `application/rtf` и `application/postscript`, до 16 байт.
Approval на проверенный PUBLIC snapshot обязателен до upload; secrets запрещены.

### Physical extracted representation

Только XHTML tree, реально возвращённый Tika: blocks/tables/metadata остаются
элементами/атрибутами/текстом с raw values и физическими связями. Версионированная
`ExtensionLocation` фиксирует response-relative fidelity; никаких final entities,
flatten или выдуманных source page/block offsets.

### Streaming/batching

Bounded snapshot spool до upload, bounded raw response stream и полная проверка
XHTML до первой выдачи. Далее XML units/batches и continuation root с terminal
manifest; response после transport failure отбрасывается. Неделимый oversized
subtree отклоняется; server memory/CPU/pids ограничивает caller.

### Exact provenance

`tika:xhtml-v1` связывает source fingerprint, полный response SHA-256, XPath и
configured server version. Последняя является декларацией caller, не результатом
удалённой attestation. Координаты относятся к XHTML response, не исходному файлу.

### Configurable limits

`TikaParserLimits`: `max_request_bytes`, `max_response_bytes`, `max_header_bytes`,
`read_chunk_bytes`, `timeout_seconds`, вложенные `XmlParserLimits`. Также действуют
`ParseContext` records/depth/physical objects/batches и configurable batch size.
Transport limits имеют finite defaults/hard caps; проверяются exact byte boundaries,
XML limits и cancellation. Allowlist MIME ограничивает вход, response допускает
только UTF-8 XHTML без compression. Remote URL/body не попадают в public errors.

### Typed errors

- Отсутствующий extra → `PARSER_DEPENDENCY_UNAVAILABLE`, service unavailable →
  `PARSER_TIKA_UNAVAILABLE`, malformed response → `PARSER_MALFORMED_INPUT`,
  timeout → `PROCESSING_TIMEOUT`; oversize/egress policy → существующие typed
  security errors. Выключенный adapter → `PARSER_UNSUPPORTED_FEATURE`.

### Fixtures

- Fixtures: allowlisted non-core formats, core formats как negative probe,
  missing extra/unavailable server, malformed/oversized response, timeout,
  cancellation, hostile metadata, secret markers в конце snapshot и подмена SHA.

### Contract/property/security tests

- Conditional adapter проходит тот же valid/empty/malformed/boundary/
  cancellation contract; property test дробит identical bounded response на
  разные chunks/batches и требует одинаковую physical projection/provenance.
- Default tests используют fake transport и ephemeral loopback HTTP server:
  никакого внешнего Tika/JVM/Docker. Проверяются no auto-download/redirects,
  отсутствие ambient credentials/proxies/cookies, cleanup и typed errors.
  Фактическая изоляция, pinning и format fidelity сервера требуют deployment
  acceptance вызывающего проекта; fake server этого не доказывает.
- Проверка при допуске F: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/parsers/builtin/test_tika.py
  packages/structuraguard/tests/property/parsers/test_tika_properties.py
  packages/structuraguard/tests/security/parsers/test_tika_security.py`;
  loopback fake HTTP — `make test-integration`.

## Расширение contract/property/security suite

`tests/contract_suites/parser.py` получает отдельные reusable case types, а не
одну функцию, материализующую весь stream:

- valid physical case с expected probe signals, raw projection и provenance;
- empty, unsupported, malformed, encoding, dependency unavailable;
- exact boundary `limit - 1 / limit / limit + 1` для каждого resource;
- cancellation до first yield, между batches и во время backend/container read;
- malicious case с expected error class/code и canary-redaction assertions;
- streaming observer: cumulative/request read caps, first yield, max resident
  batch, terminal manifest и cleanup;
- deterministic reparse и batch-size equivalence по physical projection;
- all-builtins selection matrix для TXT fallback, CSV/TSV, JSON/JSONL/log,
  JSON-vs-YAML, XML-vs-HTML и ZIP XLSX-vs-DOCX.

Fixture layout:

```text
packages/structuraguard/tests/fixtures/parsers/
    text/  delimited/  json/  markup/  documents/  tika/
packages/structuraguard/tests/property/parsers/
packages/structuraguard/tests/security/parsers/
packages/structuraguard/tests/integration/parsers/
```

Большие files/bombs не коммитятся: test builders создают минимальный synthetic
payload с малым compressed size. Static binaries имеют manifest с expected
format/provenance, SHA-256, origin/license и причиной хранения.

## Критерии приёмки

Наблюдаемые tests, найденные regressions и непроверенные части каждого критерия:
[аудит приёмки M4](M04_acceptance_audit.md). Зелёный suite не означает автоматическое
выполнение всех resource/deployment gates ниже.

Отдельный review текущего diff и применимых угроз:
[security review M4](M04_security_review.md).

- [x] **AC-01.** `Parser` signature, explicit instance-local registry и пустые
  default facades совместимы с M3; каждый adapter регистрируется отдельно без
  orchestrator changes.
- [x] **AC-02.** M4.0 DTO выражают lossless tree/table/document order,
  cross-batch continuation и exact provenance; legacy payloads проходят
  documented minor-version compatibility tests.
- [x] **AC-03.** Parser output содержит только physical `ExtractedBatch`; static
  tests запрещают `ParsePlan`, `NormalizedBatch`, business/target names, LLM/DB
  imports, network fetch, code/formula/macro execution и global mutable state.
  Единственное исключение HTTP imports — opt-in Tika boundary из ADR 0007;
  запрет resource fetch остальных adapters сохраняется.
- [x] **AC-04.** Все adapters используют strong content evidence; aggregate
  selection matrix не даёт generic TXT/Tika перехватить structured format и не
  выбирает по MIME/extension alone.
- [x] **AC-05.** Raw values, missing/empty/duplicate/order distinctions и
  format-specific physical structure сохраняются без flatten/normalization.
- [ ] **AC-06.** Streamable formats выдают bounded batches до EOF; container
  formats используют bounded spool/preflight и documented measured peak for
  each materialized part/backend call. Core tracking ограничено current batch,
  summaries и 10k progressive index refs; hard CPU kill native parser остаётся
  capability sandbox M12.
  **Частично:** streaming/tracking/lifecycle tests проходят, но нет measured
  RSS/CPU peak для каждого materialized part/native call на большом corpus.
- [ ] **AC-07.** Каждый common/format limit имеет safe finite default, validated
  override, boundary tests и stable resource name; failure закрывает stream и
  не создаёт accepted manifest.
  **Частично:** finite defaults/overrides и critical boundaries проверены;
  исчерпывающей N−1/N/N+1 matrix всех markup/document limits нет.
- [x] **AC-08.** Malformed input, unsupported feature/dependency, no text layer,
  security rejection, limit and timeout остаются различимыми typed outcomes;
  unexpected backend defects не маскируются format errors.
- [x] **AC-09.** XML/HTML/YAML and archive/document malicious suites доказывают
  отсутствие XXE/network/object construction/JS/macro/formula/executable
  behavior, traversal, archive bomb and raw error leakage.
- [x] **AC-10.** Valid/malformed/empty/encoding/boundary/oversized/cancellation/
  malicious fixtures, contract tests and properties существуют для A–E.
- [ ] **AC-11.** Optional dependencies lazy, base install выдаёт точный
  dependency outcome only for content-matched format, import/package side-effect
  tests зелёные на base и all-extras environments.
  **Частично:** locked dev parser extras и isolated base wheel/sdist проверены;
  отдельное полное `all` окружение с DB/LLM extras не проверено.
- [ ] **AC-12.** F либо проходит отдельный sandbox/no-autodownload gate после
  A–E, включая собственные dependency/detection/representation/streaming/
  provenance/limits/errors/fixtures/contract-property-security evidence, либо
  остаётся явно deferred без влияния на core M4 status.
  **Частично:** client gate проходит fake-server suites по ADR 0007; реальный
  Tika/JVM deployment, DLP и network/container isolation caller не проверены.
  F выключен по умолчанию; этот deployment gate не блокирует обязательный core.

Отметки выше относятся к наблюдаемому corpus и fidelity ADR 0004–0007, а не ко
всем возможным документам. Regression tests финального review дополнительно
проверяют DOCX text/provenance, JSON/YAML ownership и encoding identity группы A:
`test_m04_review_regressions.py`, `test_m04_probe_regressions.py`. Незакрытые
критерии не переведены в completed из-за зелёного общего suite.

## Вертикальные шаги реализации

### 1. M4.0 — зафиксировать lossless contracts, limits и typed errors

- Тесты сначала: дополнить
  `packages/structuraguard/tests/unit/contracts/test_m02_contracts.py`,
  `packages/structuraguard/tests/unit/contracts/test_m02_invariants.py`,
  `packages/structuraguard/tests/unit/contracts/test_m02_review_regressions_streams.py`,
  `packages/structuraguard/tests/unit/parsers/test_parser_output_contract.py` и
  `packages/structuraguard/tests/unit/ports/test_m03_parser_static.py` новыми
  wire/version, legacy canonical fingerprint, continuation, ordered-document,
  progressive-index, forged hash, probe/parse error matrix, memory/limit и
  architecture cases.
- Файлы: `packages/structuraguard/src/structuraguard/contracts/common.py`,
  `packages/structuraguard/src/structuraguard/contracts/source.py`,
  `packages/structuraguard/src/structuraguard/contracts/__init__.py`,
  `packages/structuraguard/src/structuraguard/ports/source.py`,
  `packages/structuraguard/src/structuraguard/ports/__init__.py`,
  `packages/structuraguard/src/structuraguard/parsers/selection.py`,
  `packages/structuraguard/src/structuraguard/parsers/execution.py`,
  `packages/structuraguard/src/structuraguard/exceptions.py` и
  `docs/adr/0004-lossless-physical-extraction.md`.
- Поведение: exact `1.0.0`/`1.1.0` compatibility, bounded core tracking,
  independent hash verification и различимые safe probe/parse outcomes.
  `SECURITY_INPUT_REJECTED` восстанавливается как `SecurityPolicyError`,
  остальные allowlisted format codes — как `ParserError`; signature `Parser`
  не меняется.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/contracts/test_m02_contracts.py
  packages/structuraguard/tests/unit/contracts/test_m02_invariants.py
  packages/structuraguard/tests/unit/contracts/test_m02_review_regressions_streams.py
  packages/structuraguard/tests/unit/parsers/test_parser_output_contract.py
  packages/structuraguard/tests/unit/ports/test_m03_parser_static.py`; затем
  `make typecheck`.

### 2. Добавить shared bounded utilities и expanded contract suite

- Тесты сначала: создать
  `packages/structuraguard/tests/unit/parsers/builtin/test_utilities.py` и
  расширить `packages/structuraguard/tests/contract_suites/parser.py`/
  `packages/structuraguard/tests/fakes/parsers.py` cases для short reads,
  over-read, deadline, deterministic IDs/fingerprints, terminal empty/
  multi-batch, first-yield observer, indexed-ref proof and cleanup.
- Файлы: internal modules под
  `packages/structuraguard/src/structuraguard/parsers/_shared/`:
  `_bounded_source.py`, `_encoding.py`, `_batch_builder.py`,
  `_container_guard.py`, `_hashing.py` и `__init__.py`.
- Поведение: reusable bounded mechanics без format dispatch; builder и core
  verifier используют одну pure projection, но считают hashes независимо;
  import не выполняет I/O и не загружает optional libraries.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/contract_suites
  packages/structuraguard/tests/unit/parsers/test_parser_output_contract.py
  packages/structuraguard/tests/unit/parsers/test_selection.py
  packages/structuraguard/tests/unit/parsers/test_registry.py
  packages/structuraguard/tests/unit/parsers/builtin/test_utilities.py`.

### 3. Подготовить dependencies и отдельные test environments

- Тесты сначала: обновить expected dependency/extras/CI contracts в
  `packages/structuraguard/tests/packaging/test_metadata.py`,
  `packages/structuraguard/tests/packaging/test_distribution_verifier.py`,
  `packages/structuraguard/tests/packaging/test_ci_contract.py` и
  `packages/structuraguard/tests/smoke/test_dependency_boundary.py`.
- Файлы: `packages/structuraguard/pyproject.toml`, root `pyproject.toml`,
  `uv.lock`, `scripts/verify_distribution.py`, `Makefile` и
  `.github/workflows/ci.yml`.
- Поведение: direct core dependencies и `logs`/`yaml` extras имеют bounded
  ranges; `hypothesis` и A–E libraries доступны отдельному parser-test group.
  Base CI проверяет lazy/missing-extra behavior; parser CI запускает property,
  security и real-library integration. `make test-security` и
  `make test-integration` появляются до написания format tests.
- Проверка: `make sync`; `make lock-check`; `uv run --locked --no-sync pytest
  packages/structuraguard/tests/packaging/test_metadata.py
  packages/structuraguard/tests/packaging/test_distribution_verifier.py
  packages/structuraguard/tests/packaging/test_ci_contract.py
  packages/structuraguard/tests/smoke/test_dependency_boundary.py`.

### 4. Реализовать группу A

- Тесты сначала: создать A files из module map и fixtures
  `packages/structuraguard/tests/fixtures/parsers/text/`.
- Файлы: `packages/structuraguard/src/structuraguard/parsers/builtin/text.py`,
  `log.py`, `markdown.py` и explicit exports в sibling `__init__.py`.
- Поведение: deterministic text/Markdown/log ownership, incremental decode,
  complete line/block/event batches, exact spans; JSON lines declines to C.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/parsers/builtin/test_text.py
  packages/structuraguard/tests/unit/parsers/builtin/test_log.py
  packages/structuraguard/tests/unit/parsers/builtin/test_markdown.py
  packages/structuraguard/tests/property/parsers/test_text_properties.py
  packages/structuraguard/tests/security/parsers/test_text_security.py
  packages/structuraguard/tests/unit/parsers/builtin/test_selection.py
  packages/structuraguard/tests/unit/parsers/test_selection.py
  packages/structuraguard/tests/unit/parsers/test_parser_output_contract.py`.

### 5. Реализовать группу B

- Тесты сначала: создать B files из module map и fixtures
  `packages/structuraguard/tests/fixtures/parsers/delimited/`.
- Файлы: `packages/structuraguard/src/structuraguard/parsers/builtin/delimited.py`
  и sibling `__init__.py`.
- Поведение: one-family CSV/TSV dialect detection, raw cells and table
  continuation; no header renaming, type conversion or global CSV/locale state.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/parsers/builtin/test_delimited.py
  packages/structuraguard/tests/property/parsers/test_delimited_properties.py
  packages/structuraguard/tests/security/parsers/test_delimited_security.py
  packages/structuraguard/tests/unit/parsers/builtin/test_selection.py`.

### 6. Реализовать группу C

- Тесты сначала: создать C files из module map и fixtures
  `packages/structuraguard/tests/fixtures/parsers/json/`, включая backend fidelity
  contract для duplicate keys/numeric lexemes.
- Файлы:
  `packages/structuraguard/src/structuraguard/parsers/builtin/json_document.py`,
  `packages/structuraguard/src/structuraguard/parsers/builtin/json_lines.py` и
  sibling `__init__.py`.
- Поведение: lossless event trees without flatten, JSONL canonical ownership,
  streaming top-level records and exact pointer occurrence. Backend, который не
  проходит fidelity tests, не merge.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/parsers/builtin/test_json.py
  packages/structuraguard/tests/property/parsers/test_json_properties.py
  packages/structuraguard/tests/security/parsers/test_json_security.py
  packages/structuraguard/tests/unit/parsers/builtin/test_selection.py
  packages/structuraguard/tests/unit/parsers/test_registry.py
  packages/structuraguard/tests/unit/parsers/test_selection.py
  packages/structuraguard/tests/unit/parsers/test_parser_output_contract.py`.

### 7. Реализовать группу D

- Тесты сначала: создать D files из module map и fixtures
  `packages/structuraguard/tests/fixtures/parsers/markup/`; XML backend spike
  фиксируется executable namespace/XPath/position tests.
- Файлы: `packages/structuraguard/src/structuraguard/parsers/builtin/xml.py`,
  `html.py`, `yaml.py` и sibling `__init__.py`.
- Поведение: separate secure event adapters, no DTD/XXE/fetch/JS/object
  construction/alias expansion, exact documented provenance and bounded trees.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/parsers/builtin/test_markup.py
  packages/structuraguard/tests/property/parsers/test_markup_properties.py
  packages/structuraguard/tests/security/parsers/test_markup_security.py
  packages/structuraguard/tests/unit/parsers/builtin/test_selection.py
  packages/structuraguard/tests/smoke/test_dependency_boundary.py
  packages/structuraguard/tests/smoke/test_import_side_effects.py`.

### 8. Реализовать группу E

- Тесты сначала: создать E files из module map и fixtures/builders
  `packages/structuraguard/tests/fixtures/parsers/documents/`, включая OOXML raw
  formula/cache/date serial and container exploits.
- Файлы: `packages/structuraguard/src/structuraguard/parsers/builtin/xlsx.py`,
  `pdf.py`, `docx.py` и sibling `__init__.py`.
- Поведение: bounded container preflight до library call, direct OOXML raw
  extraction, PDF text layer only, ordered DOCX blocks, lazy extras and
  cooperative cancellation with honest native-call limits.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/parsers/builtin/test_documents.py
  packages/structuraguard/tests/property/parsers/test_document_properties.py
  packages/structuraguard/tests/security/parsers/test_document_security.py
  packages/structuraguard/tests/unit/parsers/builtin/test_selection.py`; затем
  `make test-integration`.

### 9. Выполнить core gate и отдельно решить F

- Core gate: A–E acceptance, aggregate selection, all parser security tests,
  measured memory/deadline evidence, docs/build/package checks.
- Если gate green, оформить Tika threat/deployment decision. При реализации
  создать F files из module map, использовать caller-owned fallback-only
  registry/session и выполнить exact F command плюс
  `packages/structuraguard/tests/integration/test_tika_fake_server.py` через
  `make test-integration`.
- По уточнению ADR 0007 client merge проверяет no-autodownload/egress/lifecycle
  через fake server. Если caller не подтвердил изоляцию реального сервера,
  deployment F остаётся выключенным; core M4 status не блокируется.
- Проверка core gate: `make lint`; `make typecheck`; `make test`;
  `make test-security`; `make test-integration`.

### 10. Обновить docs/version/package и выполнить финальный review

- Тесты сначала: создать
  `packages/structuraguard/tests/docs/test_m04_architecture_contract.py` и
  обновить `packages/structuraguard/tests/packaging/test_metadata.py`,
  `packages/structuraguard/tests/packaging/test_distribution_verifier.py`,
  `packages/structuraguard/tests/unit/test_public_exports.py` на expected M4 docs,
  distribution content, lazy exports and version `0.4.0`.
- Файлы: `packages/structuraguard/pyproject.toml`, `uv.lock`,
  `scripts/verify_distribution.py`, `docs/architecture.md`,
  `docs/public-api.md`, `docs/threat-model.md`,
  `docs/codex/PROJECT_STATE.md`, `docs/index.md` и `mkdocs.yml`.
- Поведение: docs показывают explicit registration и physical output only;
  package base/all-extras metadata воспроизводима, Tika status честно указан,
  import/construction side effects отсутствуют.
- Review проверяет correctness, fidelity, compatibility, dependencies, parser
  authority, limits, cancellation, malicious corpus and absence of universal
  dispatch. Финально: `git diff --check`; `make lint`; `make typecheck`;
  `make test`; `make test-security`; `make test-integration`; `make docs`;
  `make test-build`; aggregate target — `make check`.

## Передача на ручной commit и Pull Request {#m04-manual-handoff}

Срез: 2026-09-09, локальный Python 3.12.9/macOS, ветка
`feat/m04-technical-parsers`, target будущего PR — `main`.
Канонические требования: [M4][spec-m4] и [NFR-006][spec-nfr-006].
Изменения подготовлены для ручного commit и **Draft PR**; merge readiness и
полное завершение milestone не заявляются. Commit, push и PR не выполнялись;
staging area пустая.

### Фактический checklist

- [x] A–E реализованы отдельными adapters; F — отдельный выключенный по умолчанию
  HTTP client, не core dependency и не замена специализированным adapters.
- [x] AC-01–05/08–10 сверены с наблюдаемыми tests в
  [матрице приёмки](M04_acceptance_audit.md); четыре частичных AC сохранены выше.
- [x] Findings предыдущих security/final reviews исправлены и защищены regression
  tests; повторный review исправлений не выявил новых существенных findings.
- [x] Русские public docs/docstrings, копируемые примеры, ADR 0004–0007 и
  `PROJECT_STATE.md` соответствуют реализованному scope.
- [x] Локальные quality gates, docs examples, tracked/untracked whitespace и
  проверка состава diff выполнены; результаты ниже.
- [ ] Закрыть оставшиеся AC-06/07/11/12 либо отдельно согласовать их судьбу перед
  объявлением milestone принятым. Этот handoff не меняет утверждённый scope.
- [ ] Выполнить предусмотренный шагом 10 release/version gate: package остаётся
  `0.3.0`, переход на `0.4.0` не выполнен. Wire ESM `1.1.0` — отдельная версия,
  не свидетельство выпуска SDK `0.4.0`.
- [ ] Пользователю проверить итоговый набор файлов, вручную создать commit,
  выполнить push и открыть Draft PR в `main`.

### Отличия от исходной карты реализации

- Shared helpers находятся в `parsers/builtin/_common.py`, `_json.py`,
  `_markup.py`, `_documents.py`, а pure hashing — в `parsers/_hashing.py`.
  Планировавшийся каталог `parsers/_shared/` не создан; universal parser нет.
- JSON использует bounded собственный tokenizer для raw numeric lexemes и
  duplicate keys; `ijson` не добавлен. Обязательная новая dependency — только
  `charset-normalizer`; document/XML/YAML/Tika backends подключаются extras.
- Общий `assert_parser_contract` дополнен format-specific suites, без
  запланированной замены reusable case API. Docs/static checks находятся в
  `tests/docs/test_m04_examples.py`, `test_m04_acceptance.py` и существующих
  architecture/ports suites; отдельного `test_m04_architecture_contract.py` нет.
- Реальные backend tests находятся в `tests/integration/test_document_backends.py`;
  малые текстовые OOXML fixtures — в `tests/fixtures/documents/`. ZIP/PDF payloads
  создаются test builders в памяти, бинарные артефакты не добавлены.
- Caller LOG regex/extra `logs`, общий wall-clock deadline A–D, M12 sandbox,
  Windows document worker и OCR не реализованы и не описываются как готовые.
  Подробные fidelity/security ограничения сохраняются в public API и ADR.

### Фактически выполненные проверки

В этой подготовке source/tests/dependencies не менялись. Повторены проверки
текущего кода; после правок checklist отдельно пересобрана документация.

| Команда | Наблюдаемый результат |
| --- | --- |
| `make check test-integration test-security` | Exit 0: lock, Ruff format/lint (135 файлов), strict mypy (133 файла), полный pytest — `1630 passed, 5 warnings`; strict MkDocs, offline wheel/sdist/rebuild и isolated base import — успешно; integration — `11 passed`, security — `216 passed` |
| `.venv/bin/pytest -q packages/structuraguard/tests/docs` | `45 passed`; реальные LLM API и внешний Tika не вызываются |
| `make docs` после обновления checklist | Strict build успешен; первый прогон выявил две ссылки на неверный anchor, исправлено явным `m04-manual-handoff`, повтор — exit 0 |
| `git diff --check` | Exit 0 для tracked diff |
| `git ls-files --others --exclude-standard -z \| xargs -0 -n 1 sh -c 'git diff --no-index --check -- /dev/null "$1"' sh` | Нет whitespace errors в новых файлах, не охватываемых обычным `git diff` |
| `git status --short`, `git diff --stat`, `git diff --cached --stat`, `git branch --show-current` | 23 modified + 71 untracked файла M4; staged diff пуст, существующая ветка сохранена |
| `git ls-files -m -o --exclude-standard -z \| xargs -0 file` | Все 94 файла — исходники, текстовые fixtures, tests, docs или configuration/lock; неожиданных binaries/generated artifacts нет |
| `git check-ignore -v dist site .venv .pytest_cache .ruff_cache .mypy_cache` | Build/docs outputs и caches игнорируются, в commit-кандидаты не входят |
| `command -v gitleaks detect-secrets trufflehog` | Отдельные secret scanners отсутствуют; применён локальный сигнатурный `rg` и просмотр совпадений |

`make check` включает `make lint`, `make typecheck`, `make test`, `make docs` и
`make test-build`; это выполненные вложенные targets. Отдельно после docs edits
повторён только `make docs`. Пять warnings — upstream PyMuPDF SWIG deprecations;
проверки и фильтры не ослаблялись. Предыдущие узкие прогоны (`34` новых regression
cases и `598` parser cases) зафиксированы в `PROJECT_STATE.md`, не выдаются за
новые команды этой подготовки.

Сигнатурный `rg` проверил список modified/untracked files на private-key headers,
provider token shapes, credential assignments и credential-bearing URLs.
Совпадения — искусственные canaries в Tika tests и поля тестового approval;
реальных credentials не обнаружено. В production parser diff проверены
`print`/breakpoint/pdb/TODO/FIXME и закомментированный код: `print` найден только
в JSON IPC document worker и просмотрен как необходимый transport, не debug log.
Это ограниченный локальный осмотр, не сертификат отсутствия любых secrets.

### Пропущенные проверки и residual risks

| Проверка / gate | Почему не завершена |
| --- | --- |
| AC-06: measured peak RSS/CPU для каждого part/native call | Нужны отдельные воспроизводимые большие fixtures и performance runner; имеющиеся bounded tests это не заменяют |
| AC-07: все N−1/N/N+1 сочетания limits | Исчерпывающий markup/document boundary corpus не создан; эта задача подготовки не расширяет тестовую реализацию |
| AC-11: отдельный полный `all` install | Проверены locked dev parser extras и isolated base; DB/LLM bundles не устанавливались, `make sync` повторно не нужен для неизменённого проверенного окружения |
| AC-12: реальный Tika/JVM, DLP и isolation | Caller deployment отсутствует; fake server доказывает client contract, но не безопасность внешнего контейнера, TLS/DNS или DLP |
| Remote CI и другая OS/Python matrix | Выполнен только локальный macOS/Python 3.12 прогон; push/PR/remote CI не запускались |
| Полный secret/SBOM/native exploit audit | Secret scanners не установлены; supply-chain evidence предыдущего security review сохранено как датированный срез, online advisory scan не повторялся при неизменных dependencies |
| Внешний link checker | Не настроен; strict MkDocs проверяет локальные links/anchors, доступность внешних сайтов не проверяет |
| M12 sandbox / Windows worker / OCR / LOG regex / общий A–D timeout | Реализации нет; отрицательные и ограниченные tests не считаются проверкой отсутствующих capabilities |

PyMuPDF AGPL/commercial license должна быть совместима с embedding project;
native worker не равен sandbox, `strict_mode=True` отказывает. Большие/сложные
PDF fonts/layout и OOXML extensions вне fixture corpus остаются fidelity risk.
Перед merge нужны ручная оценка открытых gates и решение о release version;
автоматический bump, staging или commit этим планом не разрешаются.

### HTML compatibility CI: исправление 2026-09-10 {#m04-html-ci-fix}

Пользователь сообщил об одинаковом падении HTML error-boundary case в CI
Python 3.12/3.13/3.14 после commit `c5abec6`. Причина — новый HTML5 dispatch
CPython превращает неизвестную marked section в bogus comment; SDK раньше
полагался на вызов marked-section parser и последующий typed отказ.

`_Dom.parse_html_declaration()` явно сохраняет прежний marked-section dispatch.
Общий tokenizer не заменён, публичный API, dependencies и CI matrix не менялись.
Существующий failing case сохранён без skip/xfail; новый
`tests/unit/parsers/builtin/test_html_compatibility.py` добавляет 39 cases:
unknown names, chunk/batch boundaries, line provenance, redacted errors,
отсутствие terminal manifest при отказе, CDATA/conditional declarations,
inert lookalikes в attributes/comments/script/style и token limits.

До production fix настоящий Python 3.14.2 воспроизвёл исходное падение
(`1 failed, 2 passed`) и новые regressions (`25 failed, 14 passed`).
После fix в isolated locked environments, на macOS:

| Python | HTML/markup narrow suite | Полный pytest |
| --- | --- | --- |
| 3.12.12 | 99 passed | 1669 passed, 5 warnings |
| 3.13.11 | 99 passed | 1669 passed, 5 warnings |
| 3.14.2 | 99 passed | 1669 passed, 5 warnings |

Команды полного повторения:

```bash
uv run --isolated --locked --all-packages --group dev --no-python-downloads --python 3.12.12 pytest -q
uv run --isolated --locked --all-packages --group dev --no-python-downloads --python 3.13.11 pytest -q
uv run --isolated --locked --all-packages --group dev --no-python-downloads --python 3.14.2 pytest -q
make lock-check lint typecheck docs test-build test-integration test-security
git diff --check
```

Перед полными прогонами та же `uv run` команда вызывала pytest только для
`test_html_compatibility.py` и `test_markup.py`; первые прогоны 3.12.12/3.13.11
разрешали download интерпретатора. Рабочая `.venv` не заменялась: в ней Python
3.12.9 также даёт `99 passed`. Lock/Ruff (136 файлов)/mypy (134 файла), strict
docs, wheel/sdist/rebuild и isolated base verification прошли; отдельные
integration — `11 passed`, security — `216 passed`. Пять warnings — прежние
PyMuPDF SWIG deprecations. Первый docs build выявил неэкранированный HTML
example; после escaping пример не ломает preprocessing старого MkDocs/CPython.

Повторный parser/security review локального fix не выявил новых существенных
findings: dispatch вызывается только в markup context, не сканирует inert data,
limits и error redaction сохранены. Raw sample/provenance checks не заменяют
проверку всех изменений stdlib HTML5. Remote Linux CI ещё не повторён агентом;
commit/push/PR не выполнялись. AC-06/07/11/12 и release/version gate остаются
открытыми; исправление этого CI-defect не меняет статус полной приёмки M4.

## Out of scope

- Semantic profiling/analyzer, создание/validation/execution `ParsePlan`.
- Business entities, target field names, DB catalog/mapping/load and LLM calls.
- OCR, scanned PDF processing, image/audio/video extraction.
- General archive ingestion, remote resource loading, browser/rendered HTML.
- Sandbox runner implementation M12; M4 лишь сохраняет fail-closed boundary.
- Automatic parser/plugin registration, dependency installation or network
  resolution.

## Риски

- Public ESM expansion is wire-visible. Minor schema version/defaults сохраняют
  old payloads, но exhaustive consumers должны явно принять новые union variants;
  это документируется до merge M4.0.
- Incremental JSON backend может не сохранить numeric lexeme/duplicate keys.
  Это hard gate C, а не повод разрешить destructive/full-DOM fallback.
- Sync document libraries могут удерживать bounded part/DOM или native resources,
  а cancellation Python thread не прерывает active native call. ZIP/page/part
  caps, короткие backend calls, bounded worker queue, cooperative cleanup и peak
  measurements обязательны; hard deadline и strict/untrusted protection зависят
  от M12 sandbox.
- Exact PDF table extraction зависит от library/version и неоднозначной layout
  geometry. Golden fixtures фиксируют поддерживаемое поведение; OCR/heuristic
  promises не добавляются.
- Encoding and dialect detection вероятностны. Confidence/evidence and explicit
  ambiguity errors сохраняют воспроизводимость; MIME/extension не повышают
  недоказанный format до supported.
- Tika остаётся наивысшим supply-chain/network/provenance risk. Его отсутствие
  после core gate допустимо и не снижает готовность обязательных A–E.

[spec-m4]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m4-technical-parsers
[spec-nfr-006]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#nfr-006-streaming
