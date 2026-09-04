# План M03 «Parser Registry и безопасное discovery plugins»

Статус: реализован, проверен и готов к ручному commit и Pull Request в `main`.

Дата плана: 2026-09-03.

Последняя проверка: 2026-09-04.

## Цель

Добавить instance-local реестр technical parsers с воспроизводимым выбором по
content evidence, `ProbeResult.confidence` и `priority`, а также явно вызываемое
discovery группы `structuraguard.parsers`, которое не импортирует недоверенный
plugin в host process.

## Основание и принятые уточнения

План опирается на текущую задачу, `AGENTS.md`, `PROJECT_CONTEXT.md`,
`SPEC_INDEX.md` и точечно извлечённые разделы ТЗ: `5`, `FR-001`–`FR-002`,
[FR-003][spec-fr-003], `NFR-004` и [M3][spec-m3]. Дополнительно проверены действующие
parser contracts,
относящиеся к ним ADR 0002/0003 и текущие parser, contract, facade, import и
packaging tests. Полное ТЗ не загружалось.

Иллюстративный contract в `FR-003` уточнён уже реализованным M2 contract и в M3
не меняется:

- идентичность parser задают read-only `adapter_id`, `version` и `priority`, а не
  поле `name`;
- `probe()` является coroutine и возвращает `ProbeResult`;
- `parse()` вызывается без дополнительного `await` и возвращает
  `AsyncIterator[ExtractedBatch]`;
- оба метода получают fingerprint-bound bounded context; DB и LLM ports parser
  не получает.

`SourceArtifact.media_type` в M3 трактуется как недоверенный declared MIME hint,
а `ProbeResult.detected_media_type` — как результат content-based probe.
Расширение извлекается только из `SourceArtifact.display_name` без обращения к
filesystem. `ProbeResult.confidence` является probe score. Для разрешения
конфликтов существующему результату не хватает signal-level evidence и
canonical format identity; M3 добавляет optional поля с defaults. Старый JSON
по-прежнему валиден, но legacy result без evidence не становится selectable в
новом registry: до M3 API выбора parser не существовало.

ADR 0002 уже запрещает host-side import/constructor недоверенного parser, а ADR
0003 закрепляет границу `Parser → ExtractedBatch → ParsePlanExecutor →
NormalizedBatch`. Новое долгоживущее архитектурное решение для M3 не требуется.

## Наблюдаемое состояние до M3

- `structuraguard.ports.Parser`, `ProbeContext`, `ParseContext`, `ProbeResult` и
  physical `ExtractedBatch` реализованы и покрыты M2 tests.
- `ProbeResult.confidence` — конечный `Decimal` в диапазоне `[0, 1]`, но contract
  не сообщает, какие signature/MIME/structure/extension signals дали оценку.
- `ExtractedBatch` содержит только physical lines, blocks, tables, trees,
  provenance и terminal manifest; semantic и DB mapping fields отсутствуют.
- `structuraguard.parsers`, registry, immutable registry snapshot, selection,
  общий `FakeParser` и entry-point discovery отсутствуют.
- `AsyncStructuraGuard` и `StructuraGuard` не имеют свойства `parsers`; facade
  operations остаются fail-loud scaffold.
- Текущие `_FakeParser`/`_TypedParser` локальны для M2 tests и проверяют только
  structural typing. Import smoke не проверяет отсутствие metadata scan при
  создании registry/facade.
- Основа typed errors уже существует: `ParserError`, `SecurityPolicyError`,
  санитизированные immutable `details` и закрытый `BuiltInErrorCode`.

## Границы scope

В M3 входят:

- ручная регистрация trusted parser objects через
  `sdk.parsers.register(parser)`;
- instance-local mutable composition registry и immutable snapshot для одного
  выбора/run;
- deterministic probe selection и typed detection evidence;
- единая duplicate policy для manual objects и plugin descriptors;
- generic validation потока `ExtractedBatch` на parser boundary;
- reusable `FakeParser` и contract/security regression suite;
- явное discovery entry points только группы `structuraguard.parsers` с
  bounded validation принятых immutable declarative descriptors;
- минимальное подключение registry к async/sync facade без реализации pipeline;
- exports, import-safety, distribution metadata/verifier и документация M3.

В M3 не входят:

- реальные TXT/LOG/CSV/JSON/XML/HTML/XLSX/PDF/DOCX/YAML parsers;
- format-specific magic tables, encoding detection и optional parser
  dependencies — это M4;
- преобразование всех raw входов из `FR-001` в source snapshot, remote URL
  adapter и source lease orchestration;
- semantic profiler/analyzer, создание или выполнение `ParsePlan`, готовые
  бизнес-сущности, `MappingPlan` и DB mapping;
- `SandboxParserRunner`, загрузка plugin artifact и выполнение plugin code — это
  отдельная реализация security runtime в M12; M3 фиксирует descriptor boundary
  и fail-closed поведение до её появления;
- auto-install dependencies, network resolution, изменение `sys.path`, hot
  reload и неявная замена зарегистрированного parser.

## Наблюдаемое поведение после M3

