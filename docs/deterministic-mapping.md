# Детерминированное сопоставление M9

`DeterministicMapper` ранжирует существующие колонки БД для полей завершённого
профиля M8. Все scores вычисляются внутри SDK. Mapper работает локально:
не открывает файлы/БД, не вызывает LLM и не использует embeddings.

## Получить кандидатов

Передайте профиль из [NormalizedDataProfiler](normalized-profiling.md), каталог
из [Database Inspector](database-inspection.md) и явно разрешённые refs.
В этом примере caller разрешает только `public.customers.email`; пустой набор
не включает остальные колонки автоматически.

<!-- example:m09-rank:start -->
```python
from structuraguard.contracts import (
    CatalogColumnRef,
    DatabaseCatalog,
    DeterministicMappingOptions,
    DeterministicMappingResult,
    MappingScope,
    NormalizedDataProfile,
)
from structuraguard.mapping import DeterministicMapper


async def rank_email(
    profile: NormalizedDataProfile,
    catalog: DatabaseCatalog,
) -> DeterministicMappingResult:
    allowed = tuple(
        CatalogColumnRef(table_id=table.table_id, column_id=column.column_id)
        for schema in catalog.schemas
        for table in schema.tables
        for column in table.columns
        if (schema.name, table.name, column.name) == ("public", "customers", "email")
    )
    scope = MappingScope(
        target_id=catalog.target_id,
        target_policy_fingerprint=catalog.target_policy_fingerprint,
        allow=allowed,
    )
    return await DeterministicMapper(DeterministicMappingOptions(top_k=5)).rank(
        profile, catalog, scope=scope
    )
```
<!-- example:m09-rank:end -->

В своём async коде вызовите `result = await rank_email(profile, catalog)`.
Для логирования используйте `result.safe_summary()`; полный result сохраняет
чувствительные имена и refs. Для SQLite замените selector `public` на реальное
имя schema из каталога, обычно `main`.

Канонические требования: [Semantic Catalog][spec-catalog],
[сигналы сопоставления][spec-mapping], [оценка достоверности][spec-confidence],
[контекст сущностей и отношений][spec-relations] и [M9][spec-m9].
[Mapping Plan][spec-plan] описывает границу будущего этапа; M9 его не создаёт.

Поддержаны profile schema `1.0.0` из M8 и catalog schema `1.1.0` / `catalog-v1`
из M7 для PostgreSQL и SQLite. Старый catalog `1.0.0` отклоняется новым API.
Изменения legacy DTO и автоматического подключения mapper к SDK facade в M9 нет. Scope может только сузить каталог: `deny` сильнее `allow`, неизвестный
ref и несовпадающий target/policy binding отклоняются. Selector не нормализует
DB identity; нормализованные имена используются только для scoring.

## Прочитать результат

`result.fields` отсортирован по `(entity_type, field_name)`;
`result.field(entity_type, field_name)` возвращает точное поле, либо безопасный
`KeyError("semantic_field_not_found")`. В `FieldCandidates` доступны:

| Поле | Смысл |
|---|---|
| `candidates` | До `top_k` legacy `MappingCandidate` с refs, confidence и lineage |
| `explanations` | Один `CandidateExplanation` на candidate ID в том же порядке |
| `status` | `auto_candidate`, `review`, `rejected` либо `unmapped` |
| `ambiguous` | Есть конкуренты с недостаточным отрывом, даже при `top_k=1` |
| `gap` | Разница двух лучших base scores; `None`, если второго кандидата нет |
| `competitor_count`, `tie_count` | Число admissible targets и число лучших с равным score до top-k |
| `reasons` | Безопасные machine-readable diagnostics, включая исключённые несовместимости |

`rejected` сохраняет низкооценённых кандидатов для анализа. `unmapped` означает,
что admissible candidates нет. `auto_candidate` — рекомендация; это не
`ValidatedMappingPlan`, permission или разрешение импортировать данные.

У explanation есть `base_score`, `final_score`, `signals`, `compatibility`,
`blockers`, penalties, FK и identity evidence. В каждом сигнале хранятся
value/weight/contribution, availability и optional coverage. Недоступный сигнал
равен нулю и отмечен `available=False`; вес не распределяется другим сигналам.

| Сигнал | Default weight |
|---|---:|
| `name_similarity` | 0.35 |
| `alias_match` | 0.20 |
| `type_compatibility` | 0.20 |
| `value_pattern_match` | 0.10 |
| `structural_context` | 0.10 |
| `database_relation_score` | 0.05 |

