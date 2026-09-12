# Проверка provenance и итоговый ValidationReport

`ProvenanceValidator` сверяет завершённые snapshots с повторным выполнением
ParsePlan. Поддельный pointer с корректно пересчитанным hash получает issue.
Ненулевое значение без обязательных origins получает `PROVENANCE_REQUIRED`.
Данные и normalization sidecars остаются неизменными.

Ниже функция для результата M5 executor; `context` должен быть тем же, с которым
создавались IDs normalized records. `generated_at` передаётся явно в UTC.

<!-- example:m12-provenance:start -->
```python
from datetime import datetime

from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.parsing import ParseExecutionContext, ValidatedParsePlan
from structuraguard.contracts.provenance import DetailedValidationReport
from structuraguard.contracts.source import ExtractedBatch
from structuraguard.validation import ProvenanceValidator


async def validate_snapshot(
    normalized: tuple[NormalizedBatch, ...],
    physical: tuple[ExtractedBatch, ...],
    plan: ValidatedParsePlan,
    context: ParseExecutionContext,
    generated_at: datetime,
) -> DetailedValidationReport:
    report = await ProvenanceValidator().validate(
        normalized,
        source_batches=physical,
        plan=plan,
        context=context,
        generated_at=generated_at,
    )
    # Только эта сводка предназначена для обычных logs/telemetry.
    print(report.safe_summary().model_dump_json())
    return report
```
<!-- example:m12-provenance:end -->

`ACCEPTED` этого примера подтверждает только provenance и настроенную нормализацию.
Чтобы потребовать также DB validation, объявите этот уровень при сборке отчёта.
Пример ниже намеренно не передаёт результат DB: исходный успешный replay сохраняется,
но новый отчёт получает `NEEDS_REVIEW`, `complete=False` и `VALIDATION_LAYER_UNVERIFIED`.

<!-- example:m12-report:start -->
```python
from structuraguard.contracts.provenance import DetailedValidationReport, ValidationLayer
from structuraguard.validation import ValidationReportBuilder


def require_database_check(provenance: DetailedValidationReport) -> DetailedValidationReport:
    return ValidationReportBuilder(
        required_layers=(ValidationLayer.DATABASE,),
    ).combine(provenance)
```
<!-- example:m12-report:end -->

Для завершения проверки caller отдельно запускает DB validator и связывает его
результат с исходными ordinals и `lineage.input_fingerprint`. Builder принимает
доверенные `ValidationLayerResult`; он не проверяет честность такой projection.

## Evidence и нормализация

Сначала проверяются форма DTO, общие budgets, manifests, EOF и source lineage.
Затем выполняется physical replay: references должны существовать, locations/spans,
исходные значения и selection trace должны точно соответствовать ParsePlan.
Проверка охватывает все значения, включая unmapped fields, и сохраняет ordinals
records/entities/values независимо от normalized batch cuts.

Для normalization передайте `ProvenancePolicy(normalizations=(...))` с
`NormalizationBinding(field=SemanticFieldRef(...), steps=(NormalizerSpec(...),),
policy=NormalizationPolicy(...))`, frozen registry в constructor и tuple результатов
в `validate(normalizations=...)`. Цепочка и locale policy выбираются владельцем;
из claimed trace нельзя выбрать другую policy или реализацию. После доказательства
physical value повторно выполняется полная цепочка registry и сравнивается её trace.
Отсутствующий trace блокирует acceptance. Ошибка normalizer сохраняет raw/input
и появляется как `NORMALIZATION_FAILED`; детализация доступна по sidecar reference.

Каждый `report.evidence` содержит:

- ordinals, value ID и признак required/verified;
- `raw_reference`: fingerprint исходного normalized batch и JSON pointer к raw scalar;
- `normalized_reference`: fingerprint/pointer исходного значения или результата normalization;
- заявленные physical refs, locations и spans. При `verified=False` эти locations
  нельзя считать доказательством.

Ошибочный snapshot может повторять заявленный `value_id`. Rejected report сохраняет
такие непроверенные evidence по отдельным ordinals и artifact pointers, чтобы
диагностика оставалась доступной. Подтверждённые value IDs должны быть уникальны;
повторный ID не может получить `verified=True` или попасть в ACCEPTED report.

Scalar values и сами transformation chains не копируются в отчёт: они доступны
по immutable references. Полный JSON может содержать restricted имена, locations
и hashes; храните его с теми же правами, что artifacts. `safe_summary()` исключает
такие данные и заменяет произвольные внешние issue codes на `VALIDATION_ISSUE`.
Hash не является обезличиванием.

## Итоговая агрегация

`DetailedValidationReport` является `ValidationReport` schema 1.1; legacy 1.0 не
изменён. `ValidationReportBuilder(required_layers=(...)).combine(report, layers=(...))`
объединяет provenance report с `ValidationLayerResult` для JSON Schema, DB и rules.
Каждый результат содержит точный `report.lineage.input_fingerprint`, fingerprints
evidence слоя, признак complete и findings. Результаты одного слоя нельзя передать
дважды. Внешний owner проверяет projection, MappingPlan/catalog/policy и собственные
input fingerprints валидаторов A–C прежде, чем создавать layer result.
Сериализованный DTO не является разрешением выполнять DB reads или загрузку.

