# ADR 0025: physical replay и итоговый ValidationReport

- Статус: принято в рамках авторизованной реализации M12-D.
- Дата: 2026-09-12.
- Основание: [план M12](../plans/M12_validation_engine.md),
  [ADR 0022](0022-conservative-normalization-and-validation.md).

## Решение

`ProvenanceValidator` принимает завершённые immutable physical/normalized snapshots,
проверенный ParsePlan и его execution context. Он повторно использует M5
`ParsePlanExecutor`, включая проверку manifests, profile, selectors, EOF и cleanup.
Второго интерпретатора ParsePlan нет. JSON pointers, XPath, CSS selectors и document
locations сравниваются как данные; файлы, URLs и selectors не открываются.

Проверяются source ID и fingerprint, extraction/plan lineage, структура records/entities,
существование refs, raw origins, locations/spans и selection trace. Source/context
задаёт доверенный владелец snapshot: согласованно подделанные bytes и все их hashes
без внешнего trusted anchor не могут быть распознаны библиотекой.

Normalization остаётся sidecar к исходному `NormalizedValue` согласно M12-A.
Владелец задаёт bindings semantic field → steps/policy и immutable registry snapshot.
Только после physical replay выполняется повтор normalization; сравнивается вся
цепочка, включая no-op/failed steps. Самосогласованный trace не доказывает результат.
Исходный normalized artifact и legacy MappingPlan не изменяются.

`DetailedValidationReport` наследует существующий `ValidationReport`, добавляет
schema 1.1, named lineage, evidence references, coverage и ordered findings.
Legacy schema 1.0 и её canonical JSON остаются прежними. Scalar copies в отчёт
не входят; полный JSON всё равно sensitive из-за locations, IDs, paths и hashes.
`safe_summary()` использует закрытые code buckets и счётчики, исключая даже
пользовательские issue codes, имена и fingerprints. `repr` подробного отчёта пуст.

`ValidationReportBuilder` объединяет результаты trusted composition для точного
`lineage.input_fingerprint`. Required/completed layers явные; непроверенный уровень
блокирует acceptance. Builder не выдаёт разрешение на загрузку и не доказывает
projection, MappingPlan/catalog binding из произвольного внешнего DTO.

## Граница поставки и последствия

Текущая задача поставляет provenance, normalization replay и итоговую агрегацию.
Автоматическая projection NormalizedBatch → schema instance/ValidationDataset,
перепривязка MappingPlan после normalization и сквозной ingest coordinator из
общего M12 остаются отдельной интеграцией. Это уточняет объём раздела D плана;
непроверенные уровни явно видны в отчёте и не получают ложный pass.

Snapshots имеют общий intake budget; превышение лимитов даёт typed отказ без
частичного report. Проверяемые расхождения собираются все, до лимита issues.
Отмена распространяется после закрытия replay iterator. Поздний physical отказ
сбрасывает provisional verified evidence. Custom normalizers — доверенный pure
Python код composition owner; registry fingerprint связывает ID/version,
но не удостоверяет исходный код реализации и не является OS sandbox.
