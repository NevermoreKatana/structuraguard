# Публичный API StructuraGuard

Статус: подтверждённое поведение package `structuraguard` версии `0.3.0` в
текущей реализации M4, включая parser groups A–C. Facade-операции pipeline в эту
поставку не входят.

Целевой API следующих milestone описан в каноническом разделе
[23. Публичный API SDK][spec-public-api]. Он не считается доступным, пока
соответствующий vertical slice не имеет реализации и тестового evidence.

## Копируемый пример M1 {#m1-copyable-example}

Основной facade асинхронный. В M1 его можно создать с явной конфигурацией, но
предметные операции всегда завершаются типизированной ошибкой. Пример не читает
источник и не возвращает фиктивный результат.

<!-- example:m01-async:start -->
```python
import asyncio

from structuraguard import (
    AsyncStructuraGuard,
    OperationNotImplementedError,
    SDKConfig,
)


async def main() -> None:
    sdk = AsyncStructuraGuard(config=SDKConfig())
    try:
        await sdk.inspect_source()
    except OperationNotImplementedError as error:
        print(error.error_code)


asyncio.run(main())
```
<!-- example:m01-async:end -->

Ожидаемый вывод:

```text
SDK_OPERATION_NOT_IMPLEMENTED
```

`asyncio.run()` вызывается приложением явно. Сам `import structuraguard` и
создание facade event loop не создают.

## Экспорты M1

Top-level module `structuraguard` экспортирует только:

- `SDKConfig`;
- основной `AsyncStructuraGuard` и отдельный `StructuraGuard`;
- `StructuraGuardError`, `SourceError`, `ParserError`,
  `DatabaseInspectionError`, `MappingError`, `ValidationError`,
  `SecurityPolicyError`, `LoadError` и `OperationNotImplementedError`.

Экспорты разрешаются лениво. Обычный `import structuraguard` не загружает
Pydantic; он загружается при первом явном обращении к `SDKConfig` или facade.

## Contracts и ports M2

Корневой `structuraguard.__all__` сохраняет ровно exports M1. Новая поверхность
доступна через два явных subpackage:

- `structuraguard.contracts` — frozen DTO, tagged scalars, physical
  `ExtractedBatch`, закрытый discriminated `ParsePlan`, semantic
  `NormalizedBatch`, `DatabaseCatalog`, `MappingPlan`, reports и checked-plan
  evidence;
- `structuraguard.ports` — `Parser`, `SemanticStructureAnalyzer`,
  `ParsePlanValidator`, `ParsePlanExecutor`, `DatabaseAdapter`, `LLMProvider`,
  `SecurityScanner`, `StagingStore` и `AuditStore`.

`SourceLocation`, `RawScalar`, `NormalizedScalar`, `ParseRule`, `ParsePlan` и
`StructureAnalysisResult` являются закрытыми discriminated unions, а не
конструкторами. Для validation отдельного union payload используется
`pydantic.TypeAdapter`.

### Копируемый contract-only пример {#m2-contract-copyable-example}

M2 позволяет создать физический DTO и проверить его JSON round-trip. Этот
пример не читает и не разбирает файл, не вызывает parser и не запускает ingest.

<!-- example:m02-contract:start -->
```python
from structuraguard.contracts import (
    ExtractedValue,
    LineRangeLocation,
    SourceArtifact,
    StringScalar,
)

source = SourceArtifact(
    artifact_id="source-1",
    display_name="input.txt",
    media_type="text/plain",
    size_bytes=2,
    source_fingerprint="a" * 64,
)
value = ExtractedValue(
    raw_value=StringScalar(value="42"),
    location=LineRangeLocation(
        source=source.ref,
        line_start=1,
        line_end=1,
    ),
    technical_type_hint="text",
)

wire = value.canonical_json()
restored = ExtractedValue.model_validate_json(wire)
assert restored == value
print(restored.location.source.source_fingerprint)
```
<!-- example:m02-contract:end -->

Ожидаемый вывод:

