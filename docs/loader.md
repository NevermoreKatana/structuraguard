# PostgreSQL loader M13

`PostgreSQLLoader.execute()` загружает проверенный sealed staging run в одной
atomic transaction. `insert_only` вставляет новые строки; `upsert` использует
полный подтверждённый PK, безусловный UNIQUE либо настроенный natural key,
который также подтверждён catalog. Произвольное поле и статистическая уникальность
не дают права выбирать строку для UPDATE.

Нужен extra `structuraguard[postgres]`. Приложение заранее выполняет явные
`begin → stage → seal` через [staging store](staging.md) и удерживает artifacts
по retention policy. Loader сам не создаёт staging schema/tables и не открывает
artifact refs как пути или URLs.

Atomic — режим по умолчанию. Durable idempotency и отдельный
`quarantine_invalid` включаются явно, как показано ниже.

Пример принимает настроенные writer/policy/store и `LoadRequest` с полным
snapshot, context и revision SEALED run. Эти объекты подготавливает приложение:

<!-- example:m13-loader:start -->
```python
from structuraguard.contracts.loading import (
    LoadRequest,
    PostgreSQLLoadPolicy,
    PostgreSQLLoadResult,
)
from structuraguard.database.loader import PostgreSQLLoader
from structuraguard.database.writer_target import PostgreSQLWriterTarget
from structuraguard.ports import RunStagingStore


async def load_staged(
    target: PostgreSQLWriterTarget,
    policy: PostgreSQLLoadPolicy,
    staging: RunStagingStore,
    request: LoadRequest,
) -> PostgreSQLLoadResult:
    loader = PostgreSQLLoader(target, policy=policy, staging=staging)
    return await loader.execute(request)
```
<!-- example:m13-loader:end -->

`PostgreSQLWriterTarget(inspection=..., dsn=..., principal=...)` получает
`PostgreSQLTarget` inspector и отдельный writer DSN типа `SecretStr`.
`PostgreSQLLoadPolicy(preflight=..., write_tables=...)` требует явный список
пар `(schema, table)`, разрешённых владельцем для записи; default mode — atomic.
Ни source, ни MappingPlan не задают credentials или расширение allowlist.

Метод `loader.dry_run(snapshot)` использует только inspector, не открывает writer
и не читает/меняет staging. Его контракт остаётся [read-only dry-run](dry_run.md).
Существующие inspector adapters `DatabaseAdapter.execute` остаются заглушками:
новый writer подключается явно через `PostgreSQLLoader`.

## Какие проверки предшествуют INSERT

- Полный normalized manifest, batch/record fingerprints и точная copy projection
  проверяются заново. EOF не заменяется наличием одного terminal batch.
- Run должен быть SEALED с указанной revision. Проверяются весь StagingContext,
  source/extraction/parse/mapping bindings, counts, artifact references и их lease,
  каждая страница batch/record metadata и вычисленный sealed fingerprint.
  Требуются все пять reference kinds; metadata-only staging не разрешает load.
  Страницы читаются порциями до 100 элементов: store должен разрешать такой размер.
- Через CAS run переводится в EXECUTING. Без ledger повтор этого run отклоняется
  до открытия writer; с ledger writer сначала проверяет committed marker.
- В writer transaction повторяются reflection, schema fingerprint, M11, точная
  projection и M12 DB constraints. Внешний ValidationReport или успешный прошлый
  dry-run не заменяет эти проверки. Business/schema policy coordinator и physical
  replay остаются у приложения; преобразованные values без replay не принимаются.

## Роли, allowlists и транзакция

Writer и inspector — разные login users одного host/port/database. Writer DSN
не может менять endpoint или scope MappingPlan. Фактические current_user и
session_user должны совпадать с настроенным writer. Запрещены superuser,
CREATEROLE, CREATEDB, BYPASSRLS, CREATE на database/schema, членство в owner roles
объектов этой БД (включая functions/types), опасные server-file/program роли и
DELETE/TRUNCATE/TRIGGER на используемых таблицах. SDK не выдаёт GRANT.

