# ADR 0023 — локальный bounded профиль JSON Schema

Статус: принято. Дата: 2026-09-12.

## Контекст

Задача M12-B требует meta-validation, локальные refs, all-errors paths и finite
controls без network. Полный JSON Schema допускает dynamic resolution и regex,
стоимость которых нельзя ограничить обычным async timeout. SDK должен явно
отказывать за пределами проверенного профиля, не менять значения и не смешивать
schema report с подтверждением physical provenance.

## Решение

- Отдельный async `JsonSchemaValidator` принимает caller-projected JSON и
  возвращает immutable `JsonSchemaResult`; старые DTO/MappingPlan не меняются.
  Issues переиспользуют `ValidationIssue` без physical refs и добавляют paths.
- Использовать `Draft202012Validator` и `referencing.Registry` с bundled meta-schemas
  и без retrieval. Schema и instance ограничиваются и копируются до traversal.
  В adapter-local `Any` остаются только нетипизированные части upstream API.
- Default refs — локальные fragments. Дополнительные resources имеют явные
  approved URNs и передаются локально. Remote/file/relative refs запрещены;
  nested IDs, dynamic refs/anchors, custom vocabularies и unknown drafts/keywords
  отклоняются. Проверяются неиспользованные definitions и target schema positions.
- Проверять циклы по edges без перехода к дочернему instance до evaluation.
  Guarded recursive trees допускаются с runtime depth/work budget. Regex —
  закрытое линейное подмножество; неподдержанное получает явный code.
- После meta-validation удалять `$schema` из owned execution copies: upstream
  `evolve` иначе выбирает stock class и обходит bounded keyword callbacks.
  Новые классы не регистрируются в global validator registry.
- Exact Decimal/integer semantics и рациональный `multipleOf` не зависят от
  Decimal context. Defaults/formats остаются annotations; repairs отсутствуют.
- Cache принадлежит service instance: LRU имеет caps entries/bytes. Fingerprint
  включает root/resources с type tags, различающими число и одноимённую строку.
  Report bytes/issues также ограничены; budget failure не даёт partial acceptance.

## Последствия и альтернативы

`jsonschema`/`referencing` становятся прямыми runtime dependencies; MIT, Python
compatibility, lock, import smoke и distribution inventory проверяются. SDK import
не импортирует backend; composition загружает bundled schema data при создании
validator, subsequent validation не делает I/O. Произвольные remote resources,
dynamic scope, regex и vocabulary support не включаются скрытым fallback.

Альтернатива — полный custom evaluator или arbitrary regex с timeout — увеличивает
объём собственного security-critical кода либо не прерывает CPU-bound работу.
Выбран ограниченный профиль стандартного evaluator и явные unsupported outcomes.
Он не обещает полную поддержку всех optional Draft 2020-12 features, OS/RSS
isolation или возможность разделять один mutable cache между OS threads.

Batch projection и record-level engine из [ADR 0022](0022-conservative-normalization-and-validation.md)
по-прежнему требуют отдельной интеграции. [API и точные controls](../json-schema-validation.md).