```text
sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

Fingerprint в примере передаёт caller; создание DTO не читает источник и не
проверяет соответствие digest его содержимому.

`Parser` возвращает только raw physical structure. Semantic types появляются
только после применения `ValidatedParsePlan`. `DatabaseAdapter` исполняет только
`ValidatedMappingPlan`, связанный с `target_id`, database fingerprint и policy
fingerprint; SQL не является частью публичного plan contract.

Каждый batch в manifest представлен `ExtractedBatchSummary` или
`NormalizedBatchSummary`: `validate_batch()` сверяет lineage и cardinality одного
batch, а `validate_batches()` — порядок, terminal marker, counts и глобальную
уникальность normalized IDs. `ExtractedSourceIndex` — selective allowlist не
более 10 000 physical references для sample/evidence/selectors, а не копия всех
объектов большого source.

Все public DTO отклоняют extra fields, naive datetime, non-finite numbers и
несогласованную локальную lineage. Collections хранятся как tuples. Для
проверки cross-artifact references validator requests получают manifests и
индексы. `model_dump_json()` поддерживает lossless tagged scalar round-trip, а
метод public DTO `canonical_json()` задаёт fingerprint representation.
Fingerprint хранится в единственной wire-форме `sha256:<64 lowercase hex>`;
bare digest на входе нормализуется до неё. `ParsePlan` и `MappingPlan` вычисляют
собственный canonical fingerprint, если caller его не передал, и отклоняют
несовпадающее явно переданное значение. Persisted aggregates и reports несут
`schema_version` и versioned `ProducerMetadata`.

`structuraguard.domain` остаётся внутренним implementation subpackage и не
входит в обещанную compatibility surface; пользовательский код должен вызывать
методы DTO из `structuraguard.contracts`.

LLM boundary принимает только canonical bounded JSON и обязательный typed
`SecurityApproval`. Он включает разрешающий `SecurityReport` и его canonical
fingerprint; `LLMRequest` повторно сверяет payload, classification, routing и
redaction fingerprints. Зарезервированные tools, credentials, handles, shell и
SQL keys после нормализации punctuation/camelCase, а также известные token/DSN/
credential canaries отклоняются до provider adapter. `SecurityReport` связан с
request/content/payload/policy, требует `scanned_items > 0` для `allowed` и не
допускает error/critical issues в успешном outcome. `AuditEvent` принимает
только `event-<UUID|ULID>`, а `run_id` — только `run-<UUID|ULID>`.
Pre-security event запрещает security
evidence; terminal success/`REJECTED_SECURITY` встраивает соответствующий
allowed/blocked `SecurityReport` и сверяет его run, redaction и canonical hash.
Audit event не может предшествовать встроенному security report.
Persisted `ValidationIssue` хранит только стабильные `code`/`message_key`, без
свободного human text. Physical `source_refs` разрешены только в contracts,
которые могут сверить их с manifest/profile allowlist; stage reports и mapping
DTO требуют пустой `source_refs`. Успешные validation/load reports учитывают каждую
запись; dry-run отдельно фиксирует `would_load_records`. `LoadReport` явно
связывает target и полную цепочку source →
mapping-validation fingerprints.

M2 фиксирует contracts. Concrete TXT/LOG/Markdown, CSV/TSV и JSON-family parsers
описаны ниже; analyzers, validators/executors, DB reflection/load, LLM providers,
staging и audit backends по-прежнему не входят в этот срез.

`ValidatedParsePlan` и `ValidatedMappingPlan` сохраняют evidence успешной
проверки, но не являются неподделываемыми полномочиями. Будущий executor или DB
adapter обязан повторно сверить fingerprints, target и policy с execution
context.

`Extracted*` и `Normalized*` могут содержать недоверенные и sensitive raw
values. `canonical_json()` и `model_dump_json()` сохраняют эти значения и не
выполняют redaction или security scan: их результат нельзя безусловно отправлять
в logs, audit или LLM. `SecurityScanner` в M2 является только protocol;
конкретного scanner нет. Проверка DTO отклоняет invalid state через
`pydantic.ValidationError`. Отклонённый input и динамические segments error
location редактируются, поэтому exception можно диагностировать по стабильному
полю модели без повторного вывода недоверенного значения. Operational exception
taxonomy методов ports в M2 не зафиксирована до появления concrete adapters.

Нормативный scope contracts задан каноническим разделом [M2 ТЗ][spec-m2]. Эта
страница описывает только подтверждённую публичную поверхность и не повторяет
полный текст требований.

## Parser registry M3

`structuraguard.parsers` предоставляет instance-local `ParserRegistry`.
Глобального mutable registry и decorator auto-registration нет. Facade создаёт
отдельный пустой registry по умолчанию либо сохраняет явно переданный
`parser_registry` по identity.

### Manual registration и selection {#m3-registry-copyable-example}

Следующий test double распознаёт только искусственную сигнатуру `DEMO`. Он нужен
для показа registration и selection, не является готовым format parser и не
вызывает `parse()`:

<!-- example:m03-registry:start -->
```python
import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

from structuraguard import AsyncStructuraGuard
from structuraguard.contracts import (
    ExtractedBatch,
    ProbeResult,
    ProbeSignal,
    ProbeSignalKind,
    ProbeSignalOutcome,
    SourceArtifact,
)
from structuraguard.parsers import ParserRegistry
from structuraguard.ports.source import ParseContext, ProbeContext


class MemoryReader:
    def __init__(self, content: bytes, source_fingerprint: str) -> None:
        self._content = content
        self.source_fingerprint = source_fingerprint

    async def read(self, *, offset: int, size: int) -> bytes:
        return self._content[offset : offset + size]


class DemoParser:
    adapter_id = "demo.parser"
    version = "1.0.0"
    priority = 10

    async def probe(
        self,
        source: SourceArtifact,
        context: ProbeContext,
    ) -> ProbeResult:
        prefix = await context.reader.read(
            offset=0,
            size=min(4, context.max_probe_bytes),
        )
        supported = prefix == b"DEMO"
        return ProbeResult(
            source=source.ref,
            adapter_id=self.adapter_id,
            adapter_version=self.version,
            supported=supported,
            confidence=Decimal("1") if supported else Decimal("0"),
            detected_media_type="application/x-demo" if supported else None,
            format_id="demo" if supported else None,
            signals=(
                ProbeSignal(
                    kind=ProbeSignalKind.SIGNATURE,
                    outcome=ProbeSignalOutcome.MATCH,
                ),
            )
            if supported
            else (),
        )

    def parse(
        self,
        source: SourceArtifact,
        context: ParseContext,
    ) -> AsyncIterator[ExtractedBatch]:
        del source, context
        raise NotImplementedError("Пример показывает только selection")


async def main() -> None:
    fingerprint = "sha256:" + "a" * 64
    source = SourceArtifact(
        artifact_id="source-1",
        display_name="input.demo",
        media_type="application/x-demo",
        size_bytes=4,
        source_fingerprint=fingerprint,
    )
    probe_context = ProbeContext(
        reader=MemoryReader(b"DEMO", fingerprint),
        source_fingerprint=fingerprint,
        max_probe_bytes=4,
    )

    registry = ParserRegistry()
    registry.register(DemoParser())
    sdk = AsyncStructuraGuard(parser_registry=registry)

    async with sdk.parsers.session() as session:
        selected = await session.select(source, probe_context)
        print(selected.adapter_id)


