# Проверка MappingPlan M11

`MappingPlanValidator` проверяет декларацию по готовым снимкам, собирает
независимые issues и выдаёт `ValidatedMappingPlan` только при `ACCEPTED`.
Валидатор не открывает БД, файлы и соединения, не вызывает LLM, не генерирует SQL
и не преобразует значения. Основной API асинхронный.

Канонические требования: [проверка Mapping Plan, §14.1][spec-14-1],
[валидация, §16][spec-16], [загрузка, §18][spec-18],
[разделение DB users, §20.3][spec-20-3], [запрет DDL, §20.4][spec-20-4]
и [milestone M11][spec-m11]. Ниже описано подтверждённое поведение реализации.

## Использование

Передайте сохранённый JSON-план и уже полученные normalized manifest, catalog-v1
и optional M8 profile. Снимки создаются вне validator:
[Database Inspector](database-inspection.md) и [M8 profiler](normalized-profiling.md).
Профиль должен относиться к тому же manifest.

Пример разрешает только `public.orders(id, amount)` в PostgreSQL. Имена заданы
доверенным кодом приложения; exact IDs берутся из отражённого каталога, а не из
плана. Передача source PK `id` разрешена здесь явно. Это policy приложения,
а не проверка живых grants. Функция ничего не загружает и не создаёт таблицы.

<!-- example:m11-validation:start -->
```python
from structuraguard.contracts import (
    CatalogColumnRef,
    DatabaseCatalog,
    MappingPlanInputReport,
    MappingPlanValidationResult,
    MappingScope,
    MappingValidationOptions,
    MappingValidationPolicy,
    NormalizedDataProfile,
    NormalizedDatasetManifest,
)
from structuraguard.mapping import MappingPlanValidator


async def check_order_plan(
    payload: bytes,
    manifest: NormalizedDatasetManifest,
    catalog: DatabaseCatalog,
    *,
    profile: NormalizedDataProfile | None = None,
    options: MappingValidationOptions | None = None,
) -> MappingPlanValidationResult | MappingPlanInputReport:
    refs = {
        column.name: CatalogColumnRef(
            table_id=table.table_id, column_id=column.column_id,
        )
        for schema in catalog.schemas
        for table in schema.tables
        if (schema.name, table.name) == ("public", "orders")
        for column in table.columns
        if column.name in {"id", "amount"}
    }
    policy = MappingValidationPolicy(
        policy_id="orders-import-v1",
        scope=MappingScope(
            target_id=catalog.target_id,
            target_policy_fingerprint=catalog.target_policy_fingerprint,
            allow=tuple(refs.values()),
        ),
        allow_schemas=("public",),
        allow_tables=(("public", "orders"),),
        source_identity_allow=(refs["id"],) if "id" in refs else (),
    )
    validator = MappingPlanValidator(policy=policy, options=options)
    return await validator.validate_json(payload, manifest, catalog, profile=profile)
```
<!-- example:m11-validation:end -->

Вызов в async-коде: `result = await check_order_plan(payload, manifest, catalog,
profile=profile)`. Здесь `payload` — bytes JSON MappingPlan, не путь к файлу.
Пример проверяется тестом, извлекающим именно этот блок из документации: insert,
upsert, запрещённый target, malformed/SQL input, low confidence, drift, review
и limit error. Тесты используют синтетические снимки без сети и DB calls.

Для готового DTO используйте `await validator.validate(plan, manifest, catalog,
profile=profile)` с теми же явными policy/options. Не создавайте permissive DTO
через `model_construct`: даже переданный DTO повторно валидируется. Метод
`validate` входит в port `structuraguard.ports.MappingPlanValidator`;
`validate_json` — дополнительный вход конкретной реализации.

Проверяйте тип результата перед обращением к `validated_plan`:

- `MappingPlanValidationResult`: `decision`, `issues`, `evidence` и optional
  `validated_plan`. При REJECTED/NEEDS_REVIEW wrapper отсутствует.
- `MappingPlanInputReport`: отказ до корректного MappingPlan, `issues`, `locations`,
  optional digest и `complete`. Этот флаг означает полноту intake, не выполнение
  всех semantic checks. Для нечитаемого JSON он равен False.
