# DB constraints и business rules M12-C

`BusinessRuleValidator` проверяет полный scope записей без I/O.
`DatabaseConstraintValidator` проверяет тот же immutable input по catalog-v1 и,
при наличии `ConstraintReader`, по существующим ключам БД. Оба сервиса собирают
все применимые независимые issues и не изменяют значения.

<!-- example:m12-rules:start -->
```python
import asyncio
from structuraguard.contracts import BusinessRuleSet, ValidationDataset
from structuraguard.validation import BusinessRuleValidator

async def main() -> None:
    data = ValidationDataset.model_validate({
        "records": ({
            "record_id": "order-1", "collection_id": "orders",
            "values": ({"field_id": "amount",
                        "value": {"kind": "decimal", "value": "-2.50"}},),
        },),
    })
    rules = BusinessRuleSet.model_validate({
        "fields": ({"collection_id": "orders", "field_id": "amount",
                    "value_type": "decimal"},),
        "rules": ({
            "rule_id": "positive_amount", "collection_id": "orders", "op": "gte",
            "left": {"kind": "field", "field_id": "amount", "value_type": "decimal"},
            "right": {"kind": "literal", "value": {"kind": "decimal", "value": "0"}},
        },),
    })
    before = data.canonical_json()
    result = await BusinessRuleValidator().validate(data, rules=rules)
    assert [issue.code for issue in result.issues] == ["RULE_VIOLATION"]
    assert result.issues[0].record_id == "order-1"
    assert data.canonical_json() == before

asyncio.run(main())
```
<!-- example:m12-rules:end -->

## Input и границы scope

`ValidationDataset.records` — завершённая ограниченная коллекция, объединённая
caller из batches **после EOF**. `record_id` уникален во всём dataset;
`collection_id` — entity/collection ID, для DB — точный catalog `table_id`.
`ValidationCell.field_id` соответствует DSL schema или catalog `column_id`.
`parent_id` явно связывает child с parent; циклы, неизвестные parents и повторные
IDs запрещены. Неограниченный streaming здесь не поддерживается.

`BusinessRuleValidator(limits=None)` выбирает defaults. Любое другое значение
должно быть точным `RecordValidationLimits`; ложные значения и subclasses не
заменяются defaults. При bounded intake в `max_nodes` заранее учитываются
посещённые и ожидающие обхода nodes, чтобы shared references не раздували очередь.

Отсутствие cell означает **missing**, `NullScalar` — **explicit null**. Значения
строго tagged: bool не integer, строка даты не date, money не float.
Нормализация, исправление типов, вычисление defaults, усечение, padding и
округление не выполняются. Dataset — sidecar projection: исходные
`NormalizedBatch`, raw values, normalization trace и provenance сохраняет caller.
Автоматическая projection/привязка ParsePlan/MappingPlan и общий Validation Engine
ещё не реализованы в общем [плане M12](plans/M12_validation_engine.md).
[Provenance validation и report builder](provenance-validation.md) доступны отдельно;
они не строят projection и не запускают DB/rules вместо caller.

## Allowlisted DSL

| Операция / DTO | Семантика |
|---|---|
| `ComparisonRule`: `equals`, `not_equals`, `gt`, `gte`, `lt`, `lte`, `date_lt`, `date_lte` | Два typed operands; ordinal operations для чисел/date/datetime, date operators требуют date/datetime одного типа |
| `RequiredIfRule`: `required_if` | При равенстве operands поле должно присутствовать и быть non-null |
| `PresenceRule`: `mutually_exclusive`, `at_least_one` | Наличие non-null cell; пустая строка считается присутствующей |
| `SumEqualsRule`: `sum_equals` | Сумма `terms` по children конкретного parent; `ProductOperand.factors` — до 8 typed operands, без expression string |
| `ItemCountRule`: `min_items`, `max_items` | Число children данного parent в `item_collection_id` |
| `UniqueByRule`: `unique_by` | Ordered composite key, обязательные `scope=collection/parent` и `nulls=distinct/equal/reject`; все дубликаты получают issue |
| `MatchesReferenceRule`: `matches_reference` | Ordered key из trusted `RuleReference`, связанного по reference ID, fingerprint и типам |

