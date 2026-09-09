# ADR 0005. Безопасные XML, HTML и YAML technical adapters

Статус: принято для M4-D. Дата: 2026-09-09.

## Решение

`XmlParser`, `HtmlParser` и `YamlParser` независимы. Общими являются только
bounded I/O, counters и manifest/batch builder. Никаких DB/LLM/semantic операций,
окончательного `ParsePlan`, target names, destructive flatten и browser engine.

По текущему требованию пользователя XML, как и YAML, становится optional:
`structuraguard[xml]` содержит `defusedxml>=0.7.1,<1`,
`structuraguard[yaml]` — `PyYAML>=6.0.3,<7`. Старый пункт плана о direct XML
dependency заменён этим решением. HTML использует стандартный `html.parser`.
Extras импортируются только при XML/YAML probe/parse; отсутствующий backend
даёт `PARSER_DEPENDENCY_UNAVAILABLE`. Core import и factory не загружают extras.

XML использует incremental `DefusedXMLParser` с одновременно включёнными
`forbid_dtd`, `forbid_entities`, `forbid_external`. Resolver отсутствует. DTD,
включая безвредный, отклоняется. `SYSTEM`, external entities, parameter entities
и Billion Laughs не становятся поводом читать файл или сеть. Помимо counters
events ограничен незаконченный backend token: Expat может задерживать callbacks.
`CurrentByteIndex` ограничивает незавершённый input buffer независимо от содержимого
comment/CDATA; запас составляет `4 * max_token_chars + read_chunk_bytes` байт.
Это conservative limit с учётом multibyte encoding и reparse deferral.

HTML сохраняет inert source-event DOM, не имитирует browser HTML5 tree repair.
`script`, `style`, `iframe`, `object`, `embed`, `template`, `svg`, `math` и их
descendants имеют `active_content_policy=denied`. Из них не строятся projections.
URL, event handlers, CSS и JS остаются raw data; никакие resources не загружаются.
`html_safe_json()` дополнительно экранирует HTML delimiters и Unicode line
separators при embedding JSON. Ни эта функция, ни parser не делают raw values
безопасными для `innerHTML`: consumer обязан использовать text output/escaping.

YAML использует только events экземпляра `SafeLoader`. Constructors, composer,
implicit type conversion и merge-key expansion не вызываются. Scalars остаются
строками; numbers, dates, null, bool не получают бизнес-тип. Python/custom tags
отклоняются; стандартные safe tags сохраняются как metadata. Alias — отдельный
leaf с target ID, не копия дерева; self-reference разрешена только как inert
ссылка, не как parent cycle. Alias/anchor counters ограничены на document.

## Физическая модель и provenance

Расширения schema `1.1.0` additive: `PhysicalNodeKind` включает markup kinds,
`ExtractedTreeNode` — optional `raw_lexeme` и bounded metadata;
`CssSelectorLocation` — optional `node_index`, `line_number`, `column_number`.
`XPathLocation.namespace_prefix` адресует namespace declaration на element,
выбранном XPath; пустой prefix означает default namespace. Это позволяет
адресовать и `xmlns=""`, для которого namespace axis XPath не содержит узла.
Новые пустые поля не сериализуются, schema `1.0.0` по-прежнему их запрещает.
Continuation root может быть также XML element или HTML document.

- XML: expanded `{URI}local` name, явные namespace declarations, attributes,
  elements, text/tail, comments, PI, повторяющиеся и пустые элементы. Абсолютный
  XPath использует `local-name()`/`namespace-uri()` и sibling occurrence вместо
  внешнего prefix map. Namespace declarations сохраняют prefix, если backend его
  отдаёт. XPath attributes/text/comments/PI однозначен в source infoset.
  XML-предписанные entity decoding, attribute whitespace и newline normalization
  выполняет backend; original lexical quotes, CDATA boundary, byte/line spans и
  выбор `<x/>` против `<x></x>` не обещаются. Это не бизнес-нормализация.
- HTML: `:scope > *:nth-child(N)` адресует element в source-event DOM;
  `node_index` различает его физические attributes/text/comment children.
  Line one-based, column zero-based; это начало события. Для attributes это
  начало enclosing start tag плюс occurrence, а не выдуманный offset атрибута.
  Original start-tag lexeme сохраняет регистр, duplicate attrs и quoting.
  Browser-вставленные `html/body/tbody`, implied closes, end/byte spans не обещаются.
  Несопоставленный end tag игнорируется, совпавший закрывает stack до matching tag;
  незакрытые элементы завершаются на EOF. Такой DOM не следует использовать для
  browser CSS queries без сопоставления исходной структуры.
- YAML: `ExtensionLocation(namespace="yaml:mark")` хранит zero-based document
  index и positional path, one-based start/end lines, zero-based end-exclusive
  columns. Mapping children идут key/value парами (`mapping_role`, `pair_index`),
  поэтому duplicate/empty/complex keys не теряются. Scalar `raw_lexeme` — точный
  slice source document; raw value — event value. Quotes/block folding отражены
  event style и lexeme. Collection marks уточняются на closing event; comments,
  lexical indentation trivia и byte offsets библиотека не предоставляет.

## Batching и ограничения

XML освобождает complete direct-child subtrees корня; attributes, namespace
declarations и mixed-content leaves корня тоже являются units. Корень повторяется
только как continuation envelope. HTML выдаёт complete top-level subtrees
synthetic document; целый `<html>…</html>` остаётся одним bounded subtree.
YAML читает UTF-8 поток по CR/LF/CRLF lines и обрабатывает complete documents;
`max_document_chars` применяется до SafeLoader. Один document не режется.

Oversized неделимый subtree/document отклоняется, а не flatten-ится.
`batch_size` — число units, `record_count` — их количество; YAML unit — document.
Empty XML/HTML root не добавляет record. Builder держит ограниченный pending batch
и один lookahead unit; terminal batch не расходует лишний batch slot. Все
физические projections и repeated envelopes учитываются в `max_physical_objects`.
Cancellation проверяется при чтении, между units, YAML events и перед terminal
batch; закрывается исходный generator и освобождается SafeLoader.

HTML/YAML принимают strict UTF-8, optional BOM. Policy видна в probe warning,
malformed bytes дают typed error без replacement fallback. XML следует encoding
declaration/BOM hardened backend. Read/probe/depth/nodes/value/text/token/subtree/
batch limits имеют finite hard caps; XML добавляет attributes/namespaces, HTML —
attributes, YAML — document/anchor/alias limits. Общие bytes/records/batches/depth/
physical limits берутся также из `ParseContext`.

## Зависимости и риски

`defusedxml` имеет PSF license; последний используемый release 0.7.1 выпущен в
2021 году. Зависимость от поведения Expat проверяется regression tests; обновления
Python/Expat требуют повторного прогона. PyYAML 6.0.3 имеет MIT license; используется
pure Python `SafeLoader`, не unsafe/C loader. Версии/хеши development installation
закреплены в `uv.lock`; эти библиотеки не расширяют core runtime closure.
Upstream: [defusedxml](https://github.com/tiran/defusedxml),
[PyYAML](https://pyyaml.org/wiki/PyYAMLDocumentation).

In-process cooperative limits не являются process sandbox или принудительным
CPU deadline; изоляция недоверенных plugins остаётся задачей M12.

Связано: [ADR 0004](0004-lossless-physical-extraction.md),
[план M4](../plans/M04_technical_parsers.md), [API](../public-api.md).
