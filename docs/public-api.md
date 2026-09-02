# Публичный API StructuraGuard

Статус: нормативный design M0. Документ фиксирует ожидаемый contract; реализация
появляется в последующих milestone.

StructuraGuard — встраиваемая Python-библиотека. Она не запускает web-service и
не требует FastAPI, Django, Celery, Redis или UI. Нормативные границы описаны в
[требованиях](requirements.md), lifecycle — в
[архитектуре](architecture.md), security contract — в
[модели угроз](threat-model.md).

## Минимальный сценарий

Основной API асинхронный. Пример использует deterministic mapping и не требует
LLM:

```python
from pathlib import Path

from pydantic import SecretStr

from structuraguard import AsyncStructuraGuard
from structuraguard.db import SQLAlchemyTarget
from structuraguard.parsers import SandboxParserRunner
from structuraguard.security import SourcePathPolicy


imports_root = Path("imports").resolve()
target = SQLAlchemyTarget(
    target_id="orders-primary",
    inspection_url=SecretStr(
        "postgresql+asyncpg://schema_inspector:<password>@db/app"
    ),
    writer_url=SecretStr(
        "postgresql+asyncpg://data_importer:<password>@db/app"
    ),
    include_schemas={"public"},
    include_tables={"customers", "orders"},
    staging_schema="structuraguard_staging",
)
sdk = AsyncStructuraGuard(
    source_path_policy=SourcePathPolicy(allowed_roots={imports_root}),
    parser_runner=SandboxParserRunner(),
)
sdk.database_targets.register(target)

result = await sdk.ingest(
    source=imports_root / "orders.xlsx",
    target=target,
    safety_policy="auto_safe",
    error_policy="atomic",
    dry_run=True,
)
```

Этот design-пример станет исполнимым после реализации соответствующих public
DTO и adapters. Placeholder `<password>` нельзя заменять secret literal в
исходном коде приложения.

`SandboxParserRunner` обязателен здесь, потому что source считается untrusted
по умолчанию. Только trusted composition owner может до run разрешить
`InProcessParserRunner` для явно trusted development input/adapter; per-run
caller не может повысить trust и отсутствие sandbox даёт
`SECURITY_SANDBOX_REQUIRED`.

`SQLAlchemyTarget` является immutable policy trusted composition owner.
`include_schemas`/`include_tables` задают максимальный allowlist, а
instance-level registration выполняется до runs. Per-run caller может только
выбрать зарегистрированный target или сузить scope. `target_identity` включает
`target_policy_fingerprint`; adapter проверяет dialect, database/cluster
identity и policy на обеих sessions. Совпадение имён schemas само по себе
недостаточно. Переданный напрямую незарегистрированный target отклоняется.

## Canonical async flow

Пошаговый flow является каноническим contract:

```python
source_analysis = await sdk.inspect_source(imports_root / "orders.xlsx")
try:
    database_catalog = await sdk.inspect_database(target)

    plan = await sdk.create_plan(
        source=source_analysis,
        database=database_catalog,
    )

    validation = await sdk.validate_plan(
        source=source_analysis,
        database=database_catalog,
        plan=plan,
    )

    if validation.can_execute:
        result = await sdk.execute(
            source=source_analysis,
            target=target,
            plan=plan,
            error_policy="atomic",
        )
finally:
    await source_analysis.aclose()
```

Методы имеют следующие обязанности:

- `inspect_source(source)` определяет формат, безопасно разбирает источник,
  строит профиль, provenance и `source_fingerprint`;
- `inspect_database(target)` использует только inspection connection и
  возвращает `DatabaseCatalog` с `database_fingerprint`;
- `create_plan(source, database)` строит декларативный `MappingPlan` без SQL;
- `validate_plan(source, database, plan)` независимо проверяет targets, типы,
  allowlist/denylist, confidence, fingerprints и запрещённые операции;
- `execute(source, target, plan, error_policy)` повторно сверяет fingerprints,
  target identity/policy, выполняет validation, staging и разрешённую
  transactional load;
- `analyze(source, target)` является read-only orchestration над анализом
  источника, БД и candidates;
- `ingest(...)` является convenience orchestration того же pipeline и не меняет
  правила validation или security;
- `propose_schema(source)` возвращает только декларативный `SchemaProposal` и
  никогда не применяет DDL.

Convenience methods не имеют дополнительных полномочий по сравнению с
каноническим flow. Это решение зафиксировано в
[ADR 0001](adr/0001-public-api-and-run-policies.md).

## SourceInput

Поддерживаются:

