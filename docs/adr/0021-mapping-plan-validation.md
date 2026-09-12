# ADR 0021 — независимая проверка декларативного MappingPlan

Статус: принято для M11. Дата: 2026-09-12.

## Контекст

MappingPlan 1.0.0 не содержит identity/relation descriptors. Constructor
`MappingPlanValidationRequest` отклоняет первый неизвестный ref или lineage
mismatch, поэтому не подходит для полного отчёта. Catalog-v1 исключает из schema
hash writable flags, target и policy; совпадение hash не разрешает запись.

## Решение

`mapping.MappingPlanValidator` реализует async protocol и принимает plan, manifest,
catalog и optional M8 profile раздельно. Trusted policy/options передаются явно
при создании. Constructor прежнего request сохранён и дополнительно проверяется
после успешного прохода правил. Domain не импортирует validator или adapters.

MappingPlan 1.1.0 дополняется закрытыми `MappingIdentity` и `MappingRelation`.
В 1.0.0 эти поля отсутствуют в serialization и content fingerprint; использование
descriptors требует явной смены версии. Новые поля `evidence` в result/wrapper
опциональны для старых snapshots, но всегда присутствуют в result M11.

Typed и JSON входы используют общий accumulator. Обычный результат сохраняет
`MappingPlanValidationResult`; повреждённый plan возвращает отдельный
`MappingPlanInputReport`, не выдумывая обязательные lineage fingerprints.
`complete` последнего относится только к intake. Некорректный trusted snapshot
и превышение budgets дают безопасный typed `ValidationError`. Читаемые
независимые элементы продолжают проверяться после ошибки соседнего элемента.

Закрытый tuple правил фиксирует codes и порядок. Locations содержат только секцию
и индексы. Result hash связывает plan, фактический manifest/catalog, profile,
policy/options, writable projection, resolved identities/order и ordered issues.
Время UTC исключено из hash. Данные и credentials в issues не попадают; полная
serialization результата с plan/bindings остаётся чувствительной.

Allow schemas/tables и exact column scope пересекаются; deny сильнее write/read
allow, включая parent lookup. System schemas и generated/non-writable targets
запрещены. Identifiers разрешаются по каталогу и никогда не интерполируются в SQL.
Неперечисленное имя с SQL fragments не разрешается; странное, но точное имя
отражённого разрешённого объекта остаётся инертной metadata.

Upsert требует полного подтверждённого PK/безусловного unique key. Не подходят
nullable keys, partial/expression/invalid indexes и deferred arbiters. Source PK
требует отдельного разрешения. Natural key дополнительно должен быть настроен
в policy и обеспечен реальным constraint. Неудачный explicit key не заменяется
другим ключом. DB-generated identity поддерживается только для insert.

FK descriptors проверяются целиком. Source values/lookup требуют разрешённых
read refs, mapped parent — exact source/target pairs. Наличие parent rows,
значения ключей и корректность relationships проверяются при load. Порядок
строится для выбранных записей общим алгоритмом M7; циклы вне плана не мешают.
Циклы внутри плана, generated-key propagation и deferred/two-phase strategies
дают контролируемый отказ.

## Последствия

`ACCEPTED` означает структурную пригодность декларации. Unknown representation,
arrays/JSON/time/binary без достаточного evidence не получают auto acceptance;
преобразования должны завершиться до mapping validation. Enum labels, range,
precision, CHECK, UNIQUE, реальные FK и provenance остаются record/load gates.
Threshold проверяется включительно по plan и каждому field; ниже threshold —
REJECTED. Неоднозначная identity или недоказанный type дают NEEDS_REVIEW без wrapper.

Fresh inspection, grants, отдельный writer, staging, dry-run, rollback,
идемпотентность и защита TOCTOU обязательны перед исполнением. Validator не
предоставляет SQL/DB/LLM handles. Facade и loader остаются отдельной работой.
Production dependencies и пользовательская схема БД не меняются.

Альтернативы: ослабить прежний request — нарушает существующие DTO гарантии;
доверять M9/M10 scores — не проверяет safety predicates; разрешить raw expressions
для удобства — расширяет доверенную границу. Выбраны отдельный вход сервиса,
закрытая декларация и независимые детерминированные проверки.

Связано: [план M11](../plans/M11_mapping_plan_validator.md),
[API валидатора](../mapping-plan-validation.md),
[catalog-v1](0017-canonical-catalog-and-dependency-graph.md).