1. Каждый новый facade получает отдельный пустой `ParserRegistry`; общий
   registry возможен только через явную constructor injection. Sync facade
   возвращает тот же registry, которым владеет его внутренний async facade.
2. `sdk.parsers.register(parser)` валидирует наличие protocol members,
   callability и snapshot значений `adapter_id`, `version`, `priority`, затем
   атомарно добавляет trusted object. Runtime shape результатов проверяется при
   вызове; import/constructor object уже выполнил composition owner.
3. Registry использует per-instance lock и copy-on-write state. Обязательный
   async context `registry.session()` удерживает frozen snapshot lease от
   selection до завершения/закрытия parse iterator; пока session активна,
   mutation того же registry typed-fails.
4. Security/trust и availability являются hard eligibility gates и не
   смешиваются со score. Валидные candidates сначала сравниваются по точному
   content-evidence vector, затем по `confidence`, `priority` и `adapter_id`.
5. Selection возвращает session-bound immutable selected-parser handle.
   Parse wrapper проверяет local invariants до каждого yield, а aggregate
   manifest — при terminal batch; до успешного исчерпания iterator batches
   считаются provisional и не могут финализировать downstream artifact.
6. Ни import package, ни импорт `structuraguard.parsers`, ни создание facade или
   registry не обращаются к `importlib.metadata` для поиска distributions или
   entry points.
7. Только явный `sdk.parsers.discover_plugins(policy=...)` просматривает exact
   group и атомарно добавляет accepted subset validated descriptors. В M3 discovery никогда
   не вызывает `EntryPoint.load()`, `import_module`, factory или constructor.
8. Попытка выполнить untrusted descriptor без sandbox завершается
   `SECURITY_SANDBOX_REQUIRED`; in-process fallback отсутствует.

## Затронутые контракты

| Контракт | Изменение M3 |
| --- | --- |
| `Parser` | Signature M2 сохраняется без изменений. Registry снимает immutable identity snapshot и проверяет соответствие каждого результата. |
| `ProbeResult` | Аддитивно получает optional canonical `format_id` и bounded tuple typed signals без raw bytes, filename или plugin metadata. DTO сохраняет M2 validation/JSON compatibility; более строгая eligibility проверяется только M3 registry. `confidence` и есть score. |
| `ProbeSignal` | Новый frozen contract с закрытыми `kind` (`signature`, `content_media_type`, `internal_structure`, `declared_media_type`, `extension`) и `outcome` (`match`, `mismatch`, `inconclusive`); kind уникален внутри результата. |
| `format_id` | Новый открытый lowercase ASCII identifier длиной 1–128 с той же segment grammar, что adapter ID. MIME aliases и container details сводит к одному ID сам parser; например, XML MIME aliases дают один format ID, а ZIP signature с XLSX structure — итоговый XLSX ID. |
| `ParserPluginDescriptor` | Новый frozen declarative contract: canonical `adapter_id`, distribution name/exact version, parsed `module:attribute`, entry-point group и canonical metadata fingerprint. Никакого callable/code object или произвольного metadata mapping. |
| `ParserDiscoveryPolicy` | Frozen runtime policy: непустой allowlist не более 64 canonical distribution names и `max_entries` от 1 до hard cap 256. Policy не читается из environment. |
| `ParserRegistry` | Concrete instance-local composition API: single/bulk registration, explicit discovery, copy-on-write state, обязательная async `session()` и read-only introspection в canonical order. |
| Registry session/snapshot | Неизменяемый набор manual registrations и plugin descriptors; selection никогда не читает live registry. Async context держит lease до закрытия всех созданных им iterators и запрещает mutation исходного registry. |
| Selected parser handle | Хранит private trusted object, identity snapshot и `ProbeResult`, связан с открытой session и отдаёт provisional locally-validated `AsyncIterator[ExtractedBatch]`; aggregate success наступает только на terminal manifest. |
| Facades | Получают `parsers` property и optional explicit `parser_registry` injection. Остальные M1 operations и root lazy exports не меняются. |
| Errors | Используются существующие `ParserError`/`SecurityPolicyError` с новыми stable `BuiltInErrorCode`; raw plugin/source data в details не попадают. |

Persistence schema и миграции данных отсутствуют. Новый descriptor является
versioned wire contract для будущего sandbox handshake; изменение его canonical
полей потребует schema-version policy.

## Registration, snapshot и duplicate policy

- Module-level mutable registry, decorators с auto-registration и implicit
  singleton запрещены. Допустимы только immutable constants, включая имя
  entry-point group.
- Одна canonical adapter ID policy применяется к manual и plugin origins:
  lowercase ASCII grammar `^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$`, длина 1–128.
  Unicode, whitespace и case folding не используются как скрытая нормализация:
  неканонический input отклоняется.
- `ParserRegistry` проверяет `isinstance(parser, Parser)`, callability
  `probe`/`parse` и identity exact types `str`, `str`, `int`; `bool` как priority
  отклоняется, допустимый диапазон priority — signed 32-bit
  `[-2_147_483_648, 2_147_483_647]`. Runtime protocol не доказывает signatures,
  поэтому coroutine/iterator shape проверяется на границе вызова, а полная
  substitutability — strict mypy contract tests. Property values снимаются один
  раз и далее считаются identity snapshot.