asyncio.run(main())
```
<!-- example:m03-registry:end -->

Ожидаемый вывод:

```text
demo.parser
```

`register()` принимает уже импортированный и созданный trusted parser object.
Composition owner тем самым явно принимает риск выполнения его in-process кода.
Canonical `adapter_id` един для manual registrations и plugin descriptors;
повторное имя либо повторная registration того же object отклоняется typed
ошибкой без замены существующего parser. Разные экземпляры одного configurable
класса разрешены только с разными canonical IDs; это не считается class-level
singleton policy. Bulk registration атомарна и не зависит от порядка входа.

Selection выполняется в async `registry.session()` по immutable snapshot.
Registry вызывает `probe` последовательно в canonical `adapter_id` order и
сначала сравнивает подтверждённые содержимым signals: internal structure,
signature и content-derived MIME. Среди parsers одного подтверждённого
`format_id` выбор продолжает `ProbeResult.confidence`, parser `priority` и
лексикографический `adapter_id`. Declared MIME и display-name extension доступны
probe через `SourceArtifact` вместе с bounded reader из `ProbeContext` и дают
typed mismatch warnings, но не могут самостоятельно подтвердить формат.
Равносильные strong signals разных `format_id` возвращают
`PARSER_FORMAT_CONFLICT`.

Начало выхода из session сразу запрещает новые `select()` и `parse()`, затем
ждёт выполняющийся probe и закрывает все созданные streams. Cancellation body
распространяется только после cleanup. Если собственный `aclose()` trusted
parser завершился ошибкой, stream остаётся quarantined и retryable; ошибка
видима caller, но registry lease освобождается. Output после начала closing не
выдаётся. Одиночные body exception, cancellation и cleanup error сохраняют свой
тип. Если одновременно произошли несколько outcomes, выход возбуждает
`BaseExceptionGroup`: исходный body exception/cancellation и каждая
санитизированная `ParserError` доступны как отдельные элементы группы.

Выбранный parser сохраняет исходный port contract:
`parse(SourceArtifact, ParseContext) -> AsyncIterator[ExtractedBatch]`.
Registry проверяет физический тип, lineage и terminal manifest. Ни
`NormalizedBatch`, ни business entities, `ParsePlan` или `MappingPlan` не
являются допустимым parser output. Parser не получает LLM/DB authority, не
определяет таблицу назначения и не генерирует SQL.

Если custom iterator trusted parser владеет внешним ресурсом, adapter должен
предоставить корректный `aclose()`. Базовый `AsyncIterator` не гарантирует этот
hook: при его отсутствии registry переводит wrapper в quarantined state и
освобождает lease, но не может принудительно закрыть ресурс adapter.

### Descriptor-only discovery {#m3-discovery-copyable-example}

`discover_plugins()` запускается только явным вызовом с
`ParserDiscoveryPolicy`, содержащей непустой allowlist distributions. Discovery
просматривает только exact entry-point group `structuraguard.parsers`, проверяет
bounded недоверенные metadata и возвращает декларативные
`ParserPluginDescriptor`. Оно не вызывает `EntryPoint.load()`, не импортирует
target module и не конструирует plugin. Ошибка отдельной distribution отражается
в `ParserPluginDiscoveryFailure` внутри `ParserPluginDiscoveryReport` и не
скрывается, но не мешает обработать остальные allowlisted distributions.
Валидные descriptors регистрируются в canonical order под per-instance lock;
каждый отклонённый duplicate остаётся отдельным наблюдаемым failure.

Если distribution `sample-plugin` установлен и объявляет
`parser.sample = vendor.sample:SampleParser`, metadata можно проверить так:

<!-- example:m03-discovery:start -->
```python
from structuraguard.contracts import ParserDiscoveryPolicy
from structuraguard.parsers import ParserRegistry

registry = ParserRegistry()
report = registry.discover_plugins(
    ParserDiscoveryPolicy(allowed_distributions=("sample-plugin",))
)
assert report.failures == ()
assert registry.snapshot().plugins == report.descriptors

descriptor = report.descriptors[0]
print(
    descriptor.adapter_id,
    descriptor.distribution_name,
    descriptor.module,
    descriptor.attribute,
)
```
<!-- example:m03-discovery:end -->

Ожидаемый вывод:

```text
parser.sample sample-plugin vendor.sample SampleParser
```

Пример читает только metadata и не вызывает `activate_plugin()`. Даже после
успешного discovery target `vendor.sample:SampleParser` не импортируется и не
конструируется.

В M3 поддерживается только native-filesystem `PathDistribution`: metadata header
читается максимум до 64 KiB, `entry_points.txt` — до 256 KiB, а symlink,
необычный тип файла, ZIP/custom provider или отсутствие безопасного `dir_fd`
дают typed per-distribution failure. Raw path и содержимое файла в отчёт не
попадают.

`ParserDiscoveryPolicy` и `ParserPluginDescriptor` экспортируются из
`structuraguard.contracts`; discovery report/failure и функция низкого уровня
`discover_parser_plugins()` — из `structuraguard.parsers`.

Descriptor в M3 не исполняется: до появления sandbox runner его activation
через `registry.activate_plugin(adapter_id)` завершается
`SECURITY_SANDBOX_REQUIRED` без in-process fallback. Неизвестный ID даёт
`PARSER_NOT_FOUND`. В самом M3 format parsers, semantic analyzer и orchestrator
отсутствовали; M4 adapters ниже не меняют plugin activation policy.

## Technical parsers M4 {#m4-technical-parsers}

### Копируемый пример физического извлечения {#m4-extraction-copyable-example}

Требуется установленный `structuraguard` без extras. Пример читает две строки
из небольшого неизменяемого snapshot, явно регистрирует TXT adapter и проверяет
весь результат. Сеть, LLM и БД не используются.

<!-- example:m04-extraction:start -->
```python
import asyncio
import hashlib

from structuraguard.contracts import SourceArtifact
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import PlainTextParser, TextParserLimits
from structuraguard.ports.source import BatchOptions, ParseContext, ProbeContext


class MemoryReader:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.source_fingerprint = "sha256:" + hashlib.sha256(content).hexdigest()

    async def read(self, *, offset: int, size: int) -> bytes:
        return self._content[offset : offset + size]


async def main() -> None:
    content = "Первая строка\nВторая строка\n".encode("utf-8")
    reader = MemoryReader(content)
    source = SourceArtifact(
        artifact_id="demo-text",
        display_name="demo.txt",
        media_type="text/plain",
        size_bytes=len(content),
        source_fingerprint=reader.source_fingerprint,
    )
    registry = ParserRegistry()
    registry.register(PlainTextParser(limits=TextParserLimits(max_line_chars=100)))
    context = ParseContext(
        reader=reader,
        source_fingerprint=reader.source_fingerprint,
        max_bytes=1024,
        max_records=10,
        max_nesting_depth=8,
        batch_options=BatchOptions(batch_size=1, max_batches=10),
    )
    async with registry.session() as session:
        selected = await session.select(
            source,
            ProbeContext(
                reader=reader,
                source_fingerprint=reader.source_fingerprint,
                max_probe_bytes=1024,
            ),
        )
        batches = [batch async for batch in selected.parse(source, context)]
        manifest = batches[-1].manifest
        assert manifest is not None
        manifest.validate_batches(batches)
        lines = [line for batch in batches for line in batch.lines]
        assert [line.text for line in lines] == ["Первая строка", "Вторая строка"]
        assert [line.location.line_start for line in lines] == [1, 2]
        print(selected.adapter_id)
        print(f"{len(batches)} batches, {len(lines)} lines")