`write_tables` — отдельный trusted список, входящий в MappingValidationPolicy.
На нём заранее берутся `SHARE ROW EXCLUSIVE` locks; lookup-only tables получают
`ACCESS SHARE`. Порядок locks стабилен по `(schema, table)`. Writer требует
табличный UPDATE grant для сильного lock даже при insert_only. Lookup-only parent
достаточно SELECT и schema USAGE. Все scopes ограничены inspector target;
зарезервированные staging schemas запрещены как target.

Transaction использует READ COMMITTED, `search_path=pg_catalog`, конечные
statement/lock/общий deadlines и отдельный NullPool connection. Блокировки
удерживаются от reflection до COMMIT, не позволяя конкурентному DML менять
write tables между классификацией и записью. Они намеренно уменьшают параллелизм.
Lookup parent может измениться: окончательная FK-проверка при DML либо допускает
COMMIT, либо откатывает соответствующую группу или весь run согласно mode.
Полный schema fingerprint перечитывается после построения плана непосредственно
перед DML и после записи ledger перед COMMIT. Grants/runtime metadata также
проверяются после DML. Миграции types/functions и других объектов вне table-lock coverage
владелец deployment согласует с импортами отдельно.

SQL создаёт закрытый SQLAlchemy Core builder. Значения всегда bind parameters;
имена берутся из проверенного catalog и ordered plan. Bulk groups разделяются
по таблице и форме присутствующих колонок, а также по `batch_size`,
`max_parameters` и `max_batch_bytes`. Missing column и explicit NULL различаются.
Каждый statement возвращает только константу на записанную строку; число
результатов обязано совпасть с числом units в batch и во всём плане.

## Ключи, FK и порядок

Composite PK/UNIQUE/natural keys сохраняют полный порядок колонок из evidence.
Upsert использует `ON CONFLICT (...) DO UPDATE` и меняет только mapped mutable
columns. Identity, PK и generated columns не входят в UPDATE SET. Другой unique
conflict — ошибка, не выбор другого target row. Nullable/partial/expression/
deferred arbiters не поддержаны. Изменение дополнительных unique/PK columns
блокируется консервативно. Если mutable columns нет, уже существующая identity
даёт skip; новая identity вставляется под удерживаемым lock.

Дубликаты проверяются во всём normalized/staged scope, включая разные source
batches; last-write-wins не применяется. Atomic откатывает весь набор;
quarantine отклоняет целые связанные группы. `insert_only` не использует `DO NOTHING`.

Dependency graph задаёт parent-before-child order после проверки всего набора.
Parent может находиться в более позднем source batch. `source_values` и `lookup`
проверяют exact ordered FK tuple через bounded EXISTS по разрешённому unique key;
lookup не переводит произвольный natural key в generated ID. `mapped_parent`
дополнительно требует конкретный parent entity/record link или split одной entity
и равенство значений обоих концов. Одного co-occurrence недостаточно.
Неизвестная связь, неверная arity, неподтверждённая уникальность или превышение
lookup budget блокируют загрузку. Cycles, generated-key propagation, deferred и
two-phase strategies остаются неподдержанными.

## Generated columns и defaults

Generated columns никогда не включаются в INSERT/UPDATE; mapping на такую колонку
отклоняется M11. Для omitted stored generated/non-key default требуется явное
разрешение владельца:

<!-- example:m13-server-value:start -->
```python
from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.database import CatalogColumnRef, ColumnCatalog
from structuraguard.contracts.loading import ServerValuePermission


def allow_server_value(table_id: str, column: ColumnCatalog) -> ServerValuePermission:
    if column.inspection is None:
        raise ValueError("Требуется metadata из свежего catalog")
    return ServerValuePermission(
        column=CatalogColumnRef(table_id=table_id, column_id=column.column_id),
        metadata_fingerprint=canonical_sha256_value(column.inspection.canonical_json()),
        evaluation_allowed=True,
    )
```
<!-- example:m13-server-value:end -->

Вызовите функцию только для выбранной владельцем non-key колонки из свежего
catalog и передайте результат в `PostgreSQLLoadPolicy.server_values`. Автоматически
разрешать все defaults/generated columns этим примером нельзя.