`FieldOperand` содержит точное имя и ожидаемый тип; `LiteralOperand` — tagged
константу. Доступ к attributes, Python/SQL expressions, callbacks, URL/file
retrieval и dynamic import отсутствует. Unknown operation/extra fields
отклоняются при разборе DTO; неизвестные field refs дают `RULE_FIELD_UNKNOWN`.

Missing operand даёт `RULE_MISSING_OPERAND`, null по умолчанию —
`RULE_NULL_OPERAND`. `ComparisonRule`/`RequiredIfRule` допускают явные policies:
`equal` сравнивает null только в equals/not_equals, `pass` оставляет null-condition
без нарушения (например, trusted представление SQL CHECK UNKNOWN). Missing
никогда не становится null. Wrong types дают `RULE_TYPE_MISMATCH`.

Сумма пустого набора равна 0; null item не пропускается. Child без корректного
parent scope даёт `RULE_PARENT_SCOPE_INVALID`. Сумма и произведения integer/Decimal
используют точные рациональные промежуточные значения, поэтому не зависят от
process Decimal context; float не принимается. `tolerance=0` по умолчанию.
Ненулевая tolerance разрешается только trusted `max_tolerance=DecimalScalar(...)`
конструктора и никогда не округляет исходные данные.

`RuleReference` задаётся владельцем в constructor, имеет immutable typed keys;
`reference.fingerprint` пересчитывается. RuleSet не может скачать или заменить
справочник. Unicode-строки сравниваются точно, без casefold/NFKC; keys не склеиваются
через separator. Даты неоднозначно не разбираются — это граница normalizer M12-A.

## DB validator

Caller передаёт target и отдельные policies из доверенной конфигурации, catalog
из DB Inspector и dataset с его IDs. Функция выполняет read-only prechecks;
она не создаёт schema, не записывает rows и не выдаёт права loader.

<!-- example:m12-database:start -->
```python
from structuraguard.contracts import ConstraintValidationPolicy, ConstraintReadPolicy
from structuraguard.contracts.database import DatabaseCatalog
from structuraguard.contracts.record_validation import ValidationDataset, RecordValidationResult
from structuraguard.database import DatabaseConstraintReader, PostgreSQLTarget, SQLiteTarget
from structuraguard.validation import DatabaseConstraintValidator


async def validate_database(
    dataset: ValidationDataset,
    catalog: DatabaseCatalog,
    target: SQLiteTarget | PostgreSQLTarget,
    policy: ConstraintValidationPolicy,
    read_policy: ConstraintReadPolicy,
) -> RecordValidationResult:
    # Чтение key columns требует отдельного read_policy.allow_columns.
    reader = DatabaseConstraintReader(target, policy=read_policy)
    return await DatabaseConstraintValidator(policy, reader=reader).validate(
        dataset, catalog=catalog,
    )
```
<!-- example:m12-database:end -->

`ConstraintValidationPolicy` связывает target ID, target policy fingerprint,
database fingerprint, точный column allowlist и `ConstraintTablePolicy` операций.
PK/source identity дополнительно требует `source_identity_allow`.
Upsert требует ordered identity, подтверждённой полным поддержанным PK/UNIQUE.
Policy не заменяет MappingPlan validation и не даёт writer permissions.

Порядок: bounded intake и catalog binding → local fields/scope → typed CHECK →
все incoming unique keys → FK после завершения всех parent collections → один
read request существующих ключей. FK проверяет типы также по родительским колонкам.

