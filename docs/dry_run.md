# PostgreSQL dry-run M13-B

`PostgreSQLDryRunPlanner.plan()` строит прогноз загрузки по нормализованным
данным и текущему состоянию PostgreSQL. Он не меняет target tables, sequences,
существующие staging records или их lifecycle. Persistent staging, audit adapter
и writer DSN в API отсутствуют. Нужен extra `structuraguard[postgres]`.

Приложение передаёт полный snapshot и доверенные policies отдельными параметрами.
Функция не требует staging или writer credentials:

<!-- example:m13-dry-run:start -->
```python
from structuraguard.contracts.loading import (
    DryRunExecutionPlan,
    DryRunPolicy,
    DryRunRequest,
)
from structuraguard.database import PostgreSQLTarget
from structuraguard.database.dry_run import PostgreSQLDryRunPlanner


async def preview_load(
    inspector: PostgreSQLTarget,
    policy: DryRunPolicy,
    snapshot: DryRunRequest,
) -> DryRunExecutionPlan:
    return await PostgreSQLDryRunPlanner(inspector, policy=policy).plan(snapshot)
```
<!-- example:m13-dry-run:end -->

`DryRunRequest(batches=..., mapping=...)` содержит законченный tuple
`NormalizedBatch` 1.1/1.2 с terminal manifest и соответствующий `MappingPlan`.
В `DryRunPolicy` обязательны `writer_principal`, `mapping_policy` и `read_policy`;
их формирует владелец приложения. Для журналирования результата вызывайте
`execution_plan.safe_summary()`.

`safe_summary()` возвращает `dry_run`, `ready`, `planned_inserts`,
`planned_updates`, `planned_skips`, `planned_quarantine` и `blocker_count`.
Счётчики считают **target rows (write units)**: один исходный record может дать
несколько units. Полный plan содержит table order, source record/entity/value IDs,
column IDs, identity, допустимые update columns, FK dependencies, решения и codes.
Fingerprints связывают normalized manifest, mapping и повторную M11 validation,
точную проекцию, политики, M12 validation и результаты проверок ключей.

| Решение | Значение |
|---|---|
| `insert` | Допустимая новая строка по текущему snapshot. |
| `update` | Upsert нашёл identity; заданы mutable columns. Равенство прежних значений не превращает UPDATE в skip. |
| `skip` | Upsert нашёл identity, но mutable columns отсутствуют. |
| `quarantine` | Запись не прошла проверку или входит в зависимую группу исключённой записи. Это прогноз; quarantine store не записывается. |

`insert_only` не использует `DO NOTHING`: существующий unique key даёт `DB_UNIQUE`.
При `atomic` любая исключённая запись добавляет `DRY_RUN_ATOMIC_REJECTED`.
При `quarantine_invalid` допустимы только закрытые ошибки данных: NOT NULL, type,
length, enum, numeric bounds/scale, value, CHECK, UNIQUE и FK. Неизвестная семантика
всегда блокирует `ready`, независимо от error policy. Units одного source record,
его parent/related records и связанных FK групп исключаются вместе. Эта
консервативная политика не оставляет половину логической записи.

## Проверки и транзакция

1. До соединения проверяются budgets, DTO, batch hashes и полный EOF manifest.
   Проекция использует точные `normalized_value` без SQL, coercion и join догадок.
   Она сохраняет source IDs для каждой target cell. Несколько entity типов для
   одной target row требуют отдельного join contract и отклоняются.
2. Inspector открывает отдельное соединение с `SERIALIZABLE READ ONLY` и
   `search_path=pg_catalog`, заданными также в startup settings драйвера.
   `ACCESS SHARE` locks берутся по точному trusted table scope до первого snapshot.
   В этом же соединении читается bounded catalog и сравнивается schema fingerprint.
3. MappingPlan повторно проверяется M11 на свежем catalog и профиле, вычисленном
   из переданных batches. `ACCEPTED` wrapper или внешний `ValidationReport` не
   заменяет эту проверку. DB constraints M12 повторяются на построенной проекции.
4. Writer должен быть отдельным login, без superuser/CREATEROLE/CREATEDB/BYPASSRLS,
   database/schema CREATE, owner membership или доступа к привилегированным ролям.
   На таблицах проверяются USAGE, INSERT и для upsert UPDATE; DELETE/TRUNCATE/TRIGGER
   у writer запрещены. На разрешённых `read_policy.allow_columns` проверяется
   SELECT обоих principals. Read policy должна покрывать записываемые columns и
   полные ключи для UNIQUE/FK; доступ не расширяется по metadata автоматически.
   Grants проверяются отдельно: schema fingerprint их не включает.