- Непригодный trusted snapshot, policy или превышение resource budget вызывают
  `structuraguard.exceptions.ValidationError`; cancellation не подавляется.

Полный result с plan и bindings чувствителен. Для diagnostics используйте codes
и locations, не логируйте исходный payload, каталог или профиль.
У нового `MappingPlanValidationResult` M11 `evidence` присутствует; в legacy
snapshots поле может отсутствовать. Для привязки issues к locations используйте
`zip(result.issues, result.evidence.locations, strict=True)` после проверки
`evidence is not None`; у intake report locations лежат непосредственно в result.

### Исключения и решения

| Ситуация | Наблюдаемый результат |
|---|---|
| Неверная форма при создании DTO/policy/options | `pydantic.ValidationError`; объект не создаётся. |
| Нарушение policy/type/key/FK/threshold/drift у читаемого плана | Result с REJECTED или NEEDS_REVIEW, issues и без wrapper. |
| Нечитаемый JSON | `MappingPlanInputReport`, REJECTED, `complete=False`; snapshots могут ещё не проверяться. |
| Читаемая, но некорректная форма или hash плана | `MappingPlanInputReport`, REJECTED, `complete=True`; проверяются пригодные независимые части. Это не обещание проверки непригодных частей. |
| Повреждённый snapshot либо неподдержанный тип входного объекта | SDK `ValidationError`, обычно `MAPPING_PLAN_INVALID`. |
| Неполный DTO с отсутствующими обязательными атрибутами, в том числе после `model_construct` | SDK `ValidationError` с `MAPPING_PLAN_INVALID` на preflight. |
| Некорректная trusted policy/options при повторной проверке | SDK `ValidationError` с `MAPPING_POLICY_MISMATCH` либо `MAPPING_PLAN_INVALID`. |
| Неподдержанный catalog/dialect | SDK `ValidationError` с `DATABASE_METADATA_UNSUPPORTED`. |
| Превышение budget, включая размер отчёта | SDK `ValidationError` с `MAPPING_LIMIT_EXCEEDED`; partial result не возвращается. |
| Отмена coroutine | `asyncio.CancelledError` распространяется. |

У SDK exception доступен `error_code`. Повторять тот же непригодный input без
изменений бессмысленно; `NEEDS_REVIEW` также не даёт права исполнить план.
Внедряемый `clock` должен возвращать timezone-aware UTC `datetime`; ошибочный
результат часов отклоняется проверкой DTO (`pydantic.ValidationError`).

## Что проверяется

| Проверка | Поведение |
|---|---|
| Existence/scope | Exact semantic/catalog refs, schema/table/column allowlist. Пустой allow запрещает всё; deny сильнее write/read allow. Unknown selectors policy дают явную ошибку. |
| Writability | Views, generated, identity ALWAYS, non-writable tables/columns и system objects не разрешены для записи. Source PK/BY DEFAULT/rowid требуют явного разрешения. |
| Обязательные targets | Non-null без default/generation/identity должен иметь mapping. Domain NOT NULL также учитывается; `required=False` не отменяет constraint. |
| Types | Сравниваются canonical families и доступный профиль. Противоречие или потеря точности отклоняются; automatic cast запрещён. Unknown evidence даёт NEEDS_REVIEW. |
| Identity/upsert | Полный разрешённый PK либо безусловный unique key; nullable/partial/expression/invalid/deferred key не подходит. Natural key дополнительно подтверждается policy. |
| Relations | Полный FK ID и ordered pairs, source bindings, разрешённые lookup refs, selected parent-before-child order. Unresolved FK и выбранный цикл блокируют acceptance. |
| Integrity/drift | Plan/manifest/profile hashes и lineage; пересчитанный schema hash, target/inspection-policy binding, отдельные validation policy и writable projection. |
| Confidence | Decimal threshold, default `0.90`, включительно для plan и каждого field. Ниже порога — REJECTED независимо от общего score. |
| SQL/DDL | Только `insert_only`/`upsert`, дополнительно суженные policy. SQL/DDL/unknown fields в декларации не исполняются. Identifiers не интерполируются в запросы. |