- Один canonical `adapter_id` образует единый namespace для manual object и
  discovered descriptor. Версия и более высокий priority не дают права занять
  существующий ID.
- Любой второй claim того же ID — manual/manual, manual/plugin или plugin/plugin
  — вызывает `PARSER_DUPLICATE_REGISTRATION`. Registry не применяет last-wins,
  version-wins, `replace=True` или неявный override.
- Single и bulk manual registration атомарны: validation и полный duplicate
  preflight завершаются до изменения registry. Discovery изолирует ошибки
  отдельных distributions/entries/collisions и атомарно добавляет только
  accepted subset; каждая ошибка остаётся видимой в typed report.
- Registry хранит immutable state object и заменяет его под per-instance
  `threading.RLock`; ни один `await` под lock не выполняется. Duplicate preflight
  повторяется непосредственно перед compare-and-swap, поэтому две threads не
  могут одновременно занять один ID.
- `session()` — обязательный async context для selection/parse. На enter он
  фиксирует snapshot и увеличивает active-session counter под тем же lock. На
  exit он сначала переходит в closing state, ждёт выполняющийся selection и
  вызывает `aclose()` всех незавершённых iterators. Normal body exception и
  cancellation не прерывают cleanup; после него lease освобождается, а исходная
  cancellation распространяется. Ошибка самого iterator `aclose()` остаётся
  видимой и допускает ручной retry, но wrapper уже quarantined и не выдаёт
  output. Selected handle после начала exit typed-fails как
  `PARSER_SESSION_CLOSED`.
- Пока существует session, `register`, bulk registration и discovery завершаются
  `PARSER_REGISTRY_FROZEN`. Это относится и к явно shared injected registry.
- Replacement/unregister не нужны в M3. Composition owner создаёт новый
  registry для другой конфигурации; текущий snapshot остаётся воспроизводимым.

## Probe selection и conflict semantics

### Eligibility и порядок

1. Snapshot отделяет trusted manual objects от untrusted descriptors. Descriptor
   без sandbox не выполняется и не участвует в host-side probe.
2. Candidates probing выполняется в canonical `adapter_id` order через один
   fingerprint-bound `ProbeContext`, последовательно: `SourceReader` не обещает
   безопасные concurrent reads. Registration/discovery order не является входом
   алгоритма; probe concurrency остаётся вне M3.
3. Для каждого `ProbeResult` core проверяет exact type, source ref/fingerprint,
   `adapter_id`, version, `format_id`, score, detected MIME и signal invariants.
   `supported=True` valid только при `confidence > 0`, canonical `format_id`,
   хотя бы одном strong match и отсутствии strong mismatch. Нарушение, включая
   legacy result без evidence, даёт `PARSER_PROBE_INVALID`. Нормальный
   `supported=False` требует zero score и не содержит positive strong signal.
4. Ожидаемая недоступность optional dependency помечает candidate недоступным
   стабильным `PARSER_DEPENDENCY_UNAVAILABLE`; неизвестное исключение probe
   прерывает выбор как `PARSER_PROBE_FAILED`, а cancellation не перехватывается.
5. Hard security/availability gates применяются до ranking. Заблокированный
   parser не может выиграть высоким score или priority.
6. Для каждого eligible result строится один exact boolean vector
   `(internal_structure_match, signature_match, content_media_type_match)`.
   Сравнение vector лексикографическое по убыванию. Среди candidates с
   максимальным vector разные `format_id` означают `PARSER_FORMAT_CONFLICT`;
   MIME alias обязан иметь тот же format ID.
7. Если maximal-vector candidates имеют один `format_id`, полный оставшийся
   ключ ranking: `ProbeResult.confidence` по убыванию, `priority` по убыванию,
   canonical `adapter_id` лексикографически по возрастанию. Version и порядок
   регистрации не используются как preference.

### Приоритет сигналов

Сильные content signals (`signature`, content-derived MIME и подтверждённая
internal structure) имеют приоритет над declared MIME, а declared MIME — над
extension. Внутри content vector internal structure стоит первой, чтобы
различать форматы общего container, затем signature и content MIME. Declared
MIME/extension не входят в ranking: они помогают parser выбрать bounded probe
strategy и формируют warnings, но сами по себе не делают candidate eligible.

| Ситуация | Результат |
| --- | --- |
| Content signature/internal structure согласованы, declared MIME или extension противоречат | Выбирается content-confirmed parser; evidence получает `PARSER_DECLARED_MIME_MISMATCH` и/или `PARSER_EXTENSION_MISMATCH`. |
| Candidate одновременно сообщает strong match и strong mismatch своему `format_id` | Result отклоняется как `PARSER_PROBE_INVALID`; противоречивое evidence не ранжируется. |
| Candidates с одинаковым maximal content vector указывают разные canonical `format_id` | Fail closed с `PARSER_FORMAT_CONFLICT`; score и priority конфликт не скрывают. |
| Declared MIME и extension конфликтуют, а content probe inconclusive | `PARSER_UNSUPPORTED_FORMAT`; MIME/extension-only selection запрещён. |
| Совпадает только extension, но положительного content probe нет | `PARSER_UNSUPPORTED_FORMAT`; parser не выбирается по suffix. |
| Несколько parsers подтверждают один content format | Применяется deterministic ranking `evidence → confidence → priority → adapter_id`. |

