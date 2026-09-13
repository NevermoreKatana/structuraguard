# PostgreSQL staging M13-A

Staging сохраняет metadata run/batch/record и ссылки на immutable artifacts до
загрузки в целевые таблицы. Scalar payload из NormalizedBatch в PostgreSQL не
копируется. Нужен установленный extra `structuraguard[postgres]`.

## Подготовка и чтение run

Пример использует готовые `writer_target`, `spec` и normalized batches от
trusted composition приложения. `spec.references` указывает на уже сохранённые
артефакты; владельцу необходимо удерживать их до указанного `retained_until`.

<!-- example:m13-staging:start -->
```python
from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.staging import (
    StagingRetentionPolicy,
    StagingRun,
    StagingRunSpec,
)
from structuraguard.stores import PostgreSQLStagingStore, PostgreSQLStagingTarget


async def stage_dataset(
    target: PostgreSQLStagingTarget,
    spec: StagingRunSpec,
    batches: tuple[NormalizedBatch, ...],
) -> StagingRun:
    store = PostgreSQLStagingStore(
        target,
        retention=StagingRetentionPolicy(),
    )
    await store.begin(spec)
    for batch in batches:
        await store.stage(batch, spec.context)
    run = await store.get_run(spec.context)
    return await store.seal(spec.context, expected_revision=run.revision)
```
<!-- example:m13-staging:end -->

Для потока caller вызывает seal после EOF и закрытия owned iterator. Сам store
принимает отдельные batches, проверяет terminal manifest и counts, но не читает
источник заново и не доказывает EOF внешнего iterator. Seal не заменяет M11/M12 и
не разрешает target write.

`RunStagingStore` расширяет существующий `StagingStore.stage`. Методы:

| Метод | Контракт |
|---|---|
| `begin(spec)` | Явное создание run; одинаковый spec возвращает прежнюю metadata. Другой spec для того же run запрещён. |
| `stage(batch, context)` | Атомарная запись summary batch и record index, без scalar payload. Требует NormalizedBatch 1.1/1.2 и последовательные ordinals. Повтор того же batch — no-op. |
| `get_run(context)` | Состояние, revision, counts, timestamps, fingerprints и retention refs. |
| `read_batches(context, offset=…, limit=…)` | Bounded страница batch summaries, доступная и при OPEN. |
| `seal(context, expected_revision=…)` | CAS, terminal manifest и полные counts; immutable sealed fingerprint. |
| `read_records(context, offset=…, limit=…)` | Bounded страница record metadata из закрытого для append run. Истёкшие или очищенные refs недоступны. |
| `transition(context, expected_revision=…, status=…)` | Разрешённый переход с CAS; caller отвечает за истинность объявленного outcome. |
| `cleanup(context)` | Maintenance роль применяет retention только к указанному run; возвращает metadata/tombstone. |

Каждый вызов сверяет **весь** StagingContext, включая staging_id, target, policy,
normalized/DB fingerprints, expiry и limits. PostgreSQL key включает trusted
namespace, target_id и run_id. Namespace из конфигурации не является изоляцией
недоверенных tenants с общими SQL credentials: для них нужны отдельные роли/схемы.

## Что сохраняется

`StagingRunSpec` содержит source/extraction/parse/mapping fingerprints, ожидаемые
batch/record counts и context. `StagingRun` добавляет timestamps UTC, revision,
фактические counts, status, sealed fingerprint, cleanup_after и purged flag.

`StagedBatch` хранит NormalizedBatchSummary и timestamp. `StagedRecord` хранит
record_id, глобальный record_index, batch_index/index_in_batch, fingerprint
исходного NormalizedRecord и `status="staged"`. Это индекс входа, а не запись
результатов будущего loader по каждой строке.

Ссылки имеют категории `raw`, `normalized`, `mapping`, `validation`, `provenance`.
Каждая содержит opaque artifact_id, fingerprint артефакта и retained_until.
Категории, заданные `StagingRetentionPolicy.retain_kinds`, обязательны; лишние
ссылки отклоняются **до persistence**, а не молча удаляются. Policy задаёт владелец
store. Для metadata-only staging можно явно выбрать пустой набор категорий;
такой набор недостаточен для воспроизведения или загрузки.