asyncio.run(main())
```
<!-- example:m04-extraction:end -->

Ожидаемый вывод:

```text
builtin.text
2 batches, 2 lines
```

`MemoryReader` и сбор всего результата в list подходят только для этого малого
примера. Для большого источника caller предоставляет `SourceReader` над
неизменяемым snapshot и обрабатывает batches по одному. До EOF и успешных
проверок terminal manifest ранее полученные batches остаются предварительными:
ошибка или cancellation не разрешают считать извлечение завершённым. Сохранённую
последовательность можно повторно читать по одному batch в `validate_batches()`;
этот метод не требует list и не удерживает весь raw output. M4 не предоставляет
готовый staging backend или ingest executor.

`probe(source, context)` возвращает bounded `ProbeResult`, а не гарантию валидности
всего файла. `parse(source, context)` возвращает async iterator физических
`ExtractedBatch`; ошибки чтения возникают при итерации. Контексты и limits
конечны, неправильная конфигурация dataclass отклоняется `ValueError`.
При досрочном выходе закрывайте registry session: она закрывает свои streams.
`asyncio.CancelledError` не преобразуется в успешный пустой результат.

| Исключение | Machine-readable code и смысл |
| --- | --- |
| `ParserError` | `PARSER_UNSUPPORTED_FORMAT` — selection не нашёл adapter; `PARSER_UNSUPPORTED_FEATURE` — возможность или dialect не поддержаны |
| `ParserError` | `PARSER_MALFORMED_INPUT`, `PARSER_ENCODING_UNSUPPORTED` — некорректный формат или кодировка; координаты зависят от формата, raw fragments не возвращаются |
| `ParserError` | `PARSER_DEPENDENCY_UNAVAILABLE`, `PARSER_NO_TEXT_LAYER`, `PARSER_TIKA_UNAVAILABLE` — отсутствует extra, текстовый слой или доступный endpoint |
| `ParserError` | `PROCESSING_TIMEOUT` — истёк deadline document/Tika adapter; `PARSER_OUTPUT_INVALID` — нарушен output contract или аварийно завершился worker |
| `SecurityPolicyError` | `SECURITY_LIMIT_EXCEEDED`, `SECURITY_INPUT_REJECTED`, `SECURITY_SANDBOX_REQUIRED` — превышен бюджет, запрещён ввод или недоступна требуемая изоляция |

Raw values могут содержать PII, инструкции и вредоносную разметку. Парсеры не
выполняют их, не вызывают LLM/БД и не создают окончательный `ParsePlan`. Извлечение
не является DLP-проверкой или HTML sanitization: не логируйте raw batches и не
выводите raw поля через `innerHTML`. Общий wall-clock deadline для всех A–D пока
не реализован; наличие finite limits и cancellation не означает hard timeout.

Это реализованный scope, а не полная приёмка [M4 ТЗ][spec-m4].
[Аудит M4](plans/M04_acceptance_audit.md) оставляет частично подтверждёнными
AC-06/07/11/12: измерения peak resources, исчерпывающие N±1 boundaries,
отдельное all-extras окружение и реальный Tika deployment. Требование streaming
определено в [NFR-006][spec-nfr-006]; форматные ограничения приведены ниже.

## Built-in text parsers M4-A

TXT, LOG и Markdown adapters регистрируются явно. Импорт
`structuraguard.parsers` не создаёт их и не меняет registry:

```python
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import builtin_text_parsers

registry = ParserRegistry()
registry.register_many(builtin_text_parsers())
```

Factory каждый раз возвращает новые `PlainTextParser`, `LogParser` и
`MarkdownParser` с IDs `builtin.text`, `builtin.log` и `builtin.markdown`.
Пределы задаются immutable `TextParserLimits`, `LogParserLimits` и
`MarkdownParserLimits`; размер batch — через `ParseContext.batch_options`.
Hard caps: `batch_size <= 1_000_000`, `max_batches <= 10_000` и
`max_physical_objects <= 10_000_000`.
После selection runtime переносит проверенный `detected_encoding` в immutable
`ParseContext`, поэтому parse не зависит от размера short read и не хранит
решение в состоянии adapter.

Probe сначала учитывает BOM и strict UTF-8, затем MAY применить bounded
`charset-normalizer`. Результат содержит `detected_encoding`, confidence и
warnings `PARSER_ENCODING_HEURISTIC`/`PARSER_ENCODING_LOW_CONFIDENCE`.
Malformed input не декодируется с replacement characters и даёт typed
`PARSER_ENCODING_UNSUPPORTED` либо `PARSER_MALFORMED_INPUT`.
Stateful escape codecs (`ISO-2022`, `HZ`) отклоняются как unsupported: их
zero-output shift sequences сделали бы terminal batching зависимым от границ
чтения.

`PlainTextParser` возвращает ordered physical lines. `MarkdownParser` добавляет
heading, paragraph, list, blank и inert fenced-code blocks. `LogParser`
сохраняет lines и event blocks; только фиксированные ISO timestamp/level,
Apache combined и bounded key-value recognizers создают raw technical hints.
Одна похожая или неизвестная строка не подтверждает LOG, а JSON-per-line не
перехватывается у JSONL adapter. Ни один adapter не выполняет code,
HTML, links или embedded commands и не формирует `ParsePlan`.

Все три parser используют line-based provenance. `line_start`/`line_end`
нумеруются с единицы, capture columns — zero-based и end-exclusive, а исходный
line terminator хранится в bounded metadata. Новые adapters создают physical
wire schema `1.1.0`; совместимость описана в
[ADR 0004](adr/0004-lossless-physical-extraction.md).
Terminal manifest включает producer identity, fingerprint effective parser
limits/batching и independently verifiable canonical batch/run fingerprints.
Выбранный `ParseContext.detected_encoding` также входит в fingerprint настроек
группы A: разные декодирования одного snapshot не разделяют `extraction_id`.
После исправления M4 identities пересчитываются; не смешивайте batches,
полученные до и после этой правки незавершённого milestone.
Group A adapters помещают в `indexed_refs` deterministic prefix реальных
line/block/value references с общим cap 10 000; terminal `source_index` точно
равен их progressive последовательности и пригоден для bounded physical sample.

## Built-in delimited parser M4-B

CSV и TSV обслуживает один узкий format-family adapter
`DelimitedTextParser`. Он не является универсальным parser и регистрируется
отдельно от Group A:

```python
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import (
    builtin_delimited_parsers,
    builtin_text_parsers,
)