`confidence` является score и остаётся `Decimal`, а не binary float. Registry
не пересчитывает и не суммирует произвольные «бонусы» MIME/extension.
Tie-breaking выбирает parser воспроизводимо, но не используется для маскировки
содержательного format conflict.

Строки с `PARSER_UNSUPPORTED_FORMAT` предполагают корректный parser, который при
неубедительном content probe возвращает `supported=False`. Если он возвращает
`supported=True` без обязательного strong evidence, это malformed result и
`PARSER_PROBE_INVALID`, а не обычный unsupported outcome.

### Terminal failure semantics

Outcomes применяются в следующем строгом порядке; более ранняя строка имеет
приоритет над более поздней.

| Условие | Public outcome |
| --- | --- |
| Registry session не содержит trusted manual registrations | `PARSER_NOT_FOUND`. Discovered descriptors выбираются отдельной sandbox-bound операцией. |
| Любой result/call shape invalid либо probe бросил неожиданное исключение | Первый по canonical adapter order hard failure: `PARSER_PROBE_INVALID` или `PARSER_PROBE_FAILED`; поддержанный ранее candidate не создаёт fallback. |
| Maximal content vector представлен разными `format_id` | `PARSER_FORMAT_CONFLICT`. |
| Есть хотя бы один valid supported result | Победитель по content vector/score/priority/ID; expected unavailable и normal unsupported candidates остаются safe evidence. |
| Supported result нет, но хотя бы один candidate недоступен из-за optional dependency | `PARSER_DEPENDENCY_UNAVAILABLE` с safe counts; registry не утверждает, что format unsupported. |
| Все вызванные parsers вернули нормальный `supported=False` | `PARSER_UNSUPPORTED_FORMAT`. |

`select_trusted()` рассматривает только manual trusted objects. Явный выбор
plugin descriptor использует отдельную sandbox-bound operation; до M12 она
детерминированно возвращает `SECURITY_SANDBOX_REQUIRED` и не влияет на outcome
trusted selection.

## Parse output boundary

- Public и static contract остаётся
  `parse(SourceArtifact, ParseContext) -> AsyncIterator[ExtractedBatch]`.
- До каждого yield selected-parser wrapper проверяет exact physical DTO type,
  source fingerprint, parser ID/version, ожидаемый batch index, extraction
  identity и локальную physical provenance. Он хранит только batch summaries и
  bounded набор physical refs для aggregate source index, а не raw batches.
- Manifest доступен только в terminal batch. При его получении wrapper сверяет
  весь накопленный ordered summary sequence, terminal marker и aggregate
  lineage; завершение iterator без valid terminal batch также является
  `PARSER_OUTPUT_INVALID`.
- Non-terminal batches по необходимости streaming передаются downstream как
  provisional. Consumer не имеет права формировать accepted manifest,
  semantic artifact, staging commit или иной финальный успех до нормального
  исчерпания iterator. Terminal failure требует отбросить provisional state;
  M3 contract test проверяет эту границу, не добавляя staging/load.
- `NormalizedBatch`, произвольная business entity, `ParsePlan`, `MappingPlan`,
  dict или иной объект дают `PARSER_OUTPUT_INVALID`. Они не передаются analyzer,
  staging или DB layer.
- Resource violation использует `SECURITY_LIMIT_EXCEEDED`; session закрывает
  iterator при early abandon/cancellation, а cancellation распространяется без
  преобразования в fallback.
- `FakeParser` умеет выдавать valid one/multi-batch sequence и программируемые
  malformed outputs, но не реализует настоящий формат или semantic analysis.

## Безопасное entry-point discovery

- Единственная group constant — `structuraguard.parsers`. Group не принимается
  из plugin metadata или пользовательской строки; spoofed groups игнорируются.
- Discovery требует прямого вызова и frozen policy с непустым allowlist
  не более 64 PEP 503-normalized distribution names и `max_entries` от 1 до
  hard cap 256. Environment/config-флаг сам по себе scan не запускает.
- Вместо глобального enumeration вызывается stdlib
  `importlib.metadata.distribution(name)` только для allowlisted distributions,
  после чего их entries фильтруются по exact group. Metadata посторонних
  distributions не разбирается M3 discovery и не может откатить его batch.
- Для filesystem `PathDistribution` SDK не вызывает небounded свойства stdlib.
  Он открывает `.dist-info` и только фиксированные `METADATA`/`PKG-INFO` и
  `entry_points.txt` через pinned directory/file descriptors с `O_NOFOLLOW`,
  проверяет regular-file identity и mutation, читает metadata header не более
  64 KiB и entry points не более 256 KiB. Symlink, FIFO, device, ZIP/custom
  provider и платформа без безопасного `dir_fd` отклоняются fail closed.