Внешние findings относятся к ordinals полного normalized snapshot; JSON paths
сохраняются в подробном отчёте. Несуществующие ordinals отклоняются. Уровни
отчёта упорядочены: normalization → JSON Schema → database → business rules → provenance.
Внутри: record → entity → value → check index → code, затем deterministic tie-break.
Одинаковые findings дедуплицируются; разные locations остаются отдельными issues.
Перестановки layer results/findings не меняют итоговый evidence hash.

`complete` означает завершение всех указанных проверок, а не успешность данных.
Error означает `REJECTED`; отсутствие доказательства или непроверенный required
layer — `NEEDS_REVIEW`; `ACCEPTED` допускается только без этих blockers.
Проверка только provenance/normalization не утверждает, что schema/DB/rules пройдены.

`total_records = valid_records + invalid_records + unresolved_records`.
Запись учитывается один раз; error имеет приоритет над unresolved.
`warning_records` пересекается с этими категориями. Глобальный issue применяется
ко всему snapshot. `required_values`/`verified_values` дают coverage обязательных
значений; при нулевом знаменателе coverage неприменима, не «100%».
`safe_summary().issue_counts` содержит числа issues по безопасным code/severity.

`evidence_fingerprint` включает named lineage, policy/registry, sidecars, результаты
слоёв и упорядоченные findings/references. Policy fingerprint также включает
ParsePlanOptions и validation fingerprint проверенного ParsePlan. `generated_at` исключён. Исходные hashes
batch artifacts закономерно меняются при другой нарезке batches.

## Отказы и repair boundaries

| Code | Значение |
|---|---|
| `PROVENANCE_REQUIRED` | Нет обязательных origins/selection evidence; needs review |
| `PROVENANCE_SOURCE_MISMATCH` | Чужой source ID или fingerprint |
| `PROVENANCE_REF_UNKNOWN` | Reference не существует в physical snapshot |
| `PROVENANCE_LOCATION_MISMATCH` | Подменены location, spans или связь origin/ref |
| `PROVENANCE_RAW_MISMATCH` | Raw value не соответствует replay |
| `PROVENANCE_SELECTION_MISMATCH` | Подменены selector, выбранное значение или refs |
| `PROVENANCE_STRUCTURE_MISMATCH` | Иная структура/lineage/число records |
| `PROVENANCE_STREAM_INVALID` | Нарушены normalized EOF, global IDs или manifest |
| `PROVENANCE_REPLAY_FAILED` | Physical replay не завершён; provisional evidence сброшено |
| `NORMALIZATION_EVIDENCE_MISSING` | Нет trace для обязательного binding |
| `NORMALIZATION_EVIDENCE_MISMATCH` | Trace/policy/registry не совпадают с replay |
| `NORMALIZATION_EVIDENCE_UNEXPECTED` | Неизвестный, повторный или неразрешённый sidecar/binding |
| `NORMALIZATION_PROVENANCE_UNVERIFIED` | Physical evidence не позволяет запускать normalization |
| `NORMALIZATION_FAILED` | Воспроизведённая цепочка вернула отказ |
| `VALIDATION_LAYER_UNVERIFIED` | Required layer не завершён |

Malformed DTO, subclass serializer, неизвестный тип, cycles или превышение
`ProvenanceLimits` дают `ValidationError` (`PROVENANCE_INPUT_INVALID` либо
`SECURITY_LIMIT_EXCEEDED`) до опасной serialization. Budget общий для snapshot;
ограничены batches/records/values/issues, nodes/depth/bytes и scalar sizes.
Действуют также controls M5 executor. Это конечные budgets, не process RSS quota.

Настроенные normalization bindings требуют registry:
`NORMALIZATION_REGISTRY_REQUIRED` / `NORMALIZATION_REGISTRY_INVALID` — ошибки
конфигурации. Builder отклоняет повторные layers или уже объединённый report с
`VALIDATION_REPORT_SCOPE_INVALID`, иной input fingerprint — с
`VALIDATION_REPORT_INPUT_MISMATCH`. Это SDK `ValidationError`, без частичного pass.

Locations не открываются; validator не читает файлы, environment, URL или БД.
Cancellation распространяется после cleanup без успешного report. Готовый report
не исправляет значения, source refs, MappingPlan или catalog. Для исправления
нужен новый корректный artifact/plan и повтор проверок. Custom normalizers остаются
доверенным pure кодом владельца; OS sandbox для него не создаётся.

[Архитектурное решение](adr/0025-provenance-replay-and-validation-report.md),
[матрица приёмки](plans/M12_D_provenance_acceptance.md).
Канонические требования: [ТЗ, §16.6 «Provenance validation»](https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#166-provenance-validation).