registry = ParserRegistry()
registry.register_many((*builtin_delimited_parsers(), *builtin_text_parsers()))
```

`builtin_delimited_parsers()` каждый раз возвращает новый instance-local
adapter `builtin.delimited`. Существующий `builtin_text_parsers()` по-прежнему
возвращает только Group A и не меняет compatibility contract.

Bounded probe определяет delimiter, quote и escape по повторяемой
многоколоночной структуре. TAB даёт `format_id="tsv"`, остальные поддержанные
delimiter — `format_id="csv"`. Одноколоночный или неустойчивый sample не
подтверждает delimited format. Равноправные правдоподобные dialects отклоняются
fail-closed как `PARSER_UNSUPPORTED_FEATURE` с
`feature="ambiguous_dialect"`, поэтому spoofed MIME или extension не заставляет
adapter выбрать dialect случайно.

Escape candidate считается подтверждённым только когда меняет physical
tokenization (`delimiter`, quote или newline). Пара одинаковых escape characters
сама по себе не включает destructive unescaping: auto-detection предпочитает
`escape_char=None` и сохраняет оба characters. Exact dialect override применяет
явно выбранную escape policy.

Caller может ограничить candidate vocabulary либо задать полный immutable
dialect override:

```python
from structuraguard.parsers.builtin import (
    DelimitedDetectionOptions,
    DelimitedDialect,
    DelimitedTextParser,
)

parser = DelimitedTextParser(
    detection_options=DelimitedDetectionOptions(
        dialect_override=DelimitedDialect(
            delimiter=";",
            quote_char='"',
            escape_char="\\",
        )
    )
)
```

Override входит в `parser_options_fingerprint`; process-global
`csv.field_size_limit` и locale не изменяются. Header не становится schema:
adapter сохраняет первую подходящую строку только как bounded
`header_candidate_row`/`header_candidate_confidence` metadata. Duplicate header
values не переименовываются, type/locale conversion, business normalization и
semantic field naming не выполняются.

Каждая существующая physical cell становится `ExtractedCell` со строковым
`raw_value`. Dialect syntax снимает только quotes/escape; пробелы, ведущие нули,
locale-looking numbers, duplicate values и formula-like prefixes `=`, `+`, `-`,
`@` остаются неизменёнными и не исполняются. Cell metadata различает quoted,
escaped и explicit empty values. В ragged row отсутствующая cell представлена
отсутствием координаты, а не синтетическим `null`; blank row учитывается в
`record_count` и table row range, даже если не содержит cells.

Одна physical table передаётся последовательностью `ExtractedTable` segments с
одинаковым `table_id`. `segment_index`, inclusive `row_start_index`/
`row_end_index` и `is_last_segment` делают продолжение проверяемым. Batch boundary
проходит только между complete logical rows; quoted newline может пересекать
границы чтения, но не разрезает row. Tokenizer обрабатывает bounded slices и
проверяет cancellation во время dialect probe и между batches без полной
загрузки large source. Помимо target `BatchOptions.batch_size`, adapter раньше
закрывает segment по `max_batch_cells` или `max_batch_chars`; одна logical row
всегда помещается в эти пределы за счёт проверок конфигурации. Char budget
считает decoded record syntax и исходный `LF`/`CRLF`, включая blank rows.
Каждый batch содержит bounded deterministic prefix реальных table/cell/value
references для `source_index`.

`TabularCellLocation` хранит zero-based logical `row_index` и `column_index`.
Malformed diagnostics дополнительно возвращают безопасные one-based
`record_number` и physical `line_number`, не включая raw row. Cell-level byte
offset или lexical span исходного quoted token пока не является публичной частью
schema `1.1.0`; downstream не должен выводить его из длины decoded value.
Расширение lexical/source-span provenance отложено до отдельного решения о
версии physical schema.

`DelimitedParserLimits` задаёт `max_columns`, `max_field_size`
(в decoded Unicode code points), `max_record_chars`, `max_batch_cells`,
`max_batch_chars`, bounded header/dialect probe и унаследованные text limits.
Defaults для delimited-specific limits: 500 columns, 1 000 000 field chars,
4 000 000 record chars, 4096 cells и 8 MiB decoded chars на batch. Hard ceilings:
10 000 columns/cells, 16 MiB на field, 32 MiB на record и 32 MiB + CRLF на
batch; header probe ограничен 1024 rows, Cartesian dialect set — 128 candidates.
`ParseContext` отдельно задаёт `max_bytes`, `max_records`, batching и
`max_physical_objects`. Overflow даёт `SECURITY_LIMIT_EXCEEDED`; malformed quote
или escape — `PARSER_MALFORMED_INPUT`; недопустимая кодировка —
`PARSER_ENCODING_UNSUPPORTED`. В errors сохраняются только allowlisted reason и
координаты, без fragment исходной строки.

Group B использует собственный instance-local incremental tokenizer без
внешнего parser runtime и общий bounded encoding helper. Runtime dependencies
не изменились; Polars не является обязательной или optional dependency этого
adapter.

## Built-in JSON parsers M4-C

JSON document и line-delimited JSON регистрируются двумя независимыми adapters:

```python
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import (
    builtin_delimited_parsers,
    builtin_json_parsers,
    builtin_text_parsers,
)

