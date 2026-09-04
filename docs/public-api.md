# Публичный API StructuraGuard

Статус: подтверждённое поведение package `structuraguard` версии `0.3.0` в
текущей реализации M3. Facade-операции pipeline в эту поставку не входят.

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

M2 фиксирует contracts, но не предоставляет concrete parsers, analyzers,
validators/executors, DB reflection/load, LLM providers, staging или audit
backends.

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
`PARSER_NOT_FOUND`. Реальные format parsers, semantic analyzer и orchestrator в
M3 отсутствуют.

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
classes не означает, что parser, DB inspector или loader уже реализованы.

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
`PARSER_PROBE_FAILED`, `PARSER_PROBE_INVALID`, `PARSER_FORMAT_CONFLICT`,
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

## Зависимости и ограничения M3

- Требуется Python 3.12 или новее.
- Единственная unconditional runtime dependency — Pydantic v2.
- Web frameworks отсутствуют в core dependencies.
- Extras `postgres`, `pdf`, `excel`, `office`, `litellm`, `tika` и `all`
  резервируют dependency bundles. Установка extra не добавляет готовый adapter.
- Registry и parser boundary реализованы, но concrete format parsers, semantic
  analyzer/executor, DB inspection/load, validation pipeline и LLM operations
  отсутствуют.
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