- filesystem path как `str` или `os.PathLike[str]`;
- raw text через явный `TextSource`, без эвристики «path или content»;
- `bytes`, `bytearray`, `BinaryIO` и `TextIO`;
- `dict`, `list` и разобранный JSON body;
- `Iterable[dict]` и `AsyncIterable[dict]`.

Обычный `str` трактуется как path. Raw text необходимо оборачивать в
`TextSource`; это исключает неоднозначность «path или content». Path принимается
только при заданной trusted composition owner immutable `SourcePathPolicy`.
Per-run caller не может расширить `allowed_roots`; без policy path отклоняется,
и host может передать уже авторизованный открытый stream.

Path open использует безопасный descriptor-first flow: SDK не следует symlink,
проверяет containment, regular-file type, identity и limits после open и читает
и fingerprint-ит тот же handle. Directory, device, FIFO, socket и symlink
отклоняются с safe details.

One-shot stream или iterator потребляется не более одного раза. `inspect_source`
возвращает async-closeable `SourceAnalysis`: immutable analysis view и
resource-bearing bounded snapshot lease, связанный с `source_fingerprint`.
Lease остаётся действительным между staged calls до `aclose()`, выхода из async
context manager или настроенного expiry. Metadata анализа остаются читаемыми
после close, но `execute` требует live lease. SDK:

- закрывает только открытые им ресурсы и собственные temporary files;
- не закрывает stream, переданный caller;
- удаляет snapshot при `SourceAnalysis.aclose()`, expiry, cancellation или
  timeout;
- для combined `ingest` владеет lease сам и закрывает его при любом outcome;
- при reuse сохранённого plan требует новое `inspect_source` исходника и
  повторно сверяет fingerprint.

Закрытый или истёкший lease даёт `SOURCE_SNAPSHOT_EXPIRED` без staging/load.
Caller должен использовать `try/finally` как выше либо async context manager.

Точные public DTO и limits snapshot относятся к M2 и parser milestone, но эти
правила ownership являются нормативными.

## MappingPlan

`MappingPlan` декларативен и не содержит SQL. Минимальные свойства contract:

- `plan_id`, `version` и `plan_fingerprint`;
- `source_fingerprint` и `database_fingerprint`;
- `target_identity` и `target_policy_fingerprint`;
- mappings сущностей, полей и relations;
- разрешённые transformations и load operations;
- confidence, warnings и unmapped fields;
- metadata алгоритма, parser и LLM, если она использовалась.

Plan immutable после создания. Редактирование создаёт новую версию и новый
`plan_fingerprint`. `execute` отклоняет plan при schema drift, source mismatch,
неизвестном target, SQL content, DDL или другой запрещённой операции.

## Независимые политики запуска

Один параметр `mode` не используется одновременно для разных смыслов.

### SafetyPolicy

MVP определяет `auto_safe`. Следующие run-level gates всегда должны пройти:

- format и parser определены;
- plan независимо валиден;
- source и database fingerprints актуальны;
- confidence достаточен, conflicts отсутствуют;
- inspection и writer подтверждают один `target_identity`;
- targets входят в allowlist, не входят в denylist и не требуют DDL;
- критические security events отсутствуют;
- обязательный transactional audit/outbox доступен.

Если условие не выполнено, результат получает `NEEDS_REVIEW` либо
`REJECTED_SECURITY` или typed `FAILED`; основные таблицы не изменяются.
`error_policy` не может понизить plan, dataset-structural, security, identity,
transaction или infrastructure failure до предупреждения.

### LoadErrorPolicy

- `atomic` — default. Любая record-level или критическая ошибка исключает commit
  либо приводит к полному rollback run;
- `quarantine_invalid` — после run-level gates валидные записи могут быть
  загружены, а record-local ошибки values/types/rules/constraints остаются в
  изолированном quarantine с issues и provenance;
- `best_effort` — допускается только при явном выборе и пропускает лишь явно
  классифицированные recoverable record-level failures. Все пропуски и
  частичные результаты отражаются в `LoadReport`; system/security failure не
  игнорируется.

Выбор `best_effort` является достаточным явным opt-in. Он не отключает
allowlist, DDL prohibition, plan validation или security policy.
Частичный commit завершается `COMPLETED_WITH_WARNINGS`.

### Dry run

`dry_run=True` ортогонален двум политикам. Выполняются parsing, mapping,
validation и проверка предполагаемых DB operations. Допустимы in-memory либо
rollback-only staging, но после возврата результата:

- ни main tables, ни persistent staging не изменены;
- temporary resources очищены;
- `LoadReport` описывает предполагаемые, а не committed operations;
- redacted audit/events могут сохраняться как control-plane evidence и отличают
  dry run от выполненной загрузки.

## Side effects

- `import structuraguard` не читает env, не обращается к сети или БД, не меняет
  logging и не создаёт threads, event loop или таблицы;
- `inspect_source` читает только переданный source и собственные bounded temp;
- `inspect_database` выполняет read-only metadata inspection;
- `analyze`, `create_plan`, `validate_plan` и `propose_schema` не записывают БД;
- `ingest(dry_run=True)` не оставляет persistent data-plane DB mutations;
- `execute` и `ingest(dry_run=False)` могут писать только allowlisted staging,
  target и pre-provisioned transactional audit/outbox tables через writer
  connection; denylisted targets запрещены;
- audit/event listeners получают redacted metadata, но не restricted raw values
  или credentials.

До реальной загрузки adapter проверяет на writer session `target_identity`,
`target_policy_fingerprint`, per-run narrowing и применимый catalog
fingerprint. Durable core audit/outbox record участвует в той же transaction,
что и target mutations; без этой capability `LOADING` не начинается. Optional
external listeners вызываются после commit. Их сбой не отменяет commit: durable
outbox сохраняет retry evidence, а result может быть
`COMPLETED_WITH_WARNINGS`.

## Роль LLM

LLM optional. Без provider SDK выполняет deterministic mapping. При разрешённом
вызове LLM может только ранжировать или выбирать из переданных mapping
candidates и вернуть schema-bound proposal, ссылающийся на них.

LLM не может:

- получить DB connection, credentials, tools или arbitrary target list;
- выполнить код, shell command, SQL или DDL;
- сформировать SQL для прямого исполнения;
- обойти allowlist, privacy policy или `validate_plan`;
- стать источником истины для решения о загрузке.

Любой LLM output считается недоверенным, проходит strict schema validation и
используется только как input для deterministic checks. Privacy routing и
parser isolation зафиксированы в
[ADR 0002](adr/0002-security-boundary-defaults.md).

## IngestResult

Каждый завершённый top-level run возвращает типизированный `IngestResult` с:

- `run_id` и pipeline `status`;
- `SourceReport` и `DatabaseReport`;
- версией и fingerprint `MappingPlan`;
- `ValidationReport` и optional `LoadReport`;
- `SecurityReport` и redacted `AuditEvent` list;
- metadata воспроизводимости: версии SDK/parser/алгоритма/prompt,
  provider/model, generation parameters, transformations и fingerprints.

`LoadReport` различает proposed и committed counts. Result не содержит DSN,
tokens, authorization headers, private keys или restricted raw values.

## Состояния pipeline

Рабочие состояния:

```text
CREATED
SOURCE_PROBING
SOURCE_PARSING
SOURCE_PROFILING
DATABASE_INSPECTING
MAPPING
PLAN_VALIDATING
NORMALIZING
VALIDATING
STAGING
LOADING
```

Терминальные состояния:

```text
COMPLETED
COMPLETED_WITH_WARNINGS
NEEDS_REVIEW
REJECTED_SECURITY
ROLLED_BACK
FAILED
CANCELLED
```

Переходы монотонны и проверяются state machine. Основной happy path следует
порядку списка рабочих состояний. Дополнительно:

- policy/validation ambiguity переводит run в `NEEDS_REVIEW` до `LOADING`;
- критическое security событие переводит run в `REJECTED_SECURITY`;
- критическая ошибка после начала transactional load приводит к rollback и
  `ROLLED_BACK`;
- если rollback подтверждён, но обязательный terminal audit event не сохранён,
  run получает `FAILED`; `LoadReport` сохраняет outcome `rolled_back`, а
  `SecurityReport` — audit gap;
- ошибка до начала load приводит к `FAILED` без ложного утверждения о rollback;
- до commit configured deadline приводит к `FAILED`/`PROCESSING_TIMEOUT` после
  rollback/cleanup, а caller cancellation — к `CANCELLED`;
- после подтверждённого commit timeout/cancellation не меняет data outcome и
  даёт `COMPLETED_WITH_WARNINGS` с post-commit delivery/cleanup details;
- если commit или rollback нельзя подтвердить, run получает `FAILED`, а report
  явно отмечает неизвестный transaction outcome; `COMPLETED` и `ROLLED_BACK`
  запрещены;
- pre-commit cancellation закрывает streams, выполняет нужный rollback/cleanup
  и завершает run как `CANCELLED`;
