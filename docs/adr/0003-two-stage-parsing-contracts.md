# ADR 0003. Двухэтапная модель parsing

Статус: принято для M2.

Дата: 2026-09-03.

## Контекст

Одно внутреннее представление не позволяет одновременно сохранить физическую
структуру источника и назначить проверяемый бизнес-смысл. Если format parser
сразу создаёт сущности, downstream зависит от особенностей CSV, JSON, PDF или
DOCX. Если LLM output применяется напрямую, недоверенный ответ получает право
на преобразование данных. `ParsePlan` также нельзя смешивать с `MappingPlan`:
первый интерпретирует источник, второй связывает уже нормализованные поля с
отражённым каталогом БД.

## Решение

- `Parser` выполняет только technical parsing и потоково возвращает
  `ExtractedBatch` с raw values, physical structure и provenance.
- `SemanticStructureAnalyzer` анализирует fingerprint-bound manifest/profile и
  bounded `PhysicalSample`, после чего возвращает `ParsePlan`, review outcome
  либо отказ. Каждый вариант plan использует закрытые typed selectors и entity
  grouping; произвольные regex, code и callbacks не являются частью schema.
- `ParsePlanValidator` проверяет discriminated plan, lineage и существование
  всех physical references. Только `ValidatedParsePlan` допускается в
  `ParsePlanExecutor`.
- `ParsePlanExecutor` детерминированно создаёт `NormalizedBatch`. Повторный
  LLM-вызов при исполнении plan запрещён.
- `MappingPlan` использует только semantic и catalog references. SQL, physical
  selectors, callbacks и executable content в нём отсутствуют. DB adapter
  принимает только `ValidatedMappingPlan`, связанный с `target_id`, и формирует
  SQL самостоятельно.
- LLM MAY предложить строго структурированный `ParsePlan` или mapping candidate
  из bounded, classified и masked context. Ответ не имеет tools, credentials,
  source/DB handles или права на execution и всегда проходит независимую
  validation. Этим решением соответствующее ограничение ADR 0002 расширяется с
  mapping candidates на semantic parsing без расширения полномочий модели.
- Persisted DTO immutable, сериализуются канонически и связываются цепочкой
  source → extraction → profile → parse plan → normalized dataset → database
  catalog → mapping plan fingerprints. Batch fingerprint не подменяет aggregate
  fingerprint.
- Каждый batch в manifest имеет lineage-aware summary. Одно-проходная
  `validate_batches()` проверяет порядок, terminal marker, counts и глобальную
  уникальность normalized IDs; selective physical source index ограничен
  10 000 references и не дублирует raw dataset.
- SHA-256 fingerprints имеют единственную persisted форму
  `sha256:<64 lowercase hex>`; bare digest нормализуется при validation, поэтому
  две текстовые формы не обходят duplicate/equality checks.
- Persisted aggregates и reports содержат `schema_version` и
  `ProducerMetadata`; validation outcomes сохраняют полную fingerprint chain и
  не допускают отказ без typed issues.
- LLM-вызов требует typed `SecurityApproval`: разрешающий `SecurityReport` и
  его canonical fingerprint. Request повторно связывает security evidence с
  точными payload, classification, routing и redaction fingerprints.
- `ValidationIssue` сохраняет stable `code`/`message_key` вместо free-form text.
  Physical refs допустимы только при наличии manifest/profile allowlist; reports
  и mapping outcomes без такой границы их отклоняют.
  audit IDs используют точные формы `event-<UUID|ULID>` и
  `run-<UUID|ULID>`, а terminal event связывает конкретный
  allowed/blocked security report без temporal inversion. `LoadReport` именует
  роли всей execution
  fingerprint chain и `target_id`, а не полагается на безымянный set.
- `contracts` содержит только DTO и wire vocabularies, `domain` — чистые
  canonical/lineage операции, `ports` — adapter protocols. Эти слои не
  импортируют infrastructure или facade.
- Корневые exports M1 не меняются. M2 публикуется через
  `structuraguard.contracts` и `structuraguard.ports`.

## Последствия

Pipeline получает дополнительную validation boundary и больше persisted
metadata, зато format adapters взаимозаменяемы, provenance проверяем, а LLM и
непроверенные plans не получают authority. Реализации format parsers,
analyzers, executors, DB reflection/load и LLM providers остаются отдельными
milestones и обязаны соблюдать зафиксированные protocols.

Связанные документы: [ADR 0001](0001-public-api-and-run-policies.md),
[ADR 0002](0002-security-boundary-defaults.md),
[архитектура](../architecture.md), [публичный API](../public-api.md),
[канонический раздел M2 ТЗ][spec-m2].

[spec-m2]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m2-доменные-модели-и-contracts