Hash подтверждает совпадение metadata, а безопасность и допустимость вычисления
подтверждает владелец policy. Разрешение не поступает из source или MappingPlan.
SQL expression из metadata не передаётся на исполнение: вычисление выполняет БД
при обычном INSERT/UPDATE существующей таблицы. Ключевые columns (PK/UNIQUE/FK),
identity, virtual generated и domain defaults этим механизмом не разрешаются.
Разрешённый non-key default может вызвать sequence: её счётчик PostgreSQL не
откатывает при rollback. Владелец policy должен явно допускать такой эффект.
Без permission такие значения блокируются. Dry-run остаётся консервативным и не
вычисляет даже разрешённые writer defaults. CHECK требует отдельного trusted
M12 binding; RLS, user triggers/rules и неподдержанные indexes отвергаются.

## Результат и отказ

Успех — `PostgreSQLLoadResult(transaction_outcome="committed")` с inserted,
updated и skipped **target units**, fingerprints и UTC timestamp начала attempt.
Полный JSON чувствителен; `safe_summary()` содержит только counts и признаки.
SQL, parameters, credentials, raw errors и source values в отчёт/logs не включаются.

До COMMIT ошибка откатывает все target writes. После подтверждённого staging CAS
loader завершает run с ROLLED_BACK либо CANCELLED, сохраняя refs. Отказ до CAS
оставляет прежний run без изменений; сбои самого CAS требуют сверки состояния.
Отмена до начала COMMIT распространяется как
CancelledError после bounded cleanup. Если ответ COMMIT потерян или исход нельзя
подтвердить, возвращается `LOAD_OUTCOME_UNKNOWN`, run остаётся UNKNOWN. С ledger
повтор того же key проверяет committed marker; без ledger повторное исполнение
экземпляром запрещено. Автоматического retry нет.

После подтверждённого COMMIT сбой cleanup/finalize не превращается в rollback:
результат сохраняет committed counts и warning. Staging и target не имеют общей
transaction; при `LOAD_STAGING_FINALIZE_FAILED` staging может остаться EXECUTING.
Отмена во время staging CAS может также оставить EXECUTING до первого target DML.
Ledger replay восстанавливает исходный result и завершает его EXECUTING/UNKNOWN
staging, если сохранённый seal совпадает. Отсутствие marker не разрешает писать
из EXECUTING/UNKNOWN staging. После известного rollback нужен свежий sealed run
с тем же key и binding. Без ledger reconciliation остаётся у приложения.

Ошибки runtime возвращаются как `LoadError` с закрытым `error_code`;
конструирование некорректных DTO может дать `pydantic.ValidationError`.
Не журналируйте полный validation error с входными данными.

| Code / результат | Значение для приложения |
|---|---|
| `LOAD_IDEMPOTENCY_REQUIRED` | Key и ledger должны быть заданы вместе. |
| `LOAD_STAGING_BINDING_MISMATCH` | Metadata не подтверждает переданный snapshot/revision. |
| `DATABASE_SCHEMA_DRIFT` | Схема изменилась; нужен новый catalog и проверенный plan. |
| `LOAD_DUPLICATE_KEY`, `LOAD_FOREIGN_KEY`, `LOAD_VALIDATION_REJECTED` | Atomic load отклонён; staging сохраняется по retention. |
| `IDEMPOTENCY_KEY_CONFLICT` | Key уже связан с другим source/plan/policy. |
| `LOAD_OUTCOME_UNKNOWN` | Сверить ledger; отсутствие подтверждения не означает rollback. |
| Committed result с `LOAD_STAGING_FINALIZE_FAILED` | Target уже committed; требуется восстановление staging metadata. |

## Durable idempotency и audit

Ledger находится в **той же PostgreSQL DB** и использует writer connection.
Создание schema — отдельная административная операция. Повтор bootstrap проверяет
версию/структуру; миграции, production DDL и GRANT автоматически не выполняются.