Record references указывают на run artifacts; selection задают record/batch
ordinals и record_id. Нормализованная и mapping ссылки дополнительно связаны с
соответствующими fingerprints spec. Store не удостоверяет подлинность внешнего
artifact, не открывает artifact_id как path/URL и не загружает ValidationReport
для проверки данных. Full JSON refs/metadata чувствителен; repr run/record/ref
скрыт. SQLAlchemy instance loggers закрыты для вывода parameters/result rows;
глобальные logging settings не меняются.

## Bootstrap и роли

Runtime constructor, import, begin, stage и ingest **не создают таблицы**.
Отдельный административный вызов:

<!-- example:m13-staging-bootstrap:start -->
```python
from structuraguard.stores import PostgreSQLStagingTarget
from structuraguard.stores.bootstrap import bootstrap_postgresql_staging


async def install_staging(admin: PostgreSQLStagingTarget) -> None:
    await bootstrap_postgresql_staging(admin)
```
<!-- example:m13-staging-bootstrap:end -->

`admin` задаёт владелец deployment: отдельный DSN, `purpose="bootstrap"`,
согласованные `target_id`, `namespace` и `schema_name`. Runtime получает другой
target с `purpose="writer"` и ограниченными credentials.

Bootstrap создаёт только `structuraguard_staging` либо явно заданную
`sg_staging_*` schema и четыре таблицы: `schema_info`, `runs`, `batches`, `records`.
Существующую совместимую схему повторно проверяет, несовместимую отклоняет.
Вызов не выполняет GRANT, DROP или миграции. Ни store, ни facade его не вызывают.
Политика установки инфраструктуры и credentials остаются у приложения.

Рекомендуемые grants для стандартной схемы выполняет администратор отдельно:

```sql
GRANT USAGE ON SCHEMA structuraguard_staging TO staging_writer, staging_cleaner;
GRANT SELECT ON ALL TABLES IN SCHEMA structuraguard_staging
    TO staging_writer, staging_cleaner;
GRANT INSERT, UPDATE ON structuraguard_staging.runs TO staging_writer;
GRANT INSERT ON structuraguard_staging.batches, structuraguard_staging.records
    TO staging_writer;
GRANT UPDATE ON structuraguard_staging.runs TO staging_cleaner;
GRANT DELETE ON structuraguard_staging.batches, structuraguard_staging.records
    TO staging_cleaner;
```

Обе runtime-роли отличаются от inspector, не являются владельцами объектов,
superuser, CREATEROLE, CREATEDB или BYPASSRLS. Проверяются login/session user,
CREATE на database/non-system schemas, владение любыми DB objects (включая
functions/types), memberships с возможностью SET ROLE даже при NOINHERIT и
привилегированные server-file/program roles. Используется та же проверка роли,
что у PostgreSQL loader. Наличие таких
прав приводит к `STAGING_PRINCIPAL_FORBIDDEN`, а SDK не меняет grants сам.
Writer не имеет DELETE staging; cleanup вызывается экземпляром с отдельным
`purpose="maintenance"`. Maintenance API не разрешает begin/stage/transition.
Никаких прав на production DDL эти примеры не выдают.

## Транзакции, lifecycle и retention

Все операции одного run сериализуются transaction-scoped advisory lock.
Каждый вызов имеет отдельную PostgreSQL transaction/NullPool connection.
Batch summary, все record rows и run counts меняются вместе; late SQL failure
или отмена до commit не оставляет частичного batch. Values передаются binds,
identifiers принадлежат закрытой staging схеме и формируются adapter/compiler.

Runtime проверяет фиксированные columns/constraints, ordered FK pairs, schema
version, RLS, triggers/rules, inheritance, defaults, generated/identity columns
и индексы: допустимы только штатные btree PK/UNIQUE indexes с operator classes
из pg_catalog. Дополнительные indexes, custom operator classes и non-default
column collations отклоняются до чтения данных или DML. Table locks берутся
до проверки: ROW EXCLUSIVE для изменяемых таблиц, ACCESS SHARE для чтения.
Административные миграции staging должны быть согласованы с остановкой imports;
SDK не заявляет защиту от произвольного параллельного изменения схемы её владельцем.

Основной переход: OPEN → SEALED → EXECUTING → COMMITTED/QUARANTINED/ROLLED_BACK/
FAILED/CANCELLED/UNKNOWN. До исполнения допустимы FAILED/CANCELLED. UNKNOWN
разрешается явным CAS только в COMMITTED/QUARANTINED/ROLLED_BACK. Terminal run
не возвращается в OPEN. Эти статусы — metadata caller, **не commit ledger loader**.