`exact_name`, `normalized_name`, `transliterated_name` публикуются с нулевым
весом: их лучший вариант уже входит в `name_similarity`, повторного сложения нет.
SDK суммирует contributions и вычитает penalties, ограничивая final score [0,1].
Произведения имеют 12 знаков, scores/gaps — 6, ROUND_HALF_EVEN и локальный
Decimal context. Фиксированный порядок tie-breaking: base score по убыванию,
затем точные schema/table/column names и IDs по возрастанию Unicode code points.

`MappingWeights` и `DeterministicMappingOptions` доступны в `structuraguard.contracts`.
Weights задаются `Decimal` либо десятичными строками, должны быть конечными,
неотрицательными, суммой ровно 1. Float и непомерные коэффициенты/exponents
отклоняются. Options immutable; runtime не читает env и не обучает веса.
Defaults: `auto_threshold=0.90`, `review_threshold=0.70`,
`ambiguity_margin=0.10`, penalties ambiguity/collision по `0.10`.
Граница review/auto включается, gap ровно 0.10 достаточен. Blockers всегда
сильнее score. Пересечение двух top-1 source fields в одном target вызывает
`TARGET_COLLISION`, без жадного переназначения второго поля.

## Русские и английские aliases

`DatabaseSemanticCatalog` принимает готовый DTO или Python dict того же wire
формата. Файлы YAML/JSON mapper не загружает; caller передаёт данные после своего I/O.
JSON serialization/validation DTO доступна через стандартные методы Pydantic.
Catalog привязан к target, target policy и database fingerprint. Его
`SemanticTable` адресует точные `schema_name`/`table_name`, `SemanticColumn` —
`column_name`. Aliases отсортированы и дедуплицированы; противоречивые annotations
одного target отклоняются. Alias для нескольких targets сохраняет конкурентов.

<!-- example:m09-aliases:start -->
```python
from structuraguard.contracts import (
    DatabaseCatalog,
    DatabaseSemanticCatalog,
    SemanticColumn,
    SemanticTable,
)


def customer_aliases(catalog: DatabaseCatalog) -> DatabaseSemanticCatalog:
    return DatabaseSemanticCatalog(
        target_id=catalog.target_id,
        target_policy_fingerprint=catalog.target_policy_fingerprint,
        database_fingerprint=catalog.database_fingerprint,
        tables=(
            SemanticTable(
                schema_name="public",
                table_name="customers",
                aliases=("Клиенты", "Customers"),
                columns=(
                    SemanticColumn(
                        column_name="legal_name",
                        aliases=("Контрагент", "Покупатель", "Customer"),
                    ),
                    SemanticColumn(
                        column_name="inn",
                        aliases=("ИНН организации", "Tax ID"),
                        semantic_type="inn",
                    ),
                ),
                identity_keys=(("inn",),),
            ),
        ),
    )
```
<!-- example:m09-aliases:end -->

Этот независимый блок рассчитан на каталог с `public.customers.legal_name` и
`public.customers.inn`. Разрешите нужные refs в отдельном `MappingScope`.
Передайте результат как `semantic_catalog=customer_aliases(catalog)` в `rank`.
Aliases не расширяют scope. Table identity keys и allowed operations являются
hints для дальнейших этапов: mapper не выбирает upsert и не подтверждает keys.
Description/comments не исполняются и не используются как aliases автоматически.

Имена сравниваются после NFKC, casefold, camelCase/token split, compact form.
Фиксированный `ru_lat_v1` даёт дополнительный сигнал до 0.80; ё→е тоже только
дополнительное сравнение. Смешанные Latin/Cyrillic tokens блокируют auto. Для generic-only имени
без alias/structural/graph evidence возвращается `GENERIC_NAME_ONLY`. Встроенный небольшой `ru_en_aliases_v1` поддерживает ИНН/Tax ID,
Контрагент/customer, Общая сумма/total_amount, Дата создания/created_at и другие
явно перечисленные в коде concepts. Stemming и автоматического перевода нет.

## Совместимость и context

Mapper использует агрегаты M8, включая native observed kinds, pattern counts,
coverage, lengths и extrema. Raw samples не участвуют в scoring. Несовместимый
тип, известный overflow или потеря формата identifier исключают target.
String→DATE/NUMERIC/UUID остаётся `conditional` с `TRANSFORMATION_REQUIRED`.
Unknown types, domain checks, enum membership, precision/scale и timezone могут
требовать дальнейшей validation. SQLite affinity сама по себе не подтверждает
совместимость. Null/default и generated/writable учитываются раздельно.

Source context — semantic entity type, сохранённые labels файла/листа/родителя/
секции/таблицы и co-occurrence neighbors. Anchors определяются один раз по
lexical/type evidence без context. FK signal различает table-level поддержку
0.50 и полный набор ordered column pairs 1.00. Composite pairs не сортируются.
Кандидат, использующий cyclic/self FK, требует `FK_STRATEGY_REQUIRED`;
неподтверждённый parent resolution оставляет `FK_UNRESOLVED`. Исключение
родителя из scope не снимает blocker с дочерней колонки. Даже полное
structural evidence не проверяет реальные FK values или grants.