- Network, package installation, dependency resolution, загрузка target по
  filesystem path и `sys.path` mutation запрещены.
- Entry-point metadata считаются attacker-controlled. До регистрации
  проверяются accepted-entry count, adapter/distribution name до 128/255,
  version до 128 и `module:attribute` до 512 символов, единая ASCII grammar
  adapter ID, canonical distribution name, exact non-empty version, отсутствие
  extras, relative/path/URL/call syntax, control/bidi symbols и credential
  canaries.
- `entry_point.name` является claimed `adapter_id`. Descriptor хранит только
  allowlisted поля и canonical fingerprint; raw metadata, path, exception text,
  stdout/stderr не попадают в repr, error details или logs.
- Enumeration нормализуется и сортируется по descriptor identity до duplicate
  preflight. Ошибка metadata, enumeration или collision изолируется на уровне
  конкретной distribution/entry; корректные siblings сохраняются, а failures
  возвращаются вместе с accepted descriptors. Превышение общего hard limit
  отклоняет неподходящие entries без выполнения plugin code.
- Discovery и activation разделены. `EntryPoint.load()`, module import и
  constructor в host process не вызываются даже после opt-in discovery.
  Недоверенный descriptor позже может быть разрешён и загружен только внутри
  `SandboxParserRunner`, который сверит distribution/version/target/fingerprint.
- Пока sandbox runner не реализован, descriptor остаётся discoverable и
  inspectable, но не executable; попытка probe/parse завершается
  `SecurityPolicyError(SECURITY_SANDBOX_REQUIRED)` без in-process fallback.
- Ручной `register(parser)` остаётся отдельной доверенной веткой: SDK не может
  sandbox уже созданный composition owner object и явно документирует этот
  residual risk.

## Typed errors

M3 добавляет stable codes в `BuiltInErrorCode`, сохраняя существующую hierarchy:

| Error class | Codes |
| --- | --- |
| `ParserError` | `PARSER_INVALID_ADAPTER`, `PARSER_DUPLICATE_REGISTRATION`, `PARSER_REGISTRY_FROZEN`, `PARSER_SESSION_CLOSED`, `PARSER_NOT_FOUND`, `PARSER_UNSUPPORTED_FORMAT`, `PARSER_DEPENDENCY_UNAVAILABLE`, `PARSER_PROBE_FAILED`, `PARSER_PROBE_INVALID`, `PARSER_FORMAT_CONFLICT`, `PARSER_PLUGIN_METADATA_INVALID`, `PARSER_PLUGIN_DISCOVERY_FAILED`, `PARSER_OUTPUT_INVALID` |
| `SecurityPolicyError` | существующие `SECURITY_SANDBOX_REQUIRED`, `SECURITY_LIMIT_EXCEEDED` |

Для non-fatal расхождения hints `BuiltInIssueCode` аддитивно получает
`PARSER_DECLARED_MIME_MISMATCH` и `PARSER_EXTENSION_MISMATCH`; они не заменяют
error outcome при конфликте сильных content signals.

`details` ограничены validated adapter ID, origin kind, safe reason code,
field/count/limit. Original exception сохраняется только как санитизированное имя
типа; filename, raw MIME/extension, entry-point target, filesystem path и source
content исключены. Неожиданные `ValueError`, `ImportError` и ошибки metadata не
выходят через public boundary напрямую.

## Критерии приёмки

Выполнено: 18 из 18 критериев.

- [x] **AC-01.** Два default facade/registry не разделяют registration state;
  явная injection является единственным способом совместного владения registry.
- [x] **AC-02.** Session snapshot immutable, canonical и не меняется.
  Перестановки одной registration set дают одинаковые ordering и fingerprint,
  рассчитанный только из validated identities/descriptors, но не из object
  repr/address.
- [x] **AC-03.** Per-instance lock/copy-on-write делает concurrent duplicate
   registration атомарной; session lease отклоняет mutation shared registry,
   ждёт in-flight selection, quarantines все wrapper streams и вызывает
   `aclose()` у close-capable iterators при never-started parse, early break,
   success, body exception и cancellation. Handle после начала exit typed-fails;
   failure самого `aclose()` остаётся видимым и retryable, но не удерживает
   registry lease и не допускает output после exit.
- [x] **AC-04.** Полная duplicate/canonical-ID matrix отклоняется typed error
  без частичного изменения и без takeover по case, Unicode, version или
  priority; priority outside signed 32-bit и `bool` отклоняются до
  fingerprint/ranking.
- [x] **AC-05.** Shuffled registration выбирает один parser по exact
   `content vector → format compatibility → confidence → priority → adapter_id`;
   sequential probing и security gate не зависят от scheduling.
- [x] **AC-06.** Contract tests покрывают signature/MIME/extension matrix:
  spoofed extension не выигрывает, metadata mismatch даёт typed warning, а
  strong/strong conflict и metadata-only detection fail closed.
- [x] **AC-07.** Forged `ProbeResult` с чужим source/parser/version, неверным
  score или противоречивыми signals отклоняется до selection.