registry = ParserRegistry()
registry.register_many(
    (
        *builtin_text_parsers(),
        *builtin_delimited_parsers(),
        *builtin_json_parsers(),
    )
)
```

`builtin_json_parsers()` каждый раз возвращает новые `JsonDocumentParser` и
`JsonLinesParser` с IDs `builtin.json` и `builtin.json-lines`. JSONL и NDJSON —
aliases одного physical format с `format_id="jsonl"`; отдельного NDJSON adapter
нет. Регистрация остаётся явной и instance-local.

`JsonDocumentParser` принимает один strict JSON document: object, array либо
top-level scalar. `JsonLinesParser` потоково читает independently complete JSON
records по физическим строкам; для content-only autodetection нужны как минимум
две записи. Поэтому один complete value с suffix `.jsonl` остаётся JSON document,
а пустой source с JSON MIME или extension — пустым TXT. MIME и extension не
переопределяют content; конфликтующие hints добавляют стандартные warnings.
JSON family принимает только strict UTF-8 либо UTF-8 с BOM и не вызывает
encoding auto-detection; иной codec и malformed bytes отклоняются typed error.
В probe ошибка UTF-8 без подтверждённого JSON prefix означает неприменимость
JSON adapter: она не блокирует TXT/CSV/XML с другой кодировкой. Для JSON prefix
ошибка остаётся `PARSER_ENCODING_UNSUPPORTED`; strict policy самого parse не меняется.
Whitespace-only JSONL lines пропускаются как records, но сохраняют место в
one-based physical line numbering.

Оба adapters возвращают ordered `ExtractedTreeNode` без destructive flatten.
Object members, array items, empty containers, duplicate и empty keys, nested и
повторяющиеся collections сохраняются как физическая структура. `raw_name`
содержит исходный JSON key, а число — исходный lexical token без числового
преобразования; technical type hint хранится отдельно. Parser не назначает
бизнес-сущности или semantic field names. Provenance использует RFC 6901 JSON
Pointer; duplicate members различаются через `occurrence_path`, а JSONL records
дополнительно сохраняют record index и physical line coordinates.

Batch boundary проходит только между complete JSON subtrees или JSONL records.
Top-level array использует общий `tree_id` и continuation fields
`segment_index`, `child_start_index`, `child_count`, `is_last_segment`, поэтому
его элементы не дублируются и не пропускаются между batches.
`JsonParserLimits` ограничивает nesting, nodes/key count, scalar и record size;
`ParseContext` независимо задаёт bytes, records, physical objects и batching.
Malformed JSON возвращает `PARSER_MALFORMED_INPUT`, а JSONL diagnostics содержит
one-based `line_number` без raw fragment. Переполнение лимита возвращает
`SECURITY_LIMIT_EXCEEDED`. Content внутри string/key остаётся inert и никогда не
исполняется. Общий bounded tokenizer не строит full-DOM и не добавляет новую
runtime dependency.

## SDKConfig

`SDKConfig` — пустая immutable Pydantic model для composition root M1:

- `SDKConfig.model_fields == {}`;
- значения принимаются только явно;
- неизвестное поле отклоняется `pydantic.ValidationError`;
- изменение созданного объекта отклоняется;
- environment не является источником конфигурации;
- `BaseSettings` и speculative operational fields отсутствуют.

Если `config` не передан facade, для каждого экземпляра создаётся отдельный
`SDKConfig`. Переданный объект возвращается через свойство `config` без замены.

## Facades и недоступные операции

`AsyncStructuraGuard` является canonical async-first точкой входа.
`StructuraGuard` — отдельная composition-based sync-оболочка, а не subclass
async facade. Обе принимают keyword-only `parser_registry: ParserRegistry | None`
и публикуют его через read-only свойство `parsers`. Sync facade делегирует тому
же registry, которым владеет внутренний async facade.

Обе facade объявляют имена будущих операций:

- `inspect_source`;
- `inspect_database`;
- `create_plan`;
- `validate_plan`;
- `execute`;
- `analyze`;
- `ingest`;
- `propose_schema`.

Их предметные параметры и результаты в текущей версии не являются
public contract. Позиционные и именованные аргументы не интерпретируются.
Async-вызов всегда
возбуждает `OperationNotImplementedError` с
`error_code="SDK_OPERATION_NOT_IMPLEMENTED"` и canonical operation name в
`details["operation"]`.

Sync facade передаёт аргументы async facade без изменения. При явном вызове вне
активного event loop он использует краткоживущий loop через `asyncio.run()` и
получает тот же `SDK_OPERATION_NOT_IMPLEMENTED`. Внутри активного event loop
вызов отклоняется до делегирования:

```text
SYNC_API_IN_ASYNC_CONTEXT
```

В async-приложении необходимо использовать `AsyncStructuraGuard`.

## Исключения

Все публичные категории наследуют `StructuraGuardError`. Наличие категорийных
classes не означает, что реализованы все parser groups, DB inspector или loader.

Публичные поля базовой ошибки:

| Поле | Семантика M1 |
| --- | --- |
| `error_code` | Непустой код формата `[A-Z][A-Z0-9_]*` |
| `message` | Ограниченное и санитизированное описание |
| `details` | Отсоединённая от input, рекурсивно immutable mapping |
| `run_id` | Необязательный санитизированный идентификатор |
| `retryable` | Явный признак допустимости повтора |
| `cause` | Только имя класса исходного исключения либо `None` |

Alias `code` отсутствует. Невалидный `error_code` отклоняется встроенным
`ValueError`. Недоступные pipeline-операции facade самостоятельно возбуждают
только два стабильных кода:

- `SDK_OPERATION_NOT_IMPLEMENTED`;
- `SYNC_API_IN_ASYNC_CONTEXT`.

Registry M3 использует `ParserError` со стабильными codes
`PARSER_INVALID_ADAPTER`, `PARSER_DUPLICATE_REGISTRATION`,
`PARSER_REGISTRY_FROZEN`, `PARSER_SESSION_CLOSED`, `PARSER_NOT_FOUND`,
`PARSER_UNSUPPORTED_FORMAT`, `PARSER_DEPENDENCY_UNAVAILABLE`,
`PARSER_MALFORMED_INPUT`, `PARSER_UNSUPPORTED_FEATURE`,
`PARSER_ENCODING_UNSUPPORTED`, `PARSER_PROBE_FAILED`, `PARSER_PROBE_INVALID`,
`PARSER_FORMAT_CONFLICT`,
`PARSER_PLUGIN_METADATA_INVALID`, `PARSER_PLUGIN_DISCOVERY_FAILED` и
`PARSER_OUTPUT_INVALID`. Невозможность безопасно активировать untrusted
descriptor возвращает `SecurityPolicyError` с `SECURITY_SANDBOX_REQUIRED`.
Расхождение с hints без неоднозначности selection представлено issue codes
`PARSER_DECLARED_MIME_MISMATCH` и `PARSER_EXTENSION_MISMATCH`.

### Security semantics ошибок

Перед сохранением ошибки реализация:

- удаляет распространённые password, token, API key, authorization, DSN,
  private-key и cookie credentials из известных key/header/assignment forms;
- скрывает URI userinfo, Basic/Bearer credentials и PEM private keys;
- не сохраняет текст исходного `cause`, санитизирует имя его класса и скрывает
  неявный exception context из стандартного traceback;
- заменяет циклы, превышение depth/item budget и non-finite numbers безопасными
  маркерами;
- сохраняет не более первых 4096 исходных символов и при усечении добавляет
  marker `[TRUNCATED]`; nesting ограничен 16 уровнями, общий обход `details` —
  256 узлами, включая корневой container;
- преобразует вложенные mappings и sequences в неизменяемые значения.

Redaction является defense-in-depth, а не универсальным detector secrets.
Произвольный credential под нейтральным именем может быть не распознан. Caller
обязан передавать в `message`, `details` и `run_id` только данные, безопасные для
отображения и логирования. Явное `raise error from cause` снова включает `cause`
в traceback и запрещено, если исходное исключение может содержать secret.
Неизменяемы `details`, но не весь объект исключения.

## Side effects

Проверенный import contract M1 запрещает package code при
`import structuraguard`:

- читать или менять environment;
- читать/создавать файлы, подключаться к сети или запускать process;
- создавать thread или event loop;
- менять event-loop policy, signal handlers и logging state;
- подключаться к БД или создавать tables;
- создавать global mutable registry.

После разрешения lazy exports тела constructors `SDKConfig` и обеих facade не
выполняют перечисленных действий через код StructuraGuard. Первый явный доступ к
`SDKConfig` или facade инициализирует стороннюю dependency Pydantic; эта граница
не входит в гарантию обычного `import structuraguard`. Исключение для sync facade
относится только к явному вызову операции вне активного loop: тогда приложение
создаёт краткоживущий loop, после чего получает typed failure.

Импорт `structuraguard.parsers`, создание `ParserRegistry` и создание facade не
перечисляют distributions и entry points. Metadata discovery является I/O и
начинается только из явного `discover_plugins()`.

## Зависимости и ограничения текущего среза

### XML / HTML / YAML

```python
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import (
    XmlParser, XmlParserLimits, HtmlParser, YamlParser, YamlParserLimits,
    html_safe_json,
)