| Проверка | Результат |
|---|---|
| NOT NULL | Missing допускается при известном default/generated/identity; explicit null проверяется отдельно |
| Types, length, enum | Без coercion и truncation; SQLite `VARCHAR(n)` дополнительно ограничивается консервативной SDK guard |
| Integer bounds | PostgreSQL smallint/int/bigint; SQLite signed 64-bit |
| Decimal precision/scale | Включая отрицательную scale; required rounding становится issue |
| UTC datetime | PostgreSQL `timestamp with time zone` без precision modifier; явный `timestamp(p)` даёт `DB_CONSTRAINT_UNVERIFIED`, поскольку catalog ещё не моделирует temporal precision. Это блокирует также прямой reader до I/O |
| Generated/nonwritable | Передача значения, включая null, отклоняется |
| UNIQUE/PK | Exact ordered keys всего входа; DB matches проверяются отдельно; nullable UNIQUE с distinct nulls не создаёт конфликт |
| FK | Ordered pairs, SIMPLE/FULL null semantics, late incoming parents, затем DB EXISTS |
| Upsert | Существующая та же identity допустима; другая строка по secondary UNIQUE или повторная incoming identity — conflict |
| CHECK/domain CHECK | SQL metadata инертны; без verified typed representation — `DB_CONSTRAINT_UNVERIFIED` |

`CheckRuleBinding` задаётся только trusted policy и связывается с catalog fingerprint,
table, optional domain column и hash точного expression. `equivalence_verified=True`
— **заверение владельца приложения**, что typed правила эквивалентны CHECK, включая
null semantics; hash сам по себе ничего не доказывает. Поддержаны только локальные
comparison/required/presence правила. Непустой binding обязателен; stale binding
отклоняется. Missing default не вычисляется из SQL и остаётся dependent issue.

Неизвестные collations/operator classes, partial/expression/deferred indexes,
неподдержанные domain key representations/SQLite numeric affinity, partitioned
objects и неполные metadata не считаются успешной проверкой.
Текстовые ключи поддержаны только при доказанной binary collation:
SQLite BINARY, PostgreSQL C/POSIX на поддержанном индексе и text/varchar.
SQL equality произвольной collation не заменяется Python equality.

## Read-only adapter и ограничения

`DatabaseConstraintReader(SQLiteTarget | PostgreSQLTarget, policy=...)` реализует
`ports.ConstraintReader`. Constructor не выполняет I/O; нет global state/cache.
Каждый read проверяет request и scope **до подключения**, затем заново инспектирует
каталог и читает keys **в той же транзакции**. SQLAlchemy строит только
параметризованные EXISTS по catalog identifiers. `ConstraintLookup` не содержит
SQL, произвольных predicates или выражений. Возвращаются только boolean outcomes,
без DB rows/значений, DSN и driver messages.

SQLite открывается `mode=ro`, с `query_only`, `trusted_schema=OFF`, authorizer,
progress/deadline controls. PostgreSQL использует отдельного reader principal,
SERIALIZABLE READ ONLY, bounded statement/lock timeouts и NullPool;
`ACCESS SHARE` берётся на читаемые таблицы до первого SELECT/snapshot, чтобы
между reflection и EXISTS имя не стало view. Locks освобождаются при rollback;
они не блокируют обычный DML, но исключают конкурирующий DDL с AccessExclusive. RLS вызывает
явный отказ `DB_CONSTRAINT_UNVERIFIED`, чтобы скрытые строки не выглядели
отсутствующими. Metadata scope не подтверждает SELECT permissions: владелец должен
дать reader права SELECT на разрешённые keys и права, достаточные для
`LOCK TABLE ... IN ACCESS SHARE MODE` (табличный SELECT в проверенной конфигурации). Ошибки доступа не маскируются.