- [x] **AC-08.** Legacy `ProbeResult` без M3 evidence сохраняет M2 JSON round-trip, но
   `supported=True` result без evidence новый registry возвращает как
   `PARSER_PROBE_INVALID`, а не как ordinary unsupported format.
- [x] **AC-09.** Reusable `FakeParser` удовлетворяет текущему `Parser` protocol
  и выдаёт valid `AsyncIterator[ExtractedBatch]`; static negative fixtures не
  позволяют вернуть `NormalizedBatch` или `MappingPlan`.
- [x] **AC-10.** Registration/call-boundary tests отклоняют non-callable, non-awaitable
    `probe` и coroutine/non-async-iterator `parse` typed errors; runtime suite
    отклоняет чужую lineage, broken manifest, duplicate/reordered batches,
    oversize и любой non-`ExtractedBatch` output до downstream.
- [x] **AC-11.** Non-terminal valid batches доступны только как provisional;
  missing/broken terminal manifest даёт `PARSER_OUTPUT_INVALID`, и consumer
  contract не допускает финализацию результата до успешного исчерпания iterator.
- [x] **AC-12.** Import package, import parser subpackage и создание обоих
  facades не вызывают metadata scan, plugin load/import/constructor,
  environment/network access или создание runtime resources.
- [x] **AC-13.** Явное discovery разрешает только allowlisted distributions и
  exact group, принимает ограниченное число bounded descriptors, игнорирует
  malformed metadata вне allowlist, детерминированно сортирует и атомарно
  обрабатывает ошибки внутри allowlist.
- [x] **AC-14.** Sentinel `EntryPoint.load()` не вызывается discovery;
  execution descriptor без sandbox возвращает `SECURITY_SANDBOX_REQUIRED`.
- [x] **AC-15.** Empty/all-unsupported/all-unavailable/mixed-invalid selection cases имеют
    exact outcomes из terminal failure table; unexpected errors и cancellation
    не превращаются в silent fallback.
- [x] **AC-16.** Все M3 errors имеют stable code, санитизированный context и не
  раскрывают malicious metadata/secret canaries в `str`, `repr`, traceback или
  details.
- [x] **AC-17.** Root lazy exports и fail-loud facade operations M1/M2 остаются
  совместимы; новый API экспортируется только через `structuraguard.parsers` и
  свойство facade.
- [x] **AC-18.** В production package нет real format parser, semantic analyzer, plugin code
    loader, network/autoinstall logic или новой runtime dependency.

## Шаги реализации

### 1. Зафиксировать probe evidence, descriptors и error vocabulary

- Тесты сначала:
  `packages/structuraguard/tests/unit/contracts/test_m03_parser_contracts.py` и
  дополнение `test_m02_contracts.py` exact-value проверками новых enums/codes,
  backward JSON round-trip `ProbeResult` и negative signal/descriptor payloads.
- Файлы:
  `packages/structuraguard/src/structuraguard/contracts/source.py`, новый
  `packages/structuraguard/src/structuraguard/contracts/plugins.py`,
  `packages/structuraguard/src/structuraguard/contracts/common.py`,
  `packages/structuraguard/src/structuraguard/contracts/__init__.py` и
  `packages/structuraguard/src/structuraguard/exceptions.py`.
- Поведение: frozen `ProbeSignal`/`ParserPluginDescriptor`, additive evidence в
  `ProbeResult`, canonical `format_id`/descriptor fingerprint и stable error
  codes. M2 JSON без evidence round-trips; `Parser` signature не меняется.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/contracts/test_m03_parser_contracts.py
  packages/structuraguard/tests/unit/contracts/test_m02_contracts.py`.

### 2. Добавить reusable FakeParser и instance-local registry

- Тесты сначала: новые
  `packages/structuraguard/tests/fakes/parsers.py` и
  `packages/structuraguard/tests/unit/parsers/test_registry.py` покрывают exact
  identity validation, два независимых registry, immutable snapshots, canonical
  order/fingerprint, single/bulk atomicity, duplicate/canonical-ID matrix,
  signed-32-bit priority boundaries, concurrent same-ID registration и session
  lease lifecycle при normal exit, exception и cancellation.
- Файлы: новые
  `packages/structuraguard/src/structuraguard/parsers/__init__.py`,
  `packages/structuraguard/src/structuraguard/parsers/registry.py` и указанные
  test fake/fixtures.
- Поведение: `register(parser)` принимает только trusted preconstructed object;
  copy-on-write под per-instance lock не держит lock во время user code/await;
  snapshot identity не перечитывает mutable properties и не зависит от insertion
  order. Async session владеет lease и закрывает iterators до release.
  Module-level mutable state отсутствует.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/parsers/test_registry.py`.

### 3. Реализовать deterministic selection и output validation

- Тесты сначала: новые
  `packages/structuraguard/tests/unit/parsers/test_selection.py`,
  `packages/structuraguard/tests/unit/parsers/test_parser_output_contract.py` и
  static negative fixtures в
  `packages/structuraguard/tests/unit/ports/test_m03_parser_static.py`.
  Matrix включает score/priority/ID ties, all signal conflicts, unavailable
  dependency, все terminal failure outcomes, forged result, wrong callable/
  awaitable/iterator shapes, probe exception, cancellation, valid multi-batch,
  wrong DTO/lineage/manifest/order, missing terminal, provisional-before-terminal
  semantics, early abandon и resource limit.
