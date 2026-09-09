# ADR 0006: ограниченные read-only document adapters

Статус: принято для M4-E. Дата: 2026-09-09.

## Контекст и решение

FR-009–FR-011, NFR-006 и parser security требуют физических данных, без
семантических сущностей, выполнения формул/макросов, сетевых ресурсов и OCR.
`XlsxParser`, `PdfParser`, `DocxParser` независимы; общий код содержит только
ограниченный transport, ZIP preflight, hardened XML и сборку batches.
Ни `ParsePlan`, ни DB/LLM adapters здесь не используются.

XLSX/DOCX читаются непосредственно из OOXML в режиме ZIP `r`. Полный объект
Workbook/Document не создаётся: стандартная конверсия workbook cells теряет
исходные numeric/date lexemes и разделение `<f>`/`<v>`. `openpyxl` предоставляет
координаты и built-in format codes, `python-docx` — проверку QName. Все XML parts,
включая неиспользуемые, проходят `defusedxml` с запретом DTD/entities/external.
Ограниченный XML member материализуется; это не полностью потоковый XML parser.

PDF использует существующий extra PyMuPDF. Поддерживаются только text-layer PDF,
без rendering/OCR. Backend не исполняет actions; adapter дополнительно отклоняет
active dictionaries, вложения, XFA, зашифрованные и автоматически repaired PDF.
Поиск запрещённых PDF names консервативен: возможен отказ на literal, похожий
на запрещённое имя. Обход objects охватывает также неиспользуемые xrefs.

Probe читает только bounded PDF signature либо ZIP central directory с обязательными
OOXML part names. Это кандидаты формата, не сертификат валидности: content types,
relationships и PDF object structure проверяются в worker перед extraction.
Warning `DOCUMENT_STRUCTURE_VALIDATED_ON_PARSE` делает границу явной. При
недостаточном probe budget adapter может не подтвердить формат.

## Изоляция и жизненный цикл