`MappingIdentity` и `MappingRelation` требуют `MappingPlan.schema_version="1.1.0"`.
В 1.0.0 новые поля отсутствуют в wire/hash; legacy планы читаются по прежним
правилам. Legacy catalog не имеет достаточной metadata и отклоняется с
`DATABASE_METADATA_UNSUPPORTED`; manifest поддерживается в версиях 1.1.0/1.2.0.

У `MappingRelation` поддержаны `source_values`, `mapped_parent` и `lookup` как
декларации будущего разрешения. Source values/lookup требуют `policy.lookup_allow`
для полного parent key, который не попадает под deny. Parent rows не читаются.
Разрешение `source_identity_allow` проверяется для каждой записываемой PK колонки,
даже если identity/upsert использует другой unique key.
Повторённая колонка в unique index не становится identity key: такой индекс
исключается из кандидатов; при отсутствии другого ключа возвращается
`MAPPING_IDENTITY_REQUIRED`, а независимые проверки продолжаются.
Для PostgreSQL FK между известными `smallint`/`integer`/`bigint` совместимы;
диапазон значений каждой записываемой колонки проверяется отдельно.
`generated_key`, `deferred`, `two_phase` распознаются, но отклоняются как
неподдержанные стратегии. Случайный выбор FK по имени поля не выполняется.

## Отчёт и воспроизводимость

Все независимые issues собираются за один вызов. Если таблица неизвестна,
проверки её колонок/типов пропускаются; проверки других mappings продолжаются.
Дубли codes у разных locations сохраняются. Фиксированный порядок:
intake → fingerprints/bindings → existence/scope/writability → coverage → types →
identity → relations/graph → confidence. Внутри правила locations сортируются.

Issues имеют стабильные `code`, `message_key`, severity; `evidence.locations`
содержит соответствующие закрытые section и числовые index/component. Физические
source refs, SQL, имена скрытых объектов и значения записей в issues отсутствуют.

| Категория | Примеры codes |
|---|---|
| Вход | `MAPPING_PLAN_INVALID`, `MAPPING_PLAN_VERSION_UNSUPPORTED`, `MAPPING_LIMIT_EXCEEDED` |
| SQL/операция | `MAPPING_SQL_FORBIDDEN`, `MAPPING_DDL_FORBIDDEN`, `MAPPING_OPERATION_FORBIDDEN` |
| Integrity | `MAPPING_PLAN_FINGERPRINT_MISMATCH`, `MAPPING_SOURCE_LINEAGE_MISMATCH`, `MAPPING_TARGET_MISMATCH`, `MAPPING_POLICY_MISMATCH`, `MAPPING_CATALOG_FINGERPRINT_MISMATCH`, `DATABASE_SCHEMA_DRIFT` |
| Ссылки/policy | `MAPPING_SCHEMA_NOT_FOUND`, `MAPPING_TABLE_NOT_FOUND`, `MAPPING_COLUMN_NOT_FOUND`, `MAPPING_SOURCE_NOT_FOUND`, `MAPPING_SCOPE_INVALID`, `MAPPING_SCHEMA_DENIED`, `MAPPING_TABLE_DENIED`, `MAPPING_COLUMN_DENIED`, `MAPPING_SYSTEM_OBJECT_FORBIDDEN` |
| Структура | `MAPPING_COLUMN_NOT_WRITABLE`, `MAPPING_TABLE_NOT_WRITABLE`, `MAPPING_GENERATED_COLUMN`, `MAPPING_REQUIRED_TARGET_MISSING`, `MAPPING_SOURCE_CONFLICT`, `MAPPING_TARGET_AMBIGUOUS` |
| Типы | `MAPPING_TYPE_INCOMPATIBLE`, `MAPPING_TYPE_UNVERIFIED`, `MAPPING_TRANSFORMATION_REQUIRED`, `MAPPING_NULLABILITY_CONFLICT`, `MAPPING_LENGTH_OVERFLOW`, `MAPPING_NUMERIC_OVERFLOW` |
| Ключи | `MAPPING_IDENTITY_REQUIRED`, `MAPPING_IDENTITY_AMBIGUOUS`, `MAPPING_UPSERT_KEY_INVALID`, `MAPPING_IDENTITY_NULLABLE`, `MAPPING_SOURCE_IDENTITY_FORBIDDEN` |
| FK | `MAPPING_FK_NOT_FOUND`, `MAPPING_FK_PAIR_MISMATCH`, `MAPPING_RELATION_UNRESOLVED`, `MAPPING_RELATION_STRATEGY_UNSUPPORTED`, `CYCLIC_DEPENDENCY_REQUIRES_STRATEGY` |
| Порог | `MAPPING_CONFIDENCE_BELOW_THRESHOLD` |