По умолчанию refs удерживаются 1 день после COMMITTED и 7 дней после остальных
terminal outcomes. Оба интервала настраиваются; quarantine использует failure
retention. Begin требует retained_until каждой ссылки не раньше
context.expires_at + максимальный интервал retention. Если resolution UNKNOWN
занял больше срока, reference может оказаться недоступной: store явно отклоняет
её чтение, а owner должен отдельно управлять сроком внешнего artifact.

Cleanup не трогает EXECUTING/UNKNOWN. Брошенные OPEN/SEALED после expires_at
получают EXPIRED с failure retention от expires_at, без объявления rollback.
По истечении retention cleanup удаляет batches/records и ссылки в run, оставляет
минимальный tombstone с IDs, fingerprints, counts и status. Tombstone запрещает
повторное использование run_id; его удаление и полный loader idempotency ledger
не входят в этот слой. Внешние artifacts удаляет их владелец, не cleanup.

`StagingLimits` ограничивает число records/batches, размер одного batch, страницу,
bytes/nodes intake и timeout/cleanup budget. Перед dump/hash проверяется размер
входа, persisted JSON читается через bounded server cursor. Размер JSON ограничен
на сервере в байтах (`octet_length`), до передачи UTF-8 значения клиенту.
Persisted fingerprints также ограничены на сервере до проверки содержимого.
Для простоты текущий
adapter проверяет metadata всего ограниченного run при каждом вызове; страница
результата bounded, но стоимость чтения — O(размер run). Это не large-stream backend.

Caller cancellation распространяется как `asyncio.CancelledError` после cleanup.
Потеря ответа COMMIT возвращает `STAGING_OUTCOME_UNKNOWN`: затем нужно прочитать
get_run. Повтор stage не дублирует batch; transition с устаревшей revision
отклоняется. Нет автоматического retry с новым run_id или ложного rollback.
Настоящий [dry-run](dry_run.md) не вызывает staging вообще.
`MemoryStagingStore` подходит для локальных bounded workflows и тестов;
его metadata теряется с экземпляром. PostgreSQL store всегда persistent.

## Ошибки и проверки

Все runtime ошибки — `StagingError`, совместимый с `LoadError`. Основные codes:
`STAGING_INPUT_INVALID`, `STAGING_LIMIT_EXCEEDED`, `STAGING_CONTEXT_MISMATCH`,
`STAGING_RUN_NOT_FOUND`, `STAGING_RUN_EXISTS`, `STAGING_RETENTION_MISMATCH`,
`STAGING_REFERENCE_EXPIRES`, `STAGING_BATCH_ORDER_INVALID`, `STAGING_RECORD_DUPLICATE`,
`STAGING_CONTENT_MISMATCH`, `STAGING_INCOMPLETE`, `STAGING_REVISION_CONFLICT`,
`STAGING_STATE_INVALID`, `STAGING_EXPIRED`, `STAGING_PURGED`,
`STAGING_SCHEMA_UNAVAILABLE`, `STAGING_PRINCIPAL_FORBIDDEN`,
`STAGING_PERMISSION_DENIED`, `STAGING_OUTCOME_UNKNOWN`, `PROCESSING_TIMEOUT`.
Driver messages и parameters не включаются в exception/report.
При непосредственном создании неверного DTO возможен `pydantic.ValidationError`;
его полный текст может содержать входные данные и не предназначен для logs.

Общий contract suite выполняется на MemoryStagingStore и PostgreSQL 16/18.
PostgreSQL suite дополнительно проверяет отсутствие runtime DDL, реальные grants,
rollback, cancellation, concurrent begin/batch/seal, потерю COMMIT ACK,
namespace isolation, schema drift, RLS/rules/triggers и SQL/log canaries.
Команды: `make test`, `make test-security`, `make test-database`.

Архитектурное решение: [ADR 0026](adr/0026-staging-and-loader-transactions.md).
Канонические требования: [ТЗ, §17][spec-stage], [§20.3][spec-users],
[§20.4][spec-ddl] и [M13][spec-m13]. Metadata с внешними references — принятый
вариант хранения; store сам не сохраняет и не удаляет payload artifacts.

[spec-stage]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#17-staging
[spec-users]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#203-разделение-db-users
[spec-ddl]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#204-запрет-ddl
[spec-m13]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m13-staging-and-loader