5. Закрытый reader выполняет только параметризованные EXISTS по проверенным
   ключам. UNIQUE, FK lookup и классификация upsert используют одну transaction.
   Входные parent keys образуют зависимости; родительские таблицы идут первыми.
   Cycles и generated/deferred/two-phase FK strategies отклоняются M11.
6. Перед возвратом соединение откатывается и закрывается. В нём никогда не
   выполняются target DML, `nextval`, defaults, triggers или `EXPLAIN ANALYZE`.
   Отмена распространяет `CancelledError` после bounded cleanup; timeout даёт
   `PROCESSING_TIMEOUT`. Cleanup failure блокирует повторное использование
   экземпляра. Partial execution plan при ошибке соединения не возвращается.

`max_records` ограничивает и исходные records, и target units. `max_bytes`
ограничивает вход до сериализации; `max_keys` и `max_queries` — совокупные key
lookups и EXISTS statements внутри transaction. Metadata и grants дополнительно
ограничены scope и общим deadline. Остальные budgets берутся из `ConstraintReadPolicy.limits`
и `PostgreSQLTarget.limits`.

## Поддержанная семантика и безопасность результата

Поддержаны явные проверяемые scalar keys и FK через exact source values/lookup
или mapped parent. Natural-key → generated-ID translation не выполняется.
Generated/default values не вычисляются: если строке они нужны, plan получает
`DRY_RUN_DEFAULT_UNVERIFIED`. CHECK требует явного trusted `CheckRuleBinding`
для M12; иначе `DB_CONSTRAINT_UNVERIFIED`. Неизвестные collations/key operators,
неподдержанные constraints и обновление дополнительных unique/PK columns при
upsert блокируют `ready`.

Views, RLS, partitioning/inheritance, user triggers/rules, expression/partial,
невалидные и не-btree indexes в используемом scope отклоняются до row queries.
Эти ограничения нужны, чтобы неизвестная DB семантика не выглядела проверенной.

Текущий вход — проверенный immutable snapshot normalized данных от приложения.
Copy values поддержаны; transformation/issue evidence без physical replay
отклоняется с `DRY_RUN_PROVENANCE_UNVERIFIED`. Fingerprint не удостоверяет
подлинность внешнего источника. Полный physical replay M12 и business/schema policy coordinator остаются
отдельной интеграцией. Atomic writer доступен через [PostgreSQLLoader](loader.md).

SQL/parameters/DSN не входят в plan или диагностику. SQLAlchemy instance loggers
подавлены без изменения глобального logging, raw driver exceptions заменяются
закрытыми `LoadError` codes. Полный JSON plan содержит чувствительные identifiers
и fingerprints; публиковать и логировать следует только `safe_summary()`.
Обработчик приложения не должен логировать исходный `DryRunRequest` или подключать
SQL tracing с параметрами. Server-side logging настраивается владельцем PostgreSQL.

Конструирование некорректных DTO может дать `pydantic.ValidationError` с входными
значениями: его полный текст также не подходит для logs. Занятый либо недоступный
после сбоя cleanup planner возвращает `DatabaseInspectionError` с кодом
`DATABASE_INSPECTION_BUSY`; ошибки исполнения прогноза — `LoadError`.

Успешный прогноз не резервирует keys и не выдаёт разрешение на запись. DML или
GRANT другого соединения могут изменить результат будущей загрузки; writer
повторяет grants/schema/constraints в своей transaction. Существующий inspector
`DatabaseAdapter.execute` остаётся заглушкой и не начинает писать при dry_run=False.

## Проверки

`tests/unit/loading` проверяет exact projection, целостность входа, counters и
atomic/quarantine. `tests/security/loading` проверяет отказ до I/O и intake budgets.
`tests/integration/database/test_postgresql_dry_run.py` на PostgreSQL 16/18 сравнивает
содержимое target и staging вместе с xmin/ctid и состоянием sequence до/после,
проверяет FK order, grants/schema drift, cancellation, отсутствие secrets и
серверный запрет DML/nextval даже при ошибке внутреннего кода.

Канонический scope: [ТЗ, M13][spec-m13] и [§18][spec-load].
Решение о read-only transaction — [ADR 0027](adr/0027-read-only-dry-run-planning.md).
Граница готовности milestone — [аудит приёмки](plans/M13_acceptance.md).

[spec-m13]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m13-staging-and-loader
[spec-load]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#18-загрузка-в-бд
