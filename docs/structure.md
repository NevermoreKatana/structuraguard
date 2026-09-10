# Структурный разбор M5

Пример преобразует небольшой CSV в `NormalizedBatch` через deterministic analyzer,
независимую проверку ParsePlan и executor. Нужен установленный `structuraguard`
текущего checkout; extras, LLM, сеть и БД не нужны. Это отдельные компоненты M5:
facade `ingest` ещё не соединяет их в pipeline.

## Копируемый пример

<!-- example:m05-normalization:start -->
```python
import asyncio
import hashlib
from collections.abc import AsyncIterator

from structuraguard.contracts import ExtractedBatch, SourceArtifact
from structuraguard.contracts.parsing import (
    ParseExecutionContext,
    ParsePlanValidationRequest,
    StructurePlanCreated,
)
from structuraguard.parsers.builtin import DelimitedTextParser
from structuraguard.ports.source import BatchOptions, ParseContext
from structuraguard.structure import (
    DeterministicStructureAnalyzer,
    ParsePlanExecutor,
    ParsePlanValidator,
)


class MemoryReader:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.source_fingerprint = "sha256:" + hashlib.sha256(content).hexdigest()

    async def read(self, *, offset: int, size: int) -> bytes:
        return self.content[offset : offset + size]


async def main() -> None:
    reader = MemoryReader(b"name,n\nAda,1\nBob,2\n")
    source = SourceArtifact(
        artifact_id="demo-csv",
        display_name="demo.csv",
        media_type="text/csv",
        size_bytes=len(reader.content),
        source_fingerprint=reader.source_fingerprint,
    )
    context = ParseContext(
        reader=reader,
        source_fingerprint=source.source_fingerprint,
        max_bytes=1024,
        max_records=10,
        max_nesting_depth=8,
        batch_options=BatchOptions(batch_size=2, max_batches=10),
    )
    extracted = [batch async for batch in DelimitedTextParser().parse(source, context)]
    manifest = extracted[-1].manifest
    assert manifest is not None

    async def replay() -> AsyncIterator[ExtractedBatch]:
        for batch in extracted:
            yield batch

    analysis = await DeterministicStructureAnalyzer().analyze(replay())
    if not isinstance(analysis, StructurePlanCreated):
        raise RuntimeError(analysis.kind)
    request = ParsePlanValidationRequest(
        source=manifest.source, manifest=manifest, profile=analysis.profile, plan=analysis.plan,
    )
    validation = await ParsePlanValidator().validate_source(request, replay())
    if validation.validated_plan is None:
        raise RuntimeError(tuple(issue.code for issue in validation.issues))
    execution = ParseExecutionContext(
        run_id="demo-run",
        source_fingerprint=source.source_fingerprint,
        extraction_fingerprint=manifest.extraction_fingerprint,
        parse_plan_fingerprint=analysis.plan.fingerprint,
        manifest=manifest,
        profile=analysis.profile,
        max_records_per_batch=1,
    )
    output = [batch async for batch in ParsePlanExecutor().execute(
        replay(), validation.validated_plan, execution,
    )]
    normalized = output[-1].manifest
    assert normalized is not None
    normalized.validate_batches(output)
    records = [record for batch in output for record in batch.records]
    values = [value for record in records for entity in record.entities for value in entity.values]
    assert [value.raw_value.value for value in values] == ["Ada", "1", "Bob", "2"]
    assert all(value.origins and value.selection for value in values)
    assert all(value.semantic_type == "unresolved" for value in values)
    print(f"{len(records)} records; provenance preserved; semantics unresolved")


asyncio.run(main())
```
<!-- example:m05-normalization:end -->

Ожидаемый вывод:

```text
2 records; provenance preserved; semantics unresolved
```

В этом малом примере списки удерживают extraction/output для replay и assertions.
Для большого источника caller предоставляет повторяемый неизменяемый snapshot
или собственное хранилище batches и обрабатывает результат по одному batch.
Готовый replay/staging backend в M5 не реализован. Каждый проход требует нового
iterator с теми же physical IDs, порядком, batch boundaries и fingerprints;
уже потреблённый iterator повторно не используется.