<!-- example:m13-ledger-bootstrap:start -->
```python
from structuraguard.database.ledger import bootstrap_postgresql_loader
from structuraguard.stores import PostgreSQLStagingTarget


async def install_ledger(admin: PostgreSQLStagingTarget) -> None:
    await bootstrap_postgresql_loader(admin)
```
<!-- example:m13-ledger-bootstrap:end -->

`admin` — отдельный target с `purpose="bootstrap"`, административным `SecretStr`
DSN и теми же `namespace`/`schema_name`, что в `LoadLedgerPolicy` loader.
Административные credentials в runtime loader не передаются.

Допустимы только `sg_staging_load_*` schemas. Администратор выдаёт runtime writer
USAGE schema, SELECT `schema_info` и SELECT/INSERT для `execution_commits`,
`execution_audit`, `execution_quarantine`. UPDATE, DELETE, TRUNCATE, TRIGGER,
schema CREATE и ownership запрещены, включая UPDATE на отдельных колонках и
INSERT на колонках schema_info. Для настоящей изоляции недоверенных tenants
нужны разные роли/schema: namespace сам по себе не ограничивает SQL privileges.

<!-- example:m13-ledger-policy:start -->
```python
from structuraguard.contracts.loading import (
    DryRunPolicy,
    LoadLedgerPolicy,
    PostgreSQLLoadPolicy,
)


def durable_load_policy(
    preflight: DryRunPolicy,
    write_tables: tuple[tuple[str, str], ...],
    ledger: LoadLedgerPolicy,
) -> PostgreSQLLoadPolicy:
    return PostgreSQLLoadPolicy(
        preflight=preflight,
        write_tables=write_tables,
        ledger=ledger,
    )
```
<!-- example:m13-ledger-policy:end -->

Передайте полученную policy в `load_staged` и задайте непрозрачный
`idempotency_key` в `LoadRequest`. Повторный вызов с тем же key и неизменным
binding возвращает исходный результат с `replayed=True`. При
`preflight.error_policy="quarantine_invalid"` эта конфигурация включает
явный quarantine mode.

Key требуется вместе с ledger policy; key без ledger также отвергается. В БД
сохраняется hash key и scope `(namespace, target_id)`. Binding содержит
source/extraction/parse/normalized/mapping/database fingerprints, effective policy
и версию алгоритма. Source manifest сверяется с plan до writer I/O. Run ID,
время attempt и настройки разбиения SQL batches не меняют binding.
Другой binding с тем же key — `IDEMPOTENCY_KEY_CONFLICT`.

Transaction advisory lock сериализует одинаковый key до staging CAS, в том числе
между процессами и параллельными вызовами одного loader. Ожидание ограничено
lock/общим timeout. Unique marker дополнительно защищает scope. После rollback
первого вызова второй может загрузить свой sealed run; после commit получает
сохранённый результат. Marker/DB state одного SELECT без lock не используются
как доказательство rollback незавершённой transaction.

Replay возвращает исходные `run_id`, counts и fingerprints с `replayed=True`,
а `attempt_run_id` связывает текущий вызов. **Эти counts не означают повторную
загрузку**: `safe_summary()` даёт INSERT/UPDATE=0 для replay. Replay не повторяет
target DML, audit INSERT или validation queries к target tables. Старый schema
fingerprint допустим только для восстановления уже committed результата.

Marker, quarantine metadata и redacted audit коммитятся с target rows; отказ
audit sink до COMMIT откатывает весь run. Audit содержит только hashes, counts,
UTC время, mode и committed outcome. Driver errors, SQL, parameters, key и
source values туда не копируются. Неуспешная transaction не оставляет committed
audit event: её статус сохраняется в staging. Полный recovery result в marker
содержит чувствительные identifiers и не предназначен для logs.

`max_receipt_bytes` ограничивает result/quarantine metadata. Ledger не очищается
при cleanup staging: tombstones сохраняются бессрочно. Удаление ledger или смена
его schema/namespace снимает прежнюю idempotency guarantee и требует отдельной
deployment policy. Уже committed запрос можно воспроизвести после expiry/cleanup
staging payload; новый load всё ещё требует действующий seal и artifact leases.

## Явный quarantine_invalid