registry = ParserRegistry()
registry.register(XmlParser(limits=XmlParserLimits(max_depth=32, max_nodes=100_000)))
registry.register(HtmlParser())
registry.register(YamlParser(limits=YamlParserLimits(max_aliases=0)))
```

Установите `structuraguard[xml,yaml]`, если нужны XML/YAML adapters.
`builtin_markup_parsers()` возвращает новые независимые instances для явной
регистрации. Optional backends не импортируются при factory/import пакета.

XML сохраняет namespace-aware tree/XPath; DTD/entities запрещены. HTML выдаёт
source-event DOM, headings/text/list blocks и физические tables/cells, не
выполняет active content и не загружает resources. `html_safe_json(batch)`
позволяет сериализовать raw payload без HTML delimiters; raw fields нельзя
передавать в `innerHTML`. YAML сохраняет hierarchy, duplicate/complex keys,
lexemes, safe tags и marks. Aliases — ограниченные ссылки, без expansion.

`XmlParserLimits`, `HtmlParserLimits`, `YamlParserLimits` конфигурируют finite
depth/node/text/token/subtree/batch limits. Дополнительно действуют bytes,
records, physical object и batch limits `ParseContext`. Неразрезаемый большой
subtree/document отклоняется с `SECURITY_LIMIT_EXCEEDED`. DTD/unsafe YAML tag —
`SECURITY_INPUT_REJECTED`; malformed input — `PARSER_MALFORMED_INPUT` с physical
line number, неизвестный XML codec/невалидный UTF-8 — typed encoding error.
HTML/YAML имеют явную strict UTF-8 policy.

HTML adapter явно сохраняет marked-section grammar: завершённая section с
неизвестным именем, например &lt;![bogus]&gt;, даёт `PARSER_MALFORMED_INPUT`
с physical line number, а поддержанные CDATA и
conditional sections остаются inert declarations. Это не зависит от нового
HTML5 dispatch stdlib, превращающего неизвестную section в bogus comment.
Такой же текст внутри attribute, comment или script/style не считается section
и сохраняется как raw data; JavaScript и conditional content не исполняются.

YAML probe ищет структурные признаки в первой значимой строке, пропуская
комментарии; двоеточие в последующей CSV cell не подтверждает YAML. JSONL с
разными типами корневых records остаётся у JSON adapters. Ошибки подтверждённого
YAML и запрет unsafe tags не подменяются успешным TXT fallback.

XML batching идёт по direct-child subtrees, HTML — top-level DOM subtrees,
YAML — documents; у XML/HTML есть continuation root. CSS coordinates относятся
к исходному event DOM, а не к исправленному browser DOM; attribute position —
enclosing tag плюс occurrence. YAML marks не включают comments/byte offsets.
Полные гарантии и ограничения: [ADR 0005](adr/0005-safe-markup-extraction.md).

### Встроенные document adapters (M4-E)

`structuraguard.parsers.builtin` экспортирует независимые `XlsxParser`,
`PdfParser`, `DocxParser`, соответствующие `*ParserLimits` и factory
`builtin_document_parsers()`. Регистрация остаётся явной. Установка:
`pip install 'structuraguard[excel,pdf,office]'`.

XLSX сохраняет worksheet tables/row segments/cells, merged ranges, точные имена
листов, raw cached values и отдельные formula sources. DOCX сохраняет mixed
block/table order, paragraphs/headings/lists, nested cells, runs и properties.
DOCX `noBreakHyphen`/`softHyphen` сохраняются как U+2011/U+00AD с run provenance.
Неизвестные содержательные run elements и неподдерживаемые контейнеры внутри
table/row/cell, включая content controls, дают `PARSER_UNSUPPORTED_FEATURE`,
а не успешный результат с потерянным текстом.
PDF извлекает text pages/blocks/lines, геометрические table candidates и bbox;
документ без текста возвращает `PARSER_NO_TEXT_LAYER`. OCR отсутствует.

Все adapters используют bounded subprocess/snapshot с hard deadline и cleanup.
`batch_size`: XLSX rows, PDF pages, DOCX body blocks. Limits задаются при создании
адаптера; действуют также ограничения `ParseContext`. `strict_mode=True` требует
будущий sandbox runner и сейчас возвращает `SECURITY_SANDBOX_REQUIRED`.
Worker поддерживает Linux/macOS; это не security sandbox. Возможен отказ на
active/unsupported documents и oversized неделимом XML part/page/table.
Worker limit error сохраняет allowlisted `resource` и числовой `limit` без raw
diagnostics. Crash, повреждённый transport и неожиданный backend defect дают
`PARSER_OUTPUT_INVALID`; известный malformed PDF остаётся `PARSER_MALFORMED_INPUT`.
Raw output не формирует `ParsePlan`, не назначает сущности и не безопасен для
`innerHTML` без escaping. Политики, точность provenance, memory caveats и
лицензирование PDF backend: [ADR 0006](adr/0006-bounded-document-adapters.md).

### Optional Tika fallback (M4-F)

`pip install 'structuraguard[tika]'` подключает HTTPX и defusedxml, но не включает
adapter и не устанавливает/запускает Tika/JVM. Публичные классы находятся только
в `structuraguard.parsers.tika`: `TikaParserAdapter`, `TikaConfig`,
`TikaParserLimits`, `TikaEgressApproval`. Default factories не меняются.

Extra также закрепляет `httpcore==1.0.9` для request-local redaction transport logs.
Недоверенные response headers/reason/errors не попадают в HTTPX/httpcore logs;
глобальная logging configuration caller не меняется. Перед upload проверяются
SHA и сигнатура одного snapshot; изменение формата после probe не разрешает egress.

Caller создаёт отдельный fallback-only `ParserRegistry` после
`PARSER_UNSUPPORTED_FORMAT` core selection. Security, malformed и conflict errors
не разрешают fallback. Начальный allowlist ограничен RTF и PostScript;
probe читает до 16 байт локально, без HTTP. Копируемая функция конфигурации ниже
не выполняет upload. `approval` поступает от реальной DLP-проверки caller;
функция не выдаёт его сама. `server_version` — версия закреплённого deployment,
заявленная caller: adapter записывает её в provenance, но не проверяет версию
сервера по сети.

<!-- example:m04-tika-config:start -->
```python
from structuraguard.parsers.tika import (
    TikaConfig,
    TikaEgressApproval,
    TikaParserAdapter,
    TikaParserLimits,
)