- Файлы: новые
  `packages/structuraguard/src/structuraguard/parsers/selection.py` и
  `packages/structuraguard/src/structuraguard/parsers/execution.py`;
  `packages/structuraguard/src/structuraguard/ports/parser.py` меняется только в
  документации signal/output obligations, без signature change.
- Поведение: selection всегда читает snapshot, применяет hard gates и полный
  exact content vector/format/ranking algorithm; selected handle пропускает
  downstream только locally valid provisional physical batches и сообщает
  aggregate success лишь после valid terminal manifest.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/parsers/test_selection.py
  packages/structuraguard/tests/unit/parsers/test_parser_output_contract.py
  packages/structuraguard/tests/unit/ports/test_m03_parser_static.py`;
  `uv run --locked --no-sync mypy packages/structuraguard/src
  packages/structuraguard/tests/unit/ports/test_m03_parser_static.py`.

### 4. Добавить explicit descriptor-only entry-point discovery

- Тесты сначала: новые
  `packages/structuraguard/tests/unit/parsers/test_discovery.py` и
  `packages/structuraguard/tests/unit/parsers/test_discovery_security.py`
  используют synthetic
  `.dist-info`/entry points и load/import sentinels. Они покрывают exact group,
  allowlisted per-distribution lookup, accepted-entry hard cap, deterministic
  order, ignored hostile non-allowlisted distribution, malicious allowlisted
  metadata corpus, collisions, enumeration failure, atomic rollback, metadata
  fingerprint stability и отсутствие host-side activation. Security regressions
  подтверждают byte caps, bounded чтение metadata header, запрет symlink и
  fail-closed отказ от custom metadata providers без обращения к их getters.
- Файлы: новый
  `packages/structuraguard/src/structuraguard/parsers/discovery.py`, дополнение
  `packages/structuraguard/src/structuraguard/parsers/registry.py`; production
  dependency не добавляется.
- Поведение: explicit call создаёт только validated descriptors. Без
  `SandboxParserRunner` любая activation fail closed с
  `SECURITY_SANDBOX_REQUIRED`.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/parsers/test_discovery.py
  packages/structuraguard/tests/unit/parsers/test_discovery_security.py`.

### 5. Подключить registry к facade и закрепить import safety

- Тесты сначала: дополнить
  `packages/structuraguard/tests/unit/test_facades.py`,
  `packages/structuraguard/tests/smoke/test_import_side_effects.py` и
  `packages/structuraguard/tests/smoke/import_probe.py`.
  Отдельные sentinels запрещают lookup `distributions`/`distribution`,
  `EntryPoint.load`, plugin import и constructor при import/construction;
  sync/async facade ownership проверяется отдельно.
- Файлы: `packages/structuraguard/src/structuraguard/sdk.py` и
  `packages/structuraguard/src/structuraguard/sync_sdk.py`; `SDKConfig` не
  получает environment-driven discovery flag, root `__all__` не расширяется.