Синхронная работа выполняется в одноразовом subprocess, не в потоке:
[PyMuPDF прямо исключает multithreading](https://pymupdf.readthedocs.io/en/latest/recipes-multiprocessing.html).
Источник копируется в временный bounded snapshot; путь выбирает SDK.
Worker получает только JSON через stdin, возвращает ограниченные JSON frames
через stdout. Нет shell, pickle, произвольного executable или source-provided
пути. Backend diagnostics, включая native stdout, перенаправлены в DEVNULL.
Окружение child содержит только настройки Python encoding, без secrets caller.

Parent deadline охватывает spool, worker и ожидание output. Timeout возвращает
`PROCESSING_TIMEOUT`, cancellation сохраняет `CancelledError`. При ошибке,
timeout, cancellation или `aclose()` worker уничтожается и reaped до удаления
snapshot. Deadline проверяется внутри `anext`, не отменяет сторонний код consumer
между batches. Время backpressure входит в общий deadline.

Реализация worker поддерживает Linux/macOS; остальные платформы явно получают
`PARSER_UNSUPPORTED_FEATURE`. Linux использует `RLIMIT_AS`; macOS не поддерживает
его уменьшение и использует parent RSS watchdog (`ps` каждые 100 мс). Возможен
кратковременный overshoot между samples. Недоступный watchdog — явный отказ
`SECURITY_SANDBOX_REQUIRED`. Дополнительно ограничены CPU, file size и open files.

Это **не sandbox M12**: здесь нет OS-level network namespace, syscall filtering,
read-only filesystem или защиты от native-code exploit. `strict_mode=True`
отказывает с `SECURITY_SANDBOX_REQUIRED` до чтения. Непривилегированный процесс,
adapters без fetch/execute и deny policies уменьшают поверхность атаки, но не
заменяют будущий sandbox runner. ParseContext не содержит SecurityConfig;
strict policy задаётся явно в limits адаптера.

## Physical fidelity и provenance

- XLSX: каждый лист — `ExtractedTable` с последовательными row segments;
  исходные пропуски строк/ячеек не заполняются. `SheetCellLocation` хранит точное
  имя листа и zero-based row/column; A1 — metadata. Имя листа больше не trim-ится
  валидатором: это исправление потери raw provenance, без изменения wire shape.
  Пустой `<c/>` — null, `<v/>` — пустая строка, отсутствующая cell отсутствует.
  `<v>` остаётся строкой, в том числе date serial и Boolean token. Shared string
  разрешается в text, исходный index сохранён отдельно; rich text run formatting
  и phonetic annotations не моделируются. `rPh` не добавляется в отображаемое
  значение ячейки. Excel escape sequences остаются raw OOXML text.
  Formula source — отдельный metadata block со ссылкой на cell; cached value
  хранится в cell и никогда не вычисляется. Shared/array formula attributes
  сохраняются без expansion. Merged ranges — отдельные metadata blocks;
  covered cells не заполняются anchor value. Именованные Excel tables представлены
  definitions (`table_name`, `table_range`, `column_name_candidate`) поверх
  worksheet cells, без второго дублирующего набора ячеек.
- PDF: page metadata, ordered text blocks/lines, one-based page и block identity;
  bbox нормализован относительно crop box, исходные point coordinates lines/blocks
  сохранены в metadata. Порядок — content stream backend, не reading-order AI.
  Text spans соединяются только в пределах физической строки. Геометрические
  tables/cells — технические кандидаты; cell provenance содержит page/bbox и
  table/row/column metadata. Точность layout зависит от PDF/backend; font/glyph
  data, image contents и byte-level PDF operators не входят в модель.
  Пустые страницы сохраняются, документ без непустого текста получает
  `PARSER_NO_TEXT_LAYER`, даже если есть изображения. Native decompression и
  extraction могут выделять память до возврата: действуют process caps/deadline.
- DOCX: `ExtensionLocation` хранит package part, zero-based body block index и
  positional path. `block.order` и table `block_index` восстанавливают interleaving
  blocks/tables, которые в DTO находятся в разных массивах. Для run — paragraph
  path, descendant run ordinal и child ordinal; byte/line offsets не выдумываются.
  Cell paragraphs и вложенные tables имеют собственные paths; cell text —
  дополнительная проекция, hierarchy не заменяется flatten. `gridSpan`/`vMerge`
  остаются raw metadata. Headings/list metadata определяются из прямого pStyle/numPr;
  inherited styles, list numbering layout, headers/footers/footnotes, drawings,
  pagination и revisions layout не интерпретируются. Core properties выдаются
  отдельно. Field instructions сохраняются как inert values, cached display
  text — отдельно. Неподдерживаемые body containers/altChunk дают typed отказ.

OOXML XML infoset включает стандартное entity/newline decoding. Raw strings
нельзя вставлять в HTML без escaping: безопасная сериализация DTO не равна
санитизации пользовательского интерфейса.

## Пределы и ошибки

Общие defaults: source 32 MiB, deadline 30 s, worker 768 MiB, ZIP 4096 members,
central directory 1 MiB, member 8 MiB, сумма unpacked 64 MiB, ratio 200,
relationships 4096, XML nodes 1 млн. EOCD проверяется до создания `ZipFile`.
ZIP64/multi-volume/encryption/нестандартная compression не поддерживаются;
проверяются declared и измеренные размеры, CRC, duplicate names, traversal,
symlink/special entries, macro/embedded executable/nested containers. ZIP не
извлекается на диск. Все external relationships отклоняются, включая hyperlinks.

`XlsxParserLimits`: sheets/rows-per-sheet/columns/cells/shared-strings/merged-ranges.
`PdfParserLimits`: pages/objects/blocks-per-page/tables-per-page/cells-per-page.
`DocxParserLimits`: blocks/runs-per-paragraph/table-cells. Все наследуют конечные
depth/node/value/text/unit/batch caps; дополнительно действуют `ParseContext`
bytes/records/physical-objects/max-batches. XML node counter включает повторные
проходы проверенных parts, а не только уникальные source nodes.

XLSX `batch_size` — максимум полных rows сегмента; PDF — страниц, DOCX — body
blocks. Служебные metadata/empty sheet не увеличивают record count. Неразрезаемый
unit, превышающий caps, отклоняется, а не урезается. Builder хранит bounded batch
и один lookahead unit; stdout ограничен 16 MiB на frame.

Malformed → `PARSER_MALFORMED_INPUT`; отсутствующий extra →
`PARSER_DEPENDENCY_UNAVAILABLE`; неподдерживаемая возможность →
`PARSER_UNSUPPORTED_FEATURE`; запрещённая capability → `SECURITY_INPUT_REJECTED`;
лимит → `SECURITY_LIMIT_EXCEEDED`, с allowlisted `resource` и bounded integer
`limit` в transport. Уточнение acceptance audit M4: native worker crash,
повреждённый frame и неожиданные RuntimeError/KeyError/IndexError/OverflowError
дают `PARSER_OUTPUT_INVALID`, а не malformed input. Известный PDF `FileDataError`
остаётся `PARSER_MALFORMED_INPUT`. Source snippets, filenames и secrets не попадают
в worker error payload. Registry сохраняет `PARSER_NO_TEXT_LAYER`/`PROCESSING_TIMEOUT`.

## Зависимости и проверки

Production deps остаются optional: `excel` = openpyxl + defusedxml,
`office` = python-docx + defusedxml, `pdf` = PyMuPDF. Imports/factories не загружают
backends. Openpyxl/python-docx — MIT, defusedxml — PSF; используются существующие
поддерживаемые bundles, lock фиксирует проверенные версии. PyMuPDF имеет
[AGPL/commercial licensing](https://pymupdf.readthedocs.io/en/latest/about.html):
владелец embedding-приложения обязан проверить лицензионную совместимость.
Browser engine, OCR и новые unconditional production dependencies не добавлены.

Проверки: общий Parser contract через registry, реальные openpyxl/python-docx
packages, детерминированные text/table PDF fixtures, Unicode/batch properties,
ZIP/XXE/action corpus, лимиты, no-text outcome, cancellation/timeout/cleanup.
`make test-integration` запускает real-library tests, `make test-security` — весь
security corpus; оба набора также входят в `make test`.
