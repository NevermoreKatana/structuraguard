# Semantic parsing M6-D

`SemanticParsingSession` получает replay проверяемого physical extraction M4,
строит plan через `HybridStructureAnalyzer`, проверяет его общим M5 validator
и применяет ко всему источнику общим executor. DB mapping не выполняется.

## Копируемый offline-пример {#m06-offline-example}

Пример превращает CSV в два semantic records без LLM и сети. Он работает из
установленного core package `structuraguard` без optional HTTP extra; вход
синтетический, ID и часы фиксированы. Проверки исполняют этот же блок из документа.

<!-- example:m06-session:start -->
```python
import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from structuraguard.contracts import (
    DataClassification, ExtractedBatch, LLMAnalysisContext, PipelineStatus,
    SemanticParsingMode, SourceArtifact,
)
from structuraguard.llm import NoLLMProvider
from structuraguard.parsers.builtin import DelimitedTextParser
from structuraguard.parsing import ParsingPolicy, SemanticParsingSession
from structuraguard.ports.source import ParseContext


async def main() -> None:
    content = b"name,n\nAda,1\nBob,2\n"
    fingerprint = "sha256:" + hashlib.sha256(content).hexdigest()
    fixed_time = datetime(2026, 9, 10, tzinfo=UTC)

    class Reader:
        source_fingerprint = fingerprint

        async def read(self, *, offset: int, size: int) -> bytes:
            return content[offset:offset + size]

    source = SourceArtifact(
        artifact_id="offline-example", display_name="example.csv",
        media_type="text/csv", size_bytes=len(content),
        source_fingerprint=fingerprint,
    )
    parse_context = ParseContext(
        reader=Reader(), source_fingerprint=fingerprint,
        max_bytes=len(content), max_records=10, max_nesting_depth=4,
    )
    batches = tuple([
        batch async for batch in DelimitedTextParser().parse(source, parse_context)
    ])

    async def replay() -> AsyncIterator[ExtractedBatch]:
        for batch in batches:
            yield batch

    context = LLMAnalysisContext(
        run_id="offline-semantic-example",
        data_classification=DataClassification.INTERNAL,
        routing_policy_id="offline", routing_policy_fingerprint=fingerprint,
        redaction_fingerprint=fingerprint,
    )
    async with SemanticParsingSession(
        replay=replay, context=context,
        policy=ParsingPolicy(mode=SemanticParsingMode.DETERMINISTIC),
        provider=NoLLMProvider(), clock=lambda: fixed_time, timer=lambda: 0,
    ) as session:
        output = [batch async for batch in session.parse_semantically()]
        assert output[-1].is_last
        assert sum(len(batch.records) for batch in output) == 2
        assert session.report is not None
        assert session.report.status is PipelineStatus.COMPLETED
        assert session.report.provenance_coverage == 1
        assert session.report.llm_calls == 0
        print("semantic session: 2 records; 0 LLM calls")


asyncio.run(main())
```
<!-- example:m06-session:end -->

Маленький immutable snapshot здесь хранится в памяти. Для больших источников
приложение предоставляет bounded replay из своего snapshot storage: каждый
stream возвращает те же M4 batches, IDs/fingerprints и terminal manifest.
Не нужно повторять extraction с новыми IDs. Session закрывает свои iterators;
provider/HTTP lifecycle принадлежит caller. Одну session используют последовательно
для одного execution; новый run требует новой session.