`MAPPING_TYPE_UNVERIFIED` и `MAPPING_IDENTITY_AMBIGUOUS` требуют review. Остальные
нарушения блокируют план как errors; наличие любой error имеет приоритет.
Ни review, ни ручной flag не создают wrapper в обход правил.

Hash evidence исключает `validated_at`, но включает версии правил, policy/options,
фактические snapshots, writable flags, decision, issues и order. Hash не является
подписью или правом записи. Порядок mappings участвует в исходном plan hash;
перестановка unordered catalog/policy collections не меняет результат проверки.

## Ограничения

Budgets `MappingValidationOptions` ограничивают input/result bytes, число mappings,
tables/columns/edges/issues и work. До JSON parse ограничивается depth; до
serialization/hash выполняется preflight. Превышение не даёт partial acceptance
или молча усечённый отчёт. Это не OS/RSS sandbox и не гарантия wall-clock deadline.

| Option | По умолчанию | Максимум конфигурации |
|---|---:|---:|
| `max_input_bytes` | 8 388 608 | 16 777 216 |
| `max_result_bytes` | 4 194 304 | 8 388 608 |
| `max_mappings` | 1 024 | 1 024 |
| `max_tables` | 512 | 2 048 |
| `max_columns` | 10 000 | 10 000 |
| `max_edges` | 10 000 | 20 000 |
| `max_issues` | 10 000 | 50 000 |
| `max_operations` | 2 000 000 | 10 000 000 |

Все значения — положительные integers. `max_edges` также ограничивает число
identity descriptors и relation descriptors по отдельности. `max_operations`
ограничивает оценку работы, а не измеренное CPU время. Лимит результата проверяется
перед возвратом; при превышении собранные issues не возвращаются частично.

Не реализуются casts, arrays/JSON/time/binary acceptance без достаточного
representation evidence, генерация SQL, row lookup, generated-key propagation
и cycle execution. Record engine должен проверить значения enum, nullable/range,
precision/scale, CHECK/UNIQUE/FK, business rules и physical provenance. M8 evidence
может обнаружить конфликт, но не заменяет проверку всех загружаемых записей.
Если точный numeric extrema профиля уже требует округления до target scale,
выдаётся `MAPPING_TRANSFORMATION_REQUIRED`: например, `1.234 → numeric(4,2)`.
Значащая точность учитывается без округления; `1.2300` при scale 2 допустимо.
Известный по M8 extrema выход за конечный диапазон PostgreSQL `real`/`float4`
даёт `MAPPING_NUMERIC_OVERFLOW`; например, `1e100` отклоняется для `real`,
но допустим для `double precision`. Это не заменяет проверку остальных записей.

Перед записью обязательны свежий scoped inspection, повторная validation, grants,
раздельные inspector/writer, staging, `dry_run`, транзакция и rollback. Snapshot
hash не закрывает TOCTOU. SDK facade, idempotency и loader остаются отдельной
работой. См. [ADR 0021](adr/0021-mapping-plan-validation.md).

[spec-14-1]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#141-проверка-mapping-plan
[spec-16]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#16-валидация
[spec-18]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#18-загрузка-в-бд
[spec-20-3]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#203-разделение-db-users
[spec-20-4]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#204-запрет-ddl
[spec-m11]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m11-mappingplan-validator