Передайте `DryRunPolicy(error_policy="quarantine_invalid", ...)` в `preflight`
и настройте ledger/key. Global M11, provenance, scope, permissions, schema и
непроверенные CHECK/defaults не обходятся. Только известные record-level findings
M12 и SQLSTATE 23505/23503/23514/23502 допускают quarantine.

Группа включает все target units исходной записи и транзитивные FK, parent и
related-record связи. SAVEPOINT охватывает всю группу. Если child не проходит
поздний FK constraint, новые parent rows этой группы тоже откатываются; независимые
группы продолжают загрузку. Permission, timeout, cancellation, schema drift,
connection, неизвестный SQLSTATE и audit failure откатывают outer transaction.
`best_effort` не поддерживается. Бюджет evaluations ограничивает размер/обход групп.

Результат содержит `quarantined` target units и source counters
`loaded_records + rejected_records = attempted_records` (accepted skips входят
в loaded_records). Staging run с rejected units получает QUARANTINED и failure
retention. `execution_quarantine` хранит hash run/record/unit/table и исходные
закрытые issue codes; raw/normalized/mapping/validation/provenance artifacts
сохраняются по существующей retention policy. Hash record/unit позволяет связать
metadata с исходными IDs без копирования sensitive values в audit.

Если все группы invalid, target DML отсутствует, а ledger фиксирует этот итог.
Повтор key возвращает тот же outcome. Для исправленных данных нужен новый key
и новый соответствующий plan. Business/schema/provenance coordinator приложения
по-прежнему отвечает за проверки вне этого DB primitive.

## Проверки

- SQLite contract: общая Core INSERT группировка и сохранение units.
- Unit/security: SQL parameters, размеры chunks, staging binding, endpoint/user
  mismatch и отказ до writer I/O.
- PostgreSQL 16/18: parent/child, parent из позднего batch, composite ключи/FK,
  natural-key upsert, update/new path, duplicate across batches и поздний SQL
  конфликт, generated values, bounded lookup, cancellation, lost COMMIT,
  staging finalize failure и неизменность настоящего dry-run.
- M13-D: duplicate/concurrent key, rollback первого concurrent attempt, source/plan/
  policy conflict, recovery UNKNOWN, schema drift перед commit, quarantine whole
  dependency group, all-invalid, запрет обхода global validation и audit failure.

[ADR 0029](adr/0029-loader-ledger-and-quarantine.md) фиксирует transaction/replay contract.

## Граница готовности

Поставлены отдельные bounded PostgreSQL API. Общие SDK `ingest`, coordinator
всех уровней `ValidationReport`, `LoadReport.would_load_records` и streaming
large-input execution отсутствуют. Даже standalone DB-generated PK отклоняется;
успешная generated-key propagation/replay не заявляется. `append`, `update_only`,
`merge`, `best_effort`, циклы и deferred/two-phase load не поддержаны.
Полный milestone принят частично: [критерии и наблюдаемые тесты](plans/M13_acceptance.md).
Исправленные угрозы и оставшиеся границы deployment —
[security review](plans/M13_security_review.md).

Канонические требования: [ТЗ, §18][spec-load], [§20.3][spec-users],
[§20.4][spec-ddl] и [M13][spec-m13]. Решение об atomic writer —
[ADR 0028](adr/0028-atomic-postgresql-insert-upsert.md).

[spec-load]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#18-загрузка-в-бд
[spec-users]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#203-разделение-db-users
[spec-ddl]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#204-запрет-ddl
[spec-m13]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m13-staging-and-loader

## Signed audit M14

`PostgreSQLLoadPolicy.audit` вместе с `PostgreSQLLoader(audit_signer=...)`
включают HMAC chain и durable delivery intent в target transaction, независимо
от idempotency. Receipt получает `audit_head`; ledger хранит reference и replay
проверяет подписанный prefix. Legacy unsigned receipts не переписываются.
После rollback отдельный terminal audit может завершиться с явным `audit_gap`.
Bootstrap, grants, migration/compatibility и ограничения доставки описаны в
[руководстве M14](security-controls.md#postgresql-transaction).