## Результаты и ограничения

Analyzer сам вызывает profiler. Для отдельного просмотра evidence доступен
`await StructuralProfiler().profile(replay())`. Профиль ограничивает retained
samples и отражает пропуски в `coverage`; он не гарантирует готовый plan.

| Результат analyzer | Действие вызывающего кода |
| --- | --- |
| `plan_created` | Передать plan, **derived** `analysis.profile` и manifest validator; затем передать тот же profile executor |
| `needs_review` | Рассмотреть ranked candidates; SDK не выбирает первый автоматически |
| `needs_semantic_analysis` | Правил, confidence или покрытия недостаточно; LLM автоматически не вызывается |
| `rejected` | Исправить неподдержанный mode; доступен только `deterministic` |

Confidence — воспроизводимый score, не вероятность. Порог по умолчанию `0.85`
включительно; снижение порога не разрешает ambiguous candidates или sampling gaps.
Большой источник можно профилировать bounded способом, но неполное покрытие
запрещает automatic plan. Явный tabular plan может охватывать больше samples,
если validator/executor проверяют весь его конечный scope и соблюдены limits.

Планы поддерживают tabular rows, literal tree paths и parent/child collections,
конечные LOG/document record groups. Optional tree fields, XML element matching,
неоднозначные ragged/merged regions требуют отдельной policy. Несколько JSONL
roots, параллельные tree/document scopes HTML и неподтверждённые участки не
сворачиваются в автоматическое решение. Semantic meaning остаётся `unresolved`;
primitive type hints не преобразуют строки в числа, деньги или даты.

## Ошибки и security semantics

- Profiler/analyzer потребляют и закрывают входной iterator при наличии close-hook.
  `StructuralProfilingError`/`StructuralAnalysisError` содержат
  `STRUCTURE_INPUT_INVALID` или `PROCESSING_TIMEOUT`. Ошибки source очищаются;
  категории `ParserError`/`SecurityPolicyError` и известные codes сохраняются.
- Validator возвращает typed `REJECTED` и `issues` при неверном plan, source,
  limit или source error. Без physical replay это `PARSE_PLAN_REPLAY_REQUIRED`.
  `ValidatedParsePlan` сериализуем и не является разрешением обойти проверки.
- Executor повторно проверяет wrapper/context/source. `ParseExecutionError.issue`
  содержит code, stage, reason и `emitted_batches`, без raw exception text.
  Cancellation распространяется; при раннем выходе consumer вызывает `aclose()`.
- Non-terminal output предварителен. Успех подтверждается только terminal manifest
  после EOF, проверки refs и успешного cleanup. Поздняя ошибка требует отката
  предварительных результатов в downstream staging; executor не пишет в БД.
- Record items/bytes ограничиваются при накоплении, включая child collections.
  Input, samples, plan, output и время имеют отдельные budgets. Sync replay timeout
  cooperative: он не прерывает блокирующий пользовательский `next()`.
- SQL, Python, shell, callbacks и пользовательские regex не входят в grammar.
  Отдельные `ParseRule`, identity и conversions явно отклоняются. Parser sandbox,
  LLM routing и DB allowlist не заменяются структурным анализом.
- Raw values, labels и provenance могут содержать PII. Их нельзя безусловно
  отправлять в logs/LLM. Hashes schema `1.1.0` проверяют согласованность payload,
  но не удостоверяют подлинность внешнего источника.

Подробности параметров и совместимости schema `1.0.0`/`1.1.0`:
[публичный API](public-api.md#parseplanvalidator-parseplanexecutor-m5-c),
[security semantics](security.md), [ADR 0010](adr/0010-verified-parse-plan-execution.md).
Канонические требования: [FR-014][spec-fr-014], [M5][spec-m5] и
[FR-012.2][spec-normalized]. [Матрица приёмки](plans/M05_acceptance.md)
отделяет проверенное поведение от отложенных возможностей плана.

[spec-fr-014]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#fr-014-structural-profiling-и-parseplan
[spec-m5]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m5-structural-profiler-и-deterministic-parseplan
[spec-normalized]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#fr-0122-normalized-data-model
