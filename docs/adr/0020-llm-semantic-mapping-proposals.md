# ADR 0020 — Semantic proposals поверх M6–M9

Статус: принято для semantic database mapper. Дата: 2026-09-11.

## Контекст

M9 выдаёт field→column candidates и объяснимые base scores; M6 уже предоставляет
provider port, strict response registry и policy-aware router. Текущий MappingPlan
не выражает entity split/relations и требует load operation. Новый пользовательский
scope включает split по связанным таблицам, хотя первоначальный план его откладывал.

## Решение

Отдельный `LLMSemanticMapper` потребляет проверенные M7/M8 snapshots через M9,
строит bounded groups и вызывает существующий router. Нового provider protocol
нет; lifecycle и общий run budget принадлежат composition root/M6. Deterministic
modules сохраняют чистые dependencies; только перечисленные semantic modules
имеют доступ к LLM/security ports. Общий active-content veto перенесён из
ParsePlan compiler в `llm._content`, прежний импорт совместим.

Required-only `SemanticMappingDecision` содержит только opaque source/candidate
IDs, ограниченные assessments, закрытые reason codes и explicit ambiguity.
Модель может выбрать несколько target tables одного entity. Такой split требует
selected fields во всех таблицах и связности по выбранным FK. Relation context
разделяется по entity/target-table pairs: цепочка может выбрать несколько FK,
каждый composite key проверяется целиком с source-pair evidence. Unlisted IDs,
SQL/code/commands и несогласованные assignments отклоняются, partial decision нет.

Результат — proposal с immutable lookup, M9 lineage, отдельным SDK confidence,
provider/model/call metadata и prompt fingerprint. Он не изменяет normalized
entities и не создаёт MappingPlan. Альтернатива с расширением MappingPlan сейчас
потребовала бы wire migration и преждевременного выбора load operation; отложена.

Prompt — отдельная проекция retained candidates, не serialization full catalog
или profile. Raw examples/extrema/SQL expressions отсутствуют. Локальная
минимизация дополняется обязательным trusted scanner exact payload; masking не
понижает classification. Production DLP не заявляется. Policy-aware fallback и
ошибки M6 сохраняются; no_llm возвращает отдельный disabled outcome без calls.

Confidence использует M9 base и ограниченный LLM weight (default 0.20, ceiling
0.30), deterministic blockers и conservative hidden-competitor bound. Table/relation
scores не добавляются повторно к field score. Минимум обязательных choices и
неполный FK/PII evidence могут оставить даже корректный split в NEEDS_REVIEW.

Policy/confidence завершены явными `auto/confirm/reject` на choice/group/run.
SDK вычитает независимые ambiguity, validation и security penalties; warning
разрешающего scan сохраняет review и fingerprint approval. Численные штрафы
не заменяют veto schema/scope/security/type/FK. Пороги и penalties включены
в options fingerprint, а response schema модели остаётся `1.0.0`.

Retention поддерживает `validated_decision` (совместимый default) и
`metadata_only`: raw response не хранится в обоих режимах. До возврата результата
оба режима проверяют полное решение и cross-group assignment. В M10
нет постоянного response store. Source examples маскируются необратимо;
обратная подстановка не нужна для ответа из IDs и потому не реализуется.
M6 classification/redaction/policy bindings сохраняются при каждом fallback.

## Последствия

Публичные M6–M9 DTO, MappingCandidate и MappingPlan wire formats не меняются;
production dependencies и DB operations не добавлены. Валидация ответа модели
остаётся обязательной также для Fake/custom providers без native schema support.
Ошибки provider/security/schema возвращаются typed exceptions; safe attempt history
доступна через router. Это конкретизация первоначального плана с group outcomes.

Проверка grants, существования parent rows, generated-key propagation, cycle
strategy и freshness живой БД остаётся следующему этапу. Join paths поддерживаются
только как явно покрытые цепочки FK; M7 join hint сам по себе не создаёт отсутствующие
source anchors. `COMPLETED` подтверждает только завершение proposal, не готовность
импорта. См. [API и ограничения](../llm-database-mapping.md).