- Поведение: `sdk.parsers.register(...)` доступен сразу, но construction не
  выполняет I/O. Явно injected registry сохраняется по identity.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/unit/test_facades.py
  packages/structuraguard/tests/smoke/test_import_side_effects.py`.

### 6. Обновить boundaries, package и публичную документацию

- Тесты сначала: расширить
  `packages/structuraguard/tests/smoke/test_m02_layer_boundaries.py`,
  `packages/structuraguard/tests/smoke/test_dependency_boundary.py`,
  `packages/structuraguard/tests/unit/test_public_exports.py`,
  `packages/structuraguard/tests/packaging/test_metadata.py`,
  `packages/structuraguard/tests/packaging/test_distribution_verifier.py` и новый
  `packages/structuraguard/tests/docs/test_m03_architecture_contract.py`, чтобы
  `parsers` зависел только от stdlib, contracts, ports и typed errors, а
  wheel/sdist содержали все новые modules без изменения root exports.
- Файлы: `scripts/verify_distribution.py`,
  `packages/structuraguard/pyproject.toml`, `uv.lock`, `docs/index.md`,
  `docs/architecture.md`, `docs/public-api.md`, `docs/threat-model.md`,
  `docs/codex/PROJECT_STATE.md` и `mkdocs.yml`.
  `uv.lock` обновляется только из-за синхронного повышения package/verifier/
  packaging-test version до `0.3.0`; runtime dependency graph не меняется.
- Поведение: документация явно различает trusted manual object и untrusted
  descriptor, фиксирует selection/conflict policy и границу
  `Parser → ExtractedBatch`; старой схемы parser → business entity/MappingPlan
  нет.
- Проверка: `uv run --locked --no-sync pytest
  packages/structuraguard/tests/smoke packages/structuraguard/tests/packaging
  packages/structuraguard/tests/docs`; `make docs`; `make test-build`.

### 7. Выполнить review и итоговые gates

- Review: correctness ranking/atomicity, API compatibility, metadata grammar,
  error redaction, import side effects, descriptor/activation boundary,
  cancellation и отсутствие semantic/DB authority.
- Security regressions: hostile metadata не загружается и не отражается в
  errors; descriptor без sandbox не исполняется; forged parser output не
  покидает boundary.
- Проверка: `git diff --check`, `make lint`, `make typecheck`, `make test`,
  `make docs`, `make test-build`; итоговый target — `make check`.

Текущий Makefile не содержит `make test-integration` и `make test-security`.
M3 не создаёт фиктивные targets: discovery security regressions входят в
`make test`, а external sandbox integration появится вместе с runner.

## Checklist перед ручным commit и Pull Request

- [x] `make sync` — lock разрешён; синхронизированы dev/docs groups, проверены
  97 packages и проаудированы 47 установленных packages.
- [x] `make check` — прошли lock check, Ruff для 73 файлов, strict mypy для 71
  source files, `830` tests, MkDocs strict и package build verification.
- [x] Wheel и sdist версии `0.3.0` собраны offline; wheel повторно собран из
  sdist, установлен без editable/source-checkout leakage и прошёл isolated
  black-box/attribution import probes.
- [x] `git diff --check` — whitespace errors отсутствуют.
- [x] `git rev-list --left-right --count main...HEAD` — `0 0`; milestone пока
  существует только в рабочем дереве ветки `feat/m03-parser-registry`.
- [x] Secret scan — production/docs не содержат credential-like значений;
  credential canaries обнаружены только в regression tests и явно проверяют
  redaction.
- [x] Debug/artifact audit — debug calls, временные и случайные untracked files
  не найдены. `dist/`, `site/`, `__pycache__/` и bytecode созданы проверками и
  исключены через `.gitignore`.
- [x] `docs/codex/PROJECT_STATE.md` соответствует версии `0.3.0`, ветке,
  реализованному scope, ограничениям и свежим результатам gates.
- [x] Финальный correctness/security review — существенных in-scope findings
  не осталось.

Не выполнялись:

- Remote CI — требует ручных commit, push и Pull Request, которые этой задачей
  прямо запрещено создавать.
- `make test-integration` и `make test-security` — targets отсутствуют в
  текущем Makefile. Применимые M3 security regressions входят в `make test`;
  external sandbox integration относится к M12.
- Проверка на отдельном Windows runner — локально доступен только macOS.
  Fail-closed ветка платформы без безопасного `dir_fd` покрыта unit test, а
  независимое platform confirmation остаётся задачей Remote CI.
- Внешние `gitleaks`, `detect-secrets`, `trufflehog`, `bandit` и `pip-audit`
  не входят в toolchain проекта и локально не установлены. Вместо них выполнены
  repository-wide credential-pattern scan, review dependency delta и все
  настроенные security regressions; новых production dependencies нет.

## Риски

- `confidence` и signals формирует adapter. Schema validation делает решение
  воспроизводимым, но не доказывает истинность content claim; untrusted code
  поэтому нельзя запускать вне sandbox.
- Entry-point enumeration безопаснее activation, но metadata может быть
  подменена между discovery и будущим run. Descriptor fingerprint и повторная
  pin verification обязательны; artifact signature/digest enforcement остаётся
  responsibility sandbox runtime.
- Добавление optional `format_id`/signals в public `ProbeResult` сохраняет M2
  JSON validation и round-trip. Совместимость DTO не означает eligibility:
  legacy result без strong evidence новый registry отклоняет как
  `PARSER_PROBE_INVALID`, поскольку прежнего selection API не существовало.
- Lexicographic ID tie-break детерминирован, но не выражает качество parser;
  качество задают content evidence, score и явный priority.
- Object registration исполняется с полномочиями host, потому что import и
  constructor уже произошли вне SDK. Это осознанная trust declaration
  composition owner, а не security sandbox.
- Если trusted manual parser не способен завершить собственный `aclose()`,
  wrapper остаётся quarantined, а ошибка не скрывается. Повторный
  `stream.aclose()` может завершить cleanup; постоянно сломанный adapter может
  удерживать внешний ресурс, но не может продолжить чтение через wrapper.
- Базовый `AsyncIterator` не обязан предоставлять `aclose()`. Если trusted
  adapter возвращает custom iterator, владеющий внешним ресурсом без close-hook,
  SDK может перевести wrapper в quarantined state и освободить registry lease,
  но не может принудительно освободить этот ресурс. Обязательный close-capable
  protocol потребует отдельного изменения публичного parser contract.
- Named lookup всё ещё выполняется host metadata finder и может перечислять
  содержимое configured `sys.path` roots. Finder считается частью доверенной
  host composition; filesystem metadata после lookup читаются собственным
  bounded FD-reader. Если metadata finder также недоверен, его CPU/memory
  изоляция переносится в отдельный runtime.
- До M12 discovered untrusted plugins намеренно не исполняются. Это безопасное
  неполное состояние, а не повод добавлять in-process fallback в M3.

[spec-fr-003]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#fr-003-реестр-парсеров
[spec-m3]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m3-parser-registry