## Лимиты и ошибки

Default `top_k=5`, допустимо 1–10. Два полных прохода не материализуют матрицу
field×column; heap хранит до `max(k, 2)` кандидатов на поле. Поиска через embeddings
и приблизительного pre-shortlist нет. Верхние пределы одновременно служат defaults:

| Budget | Ceiling |
|---|---:|
| Source fields / target columns | 1024 / 10 000 |
| FK edges / source relationships | 20 000 / 4096 |
| Aliases | 10 000 |
| Matching name / token count | 256 UTF-8 bytes / 32 |
| Field×column pairs, оба прохода суммарно | 2 000 000 |
| Evidence operations | 10 000 000 |
| Input / retained / result allocation budgets | 16 / 32 / 8 MiB |

Budgets можно сужать; совместное достижение всех ceilings не гарантируется.
`max_operations` учитывает также обход FK components, neighbors/anchors и
identity keys/constraints до вычисления соответствующего evidence, включая
ограничения FK с родителем вне scope. Это консервативный счётчик работы,
а не лимит времени в секундах.
Размеры оцениваются консервативно с overhead, поэтому JSON может быть меньше cap,
а вызов всё равно превысить allocation budget. Превышение даёт
`MappingError/MAPPING_LIMIT_EXCEEDED` целиком. Прочие коды:
`MAPPING_INPUT_INVALID`, `MAPPING_UNSUPPORTED_SCHEMA`, `MAPPING_BINDING_MISMATCH`,
`MAPPING_SEMANTIC_CATALOG_INVALID`. Schema hash mismatch сохраняет
`DatabaseInspectionError/DATABASE_SCHEMA_DRIFT`. Snapshot hash не проверяет
изменения живой БД после inspection. Отмена даёт `CancelledError`; checkpoints
выполняются между блоками по 128 пар, без фоновых задач и владельцев I/O.

Создание DTO (`MappingScope`, options, semantic annotations) может завершиться
`pydantic.ValidationError`: такая ошибка может содержать входные значения и
не предназначена для безопасного логирования. Конструктор `DeterministicMapper`
ревалидирует переданные options и возвращает `MappingError/MAPPING_INPUT_INVALID`
при их некорректности; превышение preflight budget даёт `MAPPING_LIMIT_EXCEEDED`.
`rank` ревалидирует snapshots и выдаёт перечисленные выше
безопасные machine-readable ошибки без partial result.

Caller отвечает за происхождение snapshots и формирование scope из своей policy.
Hashes не удостоверяют источник данных. Mapper не проверяет grants, существование
родительских DB rows и живую schema после inspection; он не преобразует значения,
не выбирает insert/upsert, не создаёт MappingPlan, staging или SQL. Эти возможности
не объявлены готовыми в M9.

Полный result и его refs/hashes чувствительны. Repr новых DTO скрыт, для logs
используйте `result.safe_summary()`. Не логируйте отдельно legacy candidate:
его прежний repr и serialization содержат names/refs.

## Проверить копируемые примеры

Примеры выше исполняются непосредственно из Markdown, каждый в отдельном
процессе. Проверка использует синтетические snapshots, запрещает сеть и
проверяет ru/en aliases, пустой scope и safe summary:

```bash
uv run --locked --no-sync pytest packages/structuraguard/tests/docs/test_m09_examples.py -q
```

## Воспроизвести evaluation

Из корня репозитория с dev dependencies:

```bash
uv run --locked --no-sync python scripts/evaluate_deterministic_mapping.py
```

Корпус — 24 синтетических ru/en случая: 12 calibration, 12 holdout; схемы
не пересекаются. Evaluator перебирает фиксированную сетку weights только на
calibration, отдельно публикует baseline/selected metrics и срезы по языкам и
группам. Выбор не изменяет defaults. Recall, MRR и precision на этом малом
корпусе не являются вероятностью правильности на production данных.

См. [план](plans/M09_deterministic_mapper.md),
[ADR](adr/0019-deterministic-mapping-candidates.md) и
[приёмку](plans/M09_acceptance.md) и [security review](plans/M09_security_review.md).

[spec-catalog]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#10-database-semantic-catalog
[spec-mapping]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#11-механизм-автоматического-сопоставления
[spec-confidence]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#12-оценка-достоверности
[spec-relations]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#13-определение-сущностей-и-отношений
[spec-plan]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#14-mapping-plan
[spec-m9]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m9-deterministic-db-mapper