def build_tika_fallback(
    *, endpoint: str, server_version: str, approval: TikaEgressApproval
) -> TikaParserAdapter:
    return TikaParserAdapter(
        config=TikaConfig(
            enabled=True,
            endpoint=endpoint,
            allowed_media_types=frozenset({"application/rtf"}),
            expected_server_version=server_version,
        ),
        approval=approval,
        limits=TikaParserLimits(max_request_bytes=8 * 1024 * 1024, timeout_seconds=30),
    )
```
<!-- example:m04-tika-config:end -->

`TikaEgressApproval` разрешает только проверенный `PUBLIC` snapshot:
`secrets_checked=True`, `contains_secrets=False`. Без approval upload запрещён.
До HTTP adapter проверяет размер, SHA-256 и известные credential markers всего
snapshot. Этот дополнительный scanner **не заменяет DLP caller**, особенно для
RTF escapes и обфускации. Filenames/metadata/credentials/environment не передаются;
redirects, response compression, DTD/entities и неожиданные content types запрещены.
HTTPS обязателен вне numeric loopback; endpoint не поддерживает auth/query.

Результат — `ExtractedBatch` с физическим XHTML tree. Версионированная
`ExtensionLocation` указывает XPath в ответе Tika, **не координаты исходного файла**.
Raw values сохраняются; consumer экранирует их при HTML rendering. Bounded response
проверяется до первой выдачи; действуют transport/XML limits, batching и cancellation.
Ошибки: `PARSER_TIKA_UNAVAILABLE`, `PROCESSING_TIMEOUT`, `PARSER_MALFORMED_INPUT`,
а также существующие dependency/security errors.

Network/container isolation, отключение OCR/active content на сервере, server
resource limits и secret review обеспечивает вызывающий проект. Client timeout
не гарантирует остановку удалённого job. Default tests используют fake server,
не внешний Tika; фактическая совместимость deployment требует отдельной проверки.
Подробности и изменение прежнего F/M12 gate: [ADR 0007](adr/0007-opt-in-tika-egress.md).

### Runtime dependencies

- Требуется Python 3.12 или новее.
- Unconditional runtime dependencies — Pydantic v2 и
  `charset-normalizer>=3.4,<4`.
- Web frameworks отсутствуют в core dependencies.
- Extras `xml`, `yaml`, `pdf`, `excel`, `office`, `tika` подключают backends реализованных
  adapters; `all` включает их без активации. `postgres`, `litellm` пока
  резервируют dependency bundles.
- Registry, parser boundary и группы TXT/LOG/Markdown, CSV/TSV и
  JSON/JSONL/NDJSON, XML/HTML/YAML и XLSX/PDF/DOCX реализованы, Tika доступен только
  opt-in. Semantic analyzer/executor, DB inspection/load,
  validation pipeline и LLM operations отсутствуют.
- M3 не реализует orchestrator и не имеет успешного ingest-сценария.
- Terminal manifests сами проверяют terminal batch. Для всей последовательности
  caller обязан один раз передать ordered iterable в `validate_batches()`:
  проверка потоково сверяет lineage-aware summaries, terminal marker, counts и
  глобальную уникальность normalized IDs, не удерживая raw values всех batches.
  Эта проверка не запускается автоматически для уже отданных non-terminal
  batches; orchestration её вызова относится к будущему executor.

Требования scaffold определены разделами [M1][spec-m1],
[NFR-001][spec-nfr-001], [NFR-003][spec-nfr-003] и
[NFR-012][spec-nfr-012] канонического ТЗ. Локальная трассировка реализации и
evidence scaffold находится в [плане M1](plans/M01_sdk_scaffold.md), а contracts
— в [плане M2](plans/M02_domain_contracts.md) и [ADR 0003](adr/0003-two-stage-parsing-contracts.md).
Реализация registry и discovery прослеживается через
[план M3](plans/M03_parser_registry.md) и [раздел M3 ТЗ][spec-m3].

[spec-m1]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m1-каркас-python-пакета
[spec-nfr-001]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#nfr-001-встраиваемость
[spec-nfr-003]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#nfr-003-async-first
[spec-nfr-012]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#nfr-012-ошибки
[spec-public-api]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#23-публичный-api-sdk
[spec-m2]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m2-доменные-модели-и-contracts
[spec-m3]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m3-parser-registry
[spec-m4]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m4-technical-parsers
[spec-nfr-006]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#nfr-006-streaming