Для LLM сценария передайте конкретный `LLMProvider`, trusted `SecurityScanner` и
`LLMAnalysisContext`; оставьте default `ParsingPolicy()` либо выберите `llm_first`.
[Пример provider и approval](llm.md#m06-provider-example) поясняет egress boundary.
Fingerprint в context задаёт lineage, но не заменяет scanner approval.

Публичный пакет `structuraguard.parsing` экспортирует `ParsingPolicy`,
`SemanticConfidence`, `HybridStructureAnalyzer`, `SemanticParsingSession`.
`structuraguard.structure` также экспортирует подробный `HybridAnalysis` и factories
`document_prompt()` / `document_response_schema()`. Для HTTP adapter необходимо
явно зарегистрировать эти factories вместе со structural factories M6-C.

## Режимы {#modes}

| Mode | Поведение |
| --- | --- |
| `deterministic` | M5 и полный validator/executor; ноль LLM calls. Недостаточные правила дают `NEEDS_REVIEW` с `NEEDS_SEMANTIC_ANALYSIS`. |
| `llm_assisted` — default | Уверенный M5 table/tree/log plan применяется без LLM. Неоднозначный источник получает максимум один structural request. Prose/document extraction использует chunks: высокий structural score копирования абзаца не доказывает semantic fields. |
| `llm_first` | M5 evidence собирается всегда, затем выполняется structural request либо document chunk extraction. Сравнение с M5 и validator обязательны. |

`NoLLMProvider` и отсутствующий provider/scanner не дают неявного разрешения на
generation. Policy denial виден в issues, restricted data не отправляется cloud.
Session принимает конкретный provider; `PolicyAwareLLMRouter` M6-B остаётся отдельным
run coordinator, а не provider с вымышленными aggregate capabilities. Общая facade
ingest/state machine и автоматическая композиция router с session не входят в этот API.
Registry `structure_analyzers.register(...)` также отсутствует. Пустой подтверждённый
source в любом режиме даёт `NEEDS_REVIEW` с `SOURCE_EMPTY`: нет plan, records,
LLM calls; provenance coverage равен нулю.

## Plans и применение {#plans}

`analyze_structure()` возвращает `HybridAnalysis`: source/profile/manifest, проверенный
draft plan, assessment, issues и bounded unresolved refs. `create_parse_plan()`
возвращает этот draft либо `None`. Само наличие draft не означает completed dataset.
`validate_parse_plan(plan)` выполняет physical replay и не вызывает LLM.

`parse_semantically(plan=saved_plan)` позволяет повторное применение без generation
в новой session. Проверяются lineage, актуальные source spans и scope; прошлый score
не является разрешением. Для structural plans, отличающихся от доступного M5 plan,
повторный запуск сохраняет `NEEDS_REVIEW` до разрешения scope. Cross-source plans
отклоняются. Каждая session допускает одно execution.

Document draft с положительным `final_assessment.penalty` также сохраняет
`NEEDS_REVIEW` после serialization/replay: совпадение spans не разрешает исходную
неоднозначность. Поскольку точные blockers не входят в saved plan, весь доступный
document scope консервативно отмечается unresolved. Для подтверждения нужен новый
анализ с разрешённой неоднозначностью; отдельного manual approval API в M6 нет.
Повторная проверка saved plan имеет единый source deadline для M5 evidence,
validation и chunk coverage, включая cleanup.

Успешный terminal batch появляется после полного source replay и cleanup. До него
records предварительные. При `NEEDS_REVIEW` подтверждённые records остаются preview:
terminal manifest и normalized fingerprint отсутствуют. Downstream должен хранить
preview отдельно от подтверждённого dataset. Cancellation/раннее закрытие дают
`CANCELLED`; ошибки source/selection дают `FAILED` и typed exception. Если physical
snapshot ещё не удалось подтвердить, report отсутствует: fingerprints не выдумываются.
Сам по себе `analyze_structure()` report не создаёт; его формирует потребление
`parse_semantically()`. Если генерация отклонена как unsafe, возможен
`REJECTED_SECURITY` без подтверждённого dataset.
Consumer обязан исчерпать iterator либо вызвать `aclose()`/закрыть session.
Если анализ завершился exception или cancellation, session также закрывается для
новых операций: повторный `analyze_structure()` не может заново открыть LLM budget
того же run. Для нового запуска создаётся отдельная session.
При заполненном issue budget ошибки и отмена сохраняют bounded report: приоритет
имеют security veto, затем terminal причина. Если доступен только один слот и его
занимает veto, `FAILED`/`CANCELLED` остаётся видимым в status.

| Условие | Наблюдаемое поведение |
| --- | --- |
| Неоднозначность, неполный scope, конфликт entities, исчерпанный LLM budget | Bounded issues в analysis/report, `NEEDS_REVIEW`; подтверждённые records могут остаться preview |
| Нет разрешённого provider/scanner, restricted cloud | `LLM_POLICY_DENIED` в issues, без запрещённого egress |
| Неизвестны input/output/context token caps | `LLM_CAPABILITY_MISMATCH` до вызова |
| Подтверждённый usage превышает context cap | `LLM_CONTEXT_LIMIT`, без принятой generation |
| Source replay/cleanup failure или общий source deadline | Исключение source/`ParseExecutionError`/`TimeoutError`; `FAILED`, если snapshot подтверждён |
| Отмена или раннее закрытие parsing iterator | `CANCELLED`, если snapshot подтверждён; task cancellation распространяется как `asyncio.CancelledError` |
| Операция после закрытия session / повтор execution | `LLMProviderError` с `LLM_REQUEST_INVALID` / `LLM_POLICY_DENIED` |

Ошибки LLM обычно превращаются в issues Hybrid outcome; это отличается от прямого
`LLMStructureAnalyzer`, который распространяет typed provider/plan errors.
Ориентируйтесь на status и коды issues, а не только на наличие exception или records.

## Bounded samples и document chunks {#chunks}

- Structural request содержит до 100 физических значений из bounded prefix/tail,
  каталог M6-C и M5 observations/candidates. Source IDs заменяются локальными aliases.
  Oversized values пропускаются; selectors и scope затем проверяются полным replay.
  Табличные записи никогда не запускают individual generation; размер таблицы
  не увеличивает structural call count выше одного.
- Documents читаются в source order. Выбирается одно представление текста:
  block text; иначе вложенные PDF lines; иначе lines или scalar tree nodes.
  XML сохраняет XPath через origin, JSON структурные коллекции используют tree plan.
  Document tables требуют отдельного tabular scope и попадают в review.
- Один chunk содержит до 16 fragments и 4096 bytes сериализованных fragments;
  trusted envelope, prompt и JSON Schema дополнительно входят в provider limits.
  Большой текст режется по UTF-8 границам; offsets измеряются Unicode codepoints.
  Соседний chunk включает до одного overlap fragment. Полный raw документ не
  материализуется и не передаётся модели.
- Chunk fingerprint включает physical coordinates и текст. Exact repeats не
  вызываются повторно. Entity identity — hash anchor `(ref,start,end,quote hash)`.
  Одинаковые значения в разных местах остаются разными occurrences. Повтор anchor
  объединяет совместимые поля; конфликт type/parent/field spans удаляет entity из
  accepted plan и создаёт issue. Parent cycles/missing parents не создают orphans.
- Ответ содержит только strict entities, closed semantic types, anchors, fields,
  exact quotes и unresolved aliases. Произвольного generated value нет. Quote
  сверяется с chunk, а каждый span/anchor повторно проверяется по полному source
  в `ParsePlanValidator` и executor. Сохранённый plan содержит hashes/offsets,
  а executor копирует source text; несколько spans соединяются одним пробелом.
- Непокрытый fragment, unindexed text, непрочитанный из-за LLM budget scope,
  conflicting entity или unsupported structure становится review issue. Pipeline
  продолжает physical validation до EOF, даже когда generation budget исчерпан.

## Budgets и confidence {#budgets}

| ParsingPolicy default | Значение |
| --- | --- |
| LLM run | 16 calls, 524288 reserved tokens, 120000 ms |
| Documents | 32 chunks; 32 entity occurrences в итоговом plan |
| Wire entity/field | 16 entities на response, 16 fields на entity, 8 spans на value, 4096 characters на quote |
| Diagnostics | 256 issues, 2048 source refs |
| Output | 1000 records на batch; дополнительные byte/item limits `ParsePlanOptions` |

До каждой попытки резервируется весь declared input+output token cap provider;
input, output и context caps должны быть известны. Суммарный response usage
проверяется и против context cap. Reservation не возвращается при timeout или
неизвестном usage. Time budget учитывает ожидание scanner между вызовами. Общий
source timeout и context limits HTTP adapter действуют дополнительно. Retry/repair
в session отсутствуют. Увеличение policy не снимает hard limits contracts/M5.

`hybrid_v1` вычисляет `max(0, 0.2*evidence + 0.5*validation + 0.3*agreement - penalty)`
в фиксированной Decimal context. `validation=1` только для физически принятого plan.
Structural evidence берётся из M5 candidates; для documents используется доля
разрешённых physical text refs с учётом пропусков. Structural agreement сравнивает
selectors, boundaries, groups и parent links; semantic names/metadata в сравнение
не входят. При отсутствии готового M5 plan используются консервативные 0.85/0.5
по наличию сильного candidate evidence. Document agreement — 0.85 для exact grounding,
1 при подтверждённом повторе anchor. Issues дают penalty 0.2 и **всегда** требуют
review независимо от score. Порог — 0.85. Model self-confidence не используется.
Это версионированная эвристика, а не калиброванная вероятность правильной семантики.

При timeout/rate limit/unavailable структурного request доступный M5 plan можно
получить как deterministic preview с исходной ошибкой. Unsafe/policy denied не
запускают этот fallback. Fallback не меняет classification/provider для нового egress.

## Report и безопасность {#report}

`SemanticParseReport` schema 1.1.0 содержит plan/fingerprints, mode, assessment,
safe provider attempts (provider/model, latency, prompt version/hash, usage),
unresolved blocks/refs, issues и provenance coverage. Неизвестные totals usage —
`None`; reservation остаётся в attempts. Все ненулевые semantic values содержат
refs и origins. Coverage — доля таких values с provenance, отдельно от source coverage.
Нет плана/values — coverage 0. Report/plan содержат source references и semantic
names: целиком их нельзя считать безопасным audit log. Safe attempts не содержат
raw prompts, credentials, headers или backend errors.

Каждый exact minimized payload требует trusted `SecurityApproval`. Известные
code/SQL/command/injection fragments отклоняются детерминированно; prompt лишь
обозначает trust boundary. Prompt/schema document factories имеют version `1.0.0`.
Production PII detection/redaction остаётся внешней security dependency. Redaction
должна быть согласована с immutable snapshot и offsets; context fingerprint сам
по себе не подтверждает удаление PII. Никакого silent masking/смены classification.
LLM не получает tools, DB catalog/credentials, SQL или filesystem access. Фильтр
известных injection fragments не доказывает распознавание всех атак: обязательны
закрытая grammar и повторная source validation независимо от ответа модели.

Новые spans/assessment используют ParsePlan 1.2.0; spans в normalized output —
schema 1.2.0. Старые 1.0.0/1.1.0 payloads сохраняют прежние hashes; новые поля
не записываются под старой версией. Schema validation не разрешает исполнение кода.

## Проверки и ограничения {#m06-d-checks}

`test_hybrid.py`, `test_document_flow.py`, `test_document_merge.py`,
`test_semantic_session.py` и security `test_document_semantics.py` проверяют
реальные M4/M5 с controlled clocks/IDs и fake provider: CSV regions, JSON children,
LOG variants, XML paths, text-layer PDF/DOCX contracts, overlap/conflicts, budgets,
saved replay, cancellation и injection. Default suite не вызывает внешние LLM.

OCR, автоматический document table mapping, неограниченные cross-chunk relations,
JSONL multi-root matching и произвольная LOG grammar не реализованы. Неоднозначные
границы, spans вне доступного chunk и entity counts сверх budget требуют review.
Fake tests подтверждают contracts и детерминизм, но не качество реальной модели.
Решения и совместимость: [ADR 0014](adr/0014-hybrid-semantic-parsing.md).

Наблюдаемые проверки каждого критерия и частично закрытые K5/K7:
[матрица приёмки M6](plans/M06_acceptance.md). Последние полные gates, security
исправления и оставшиеся риски: [security review](plans/M06_security_review.md).
Результаты локальных fake tests не подтверждают live deployment или remote CI.

Канонические требования: [FR-015 в ТЗ][spec-fr-015] и [milestone M6][spec-m6].

[spec-fr-015]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#fr-015-llm-assisted-semantic-parsing
[spec-m6]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m6-llm-assisted-semantic-parsing