- dry run не входит в `LOADING` и завершается `COMPLETED` либо
  `COMPLETED_WITH_WARNINGS` после rollback-only staging/validation;
- из терминального состояния переходов нет; invalid transition отклоняется с
  `PIPELINE_INVALID_STATE_TRANSITION`.

`NEEDS_REVIEW` не возобновляется в том же run. После изменения/подтверждения plan
создаётся новый run с новой audit chain entry.

## Ошибки

Expected policy outcomes отражаются статусом и reports. Невалидный вызов,
невозможность выполнить этап или infrastructure failure возвращают typed error:

```text
StructuraGuardError
SourceError
ParserError
DatabaseInspectionError
MappingError
ValidationError
SecurityPolicyError
LoadError
```

Каждая ошибка содержит `code`, безопасный `message`, redacted `details`,
`run_id`, `retryable` и sanitized `cause`. Минимальные стабильные коды M0:

- `PARSER_NO_TEXT_LAYER` — PDF не содержит текстового слоя;
- `SECURITY_SANDBOX_REQUIRED` — strict run не может безопасно запустить parser;
- `PIPELINE_INVALID_STATE_TRANSITION` — запрещённый переход состояния;
- `SYNC_API_IN_ASYNC_CONTEXT` — sync facade вызван в активном event loop;
- `SOURCE_FINGERPRINT_MISMATCH` и `DATABASE_FINGERPRINT_MISMATCH` — drift;
- `SOURCE_PATH_NOT_ALLOWED` — path policy отсутствует или источник небезопасен;
- `SOURCE_SNAPSHOT_EXPIRED` — snapshot lease закрыт или истёк;
- `DATABASE_TARGET_MISMATCH` — inspection и writer относятся к разным targets;
- `AUDIT_DURABILITY_REQUIRED` — отсутствует атомарный audit/outbox capability;
- `PROCESSING_TIMEOUT` — истёк configured deadline;
- `DDL_FORBIDDEN` — запрошена DDL operation;
- `TARGET_NOT_ALLOWED` — target не зарегистрирован, расширяет maximum allowlist
  либо входит в denylist.

Новые machine-readable codes добавляются без переиспользования старого смысла.

## Sync facade

`StructuraGuard` зеркалирует public operations `AsyncStructuraGuard`, но не
создаёт скрытый thread или nested event loop. Вызов из активного event loop
завершается `SYNC_API_IN_ASYNC_CONTEXT`; caller должен использовать async API.

Полученный через sync facade `SourceAnalysis` поддерживает context manager и
`close()` вместо `aclose()`; lease и expiry semantics остаются теми же.

Cancellation, timeout, rollback и result semantics совпадают с async contract.

## Extension points

Регистрация выполняется на конкретном SDK instance до начала run. Object-based
API ниже предназначен только для trusted executable code, уже загруженного
composition owner в host process:

```python
sdk.parsers.register(MyCorporateXMLParser())
sdk.mapping.aliases.register(
    source_alias="Контрагент",
    targets=["customers.legal_name"],
)
sdk.validation.rules.register(MyDomainRule())
```

Registries не глобальны. Начавшийся run использует immutable snapshot registry;
параллельная регистрация не меняет его поведение. Версии parser, aliases и rules
попадают в metadata воспроизводимости.

Для untrusted parser используется декларативный sandbox descriptor, а не
imported object. Artifact/module resolution, import и constructor выполняются
внутри `SandboxParserRunner`; host-side import до sandbox запрещён. Output в
обоих случаях остаётся недоверенным и повторно валидируется core. Executable
normalizer, validator, business rule, listener, DB или LLM adapter, переданный
как object, также является trusted host code. Untrusted executable extensions
без отдельного sandbox protocol не входят в MVP и отклоняются. Aliases,
declarative rules и templates являются неисполняемыми данными.

## Совместимость

- public API следует Semantic Versioning;
- minor release сохраняет backward compatibility;
- удаление public API предваряется deprecation period;
- изменение machine-readable code или semantics существующего policy является
  breaking change;
- точные DTO утверждаются в M2 без ослабления contract M0.

## Out of scope

Public API MVP не включает:

- web endpoints, background workers или UI;
- применение DDL и migration administration;
- OCR, media processing и выполнение document content;
- встроенное долговременное хранилище audit keys или masking map;
- неявную отправку source content внешней LLM;
- LLM classification, explanation, normalization или schema proposal за
  пределами выбора из переданного candidate set;
- sandbox execution произвольных типов executable extensions кроме parser;
- гарантию production support для SQLite или произвольной СУБД.