`ConstraintReadPolicy` ограничивает key columns, `chunk_size` (до 64 key lookups
на один SELECT), `max_queries` для key SELECTs и число keys. Metadata queries,
rows, bytes, deadline и cleanup ограничивает `target.limits`. Весь результат
привязан к точному request fingerprint и fingerprint boolean outcomes;
`consistency=single_transaction` описывает только этот read. Это не reusable DB
snapshot token и не гарантия после закрытия транзакции. Нет долгоживущих locks.

`RecordValidationLimits` задаёт records/rules/keys/issues/nodes/bytes/text/numeric
и evaluation budgets. CHECK bindings учитывают общий budget, а не перезапускают
его для каждого CHECK. Превышение вызывает typed `ValidationError` с
`SECURITY_LIMIT_EXCEEDED`, без partial acceptance. Reader errors —
`DatabaseInspectionError`; malformed Pydantic input — redacted contract error.
Встроенные guards ограничивают Python/SQL work, но не заменяют OS/server isolation.

## Issues и repair boundaries

`RecordValidationResult.issues` содержит `RecordValidationIssue`: вложенный
`issue: ValidationIssue` с code/message_key, `record_id`, `collection_id`, `field_ids`
и optional `rule_id`. Property `code` доступно прямо на wrapper. Raw values отсутствуют.
`accepted` означает отсутствие issues; `DB_CONSTRAINT_UNVERIFIED` также блокирует
acceptance. Полный набор сериализованных input/policies остаётся чувствительным.
IDs, field names и пользовательские rule IDs тоже могут содержать PII; полный
report не является безопасным log event. Для общей сводки используйте
`DetailedValidationReport.safe_summary()` после доверенной сборки результатов.

Невалидные DTO, policy bindings и превышение budgets дают SDK
`structuraguard.exceptions.ValidationError`; сбой DB adapter, schema drift,
отказ доступа или timeout — `DatabaseInspectionError`. Эти исключения не заменяют
ошибки данных и не возвращают частичный успешный результат. При создании DTO
напрямую возможен `pydantic.ValidationError`: его текст может включать исходный input.

Коды: `DB_NOT_NULL`, `DB_TYPE_MISMATCH`, `DB_LENGTH`, `DB_NUMERIC_BOUNDS`,
`DB_NUMERIC_SCALE`, `DB_ENUM`, `DB_VALUE_INVALID`, `DB_COLUMN_NOT_WRITABLE`,
`DB_TABLE_NOT_WRITABLE`, `DB_TARGET_NOT_ALLOWED`, `DB_COLUMN_NOT_ALLOWED`,
`DB_SOURCE_IDENTITY_NOT_ALLOWED`, `DB_UPSERT_IDENTITY_INVALID`, `DB_UNIQUE`,
`DB_FOREIGN_KEY`, `DB_CHECK`, `DB_CONSTRAINT_UNVERIFIED`; DSL —
`RULE_VIOLATION`, `RULE_MISSING_OPERAND`, `RULE_NULL_OPERAND`, `RULE_TYPE_MISMATCH`,
`RULE_FIELD_UNKNOWN`, `RULE_PARENT_SCOPE_INVALID`, `RULE_REFERENCE_INVALID`,
`RULE_TOLERANCE_NOT_ALLOWED`.

Автоматических repairs нет. Изменение нормализации, правил, policy или данных
требует нового validation report. Финальная защита UNIQUE/FK/CHECK, concurrency,
TOCTOU и commit/rollback остаётся обязанностью будущего транзакционного loader.

Решение: [ADR 0024](adr/0024-deterministic-record-constraints.md).
Канонические требования: [ТЗ, §16.4](https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#164-ограничения-бд)
и [§16.5](https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#165-бизнес-правила).
Семантика сверена с первичными источниками:
[PostgreSQL constraints](https://www.postgresql.org/docs/18/ddl-constraints.html),
[numeric types](https://www.postgresql.org/docs/18/datatype-numeric.html),
[SQLite types и collations](https://www.sqlite.org/datatype3.html),
[PostgreSQL LOCK](https://www.postgresql.org/docs/18/sql-lock.html).
