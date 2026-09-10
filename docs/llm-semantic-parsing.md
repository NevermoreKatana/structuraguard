# LLMStructureAnalyzer — semantic parsing M6-C

`LLMStructureAnalyzer` предлагает и проверяет `ParsePlan` через provider-neutral
`LLMProvider`. Он принимает `StructureAnalysisRequest`: source manifest M4,
`StructureProfile` M5 с deterministic candidates, mode и bounded `PhysicalSample`.
Trusted caller передаёт `LLMStructurePolicy`, `LLMAnalysisContext`, scanner,
настоящий `ParsePlanValidator` и factory полного replay того же snapshot.

Канонические требования: [FR-015 в ТЗ][spec-fr-015] и [milestone M6][spec-m6].

## Публичный вызов

Функция ниже принимает уже подготовленный request и зависимости host application.
`replay()` каждый раз открывает независимый stream с теми же IDs/fingerprints.
Analyzer закрывает оба stream, включая ошибки и cancellation. Provider lifecycle
принадлежит caller; source целиком в памяти analyzer не удерживается.

<!-- example:m06-analyzer:start -->
```python
from collections.abc import AsyncIterable, Callable

from structuraguard.contracts import (
    ExtractedBatch,
    LLMAnalysisContext,
    LLMStructurePolicy,
    StructureAnalysisRequest,
    StructureAnalysisResult,
)
from structuraguard.ports import LLMProvider, SecurityScanner
from structuraguard.structure import LLMStructureAnalyzer, ParsePlanValidator


async def analyze_structure(
    request: StructureAnalysisRequest,
    replay: Callable[[], AsyncIterable[ExtractedBatch]],
    provider: LLMProvider,
    scanner: SecurityScanner,
    context: LLMAnalysisContext,
) -> StructureAnalysisResult:
    policy = LLMStructurePolicy(max_sample_values=100, max_catalog_refs=256)
    analyzer = LLMStructureAnalyzer(
        provider=provider,
        scanner=scanner,
        validator=ParsePlanValidator(options=policy.execution),
        context=context,
        policy=policy,
    )
    return await analyzer.analyze(request, replay=replay)
```
<!-- example:m06-analyzer:end -->

Для HTTP adapter зарегистрировать `prompts=(semantic_prompt(),)` и
`schemas=(semantic_response_schema(),)` из `structuraguard.structure`.
Prompt и strict response schema имеют версию `1.0.0`. Native JSON Schema
используется при capability `json_schema=True`; JSON mode также требует локальной
строгой проверки. Настройки HTTP lifecycle, token limits и routing описаны
в [LLM API](llm.md). Default tests используют только fake provider/HTTP transport.

## Что может описать модель

| Семейство | Разрешённая grammar |
| --- | --- |
| Таблица | Zero-based header/data/footer regions, repeated headers, конечный data end, column selectors, одна row entity |
| Дерево | Реальный root, literal KEY/name/occurrence и ITEM steps, nested parent/child groups, paths к values или names |
| Логи | Полный конечный ordered scope строк, disjoint explicit records, несколько event variants, bounded multiline records; selections целой записи или части строки по delimiter/index/offset |
| Документ | Полный конечный ordered scope blocks, sections как explicit groups, несколько entity groups; block text либо key/value target с проверкой literal key |

`SemanticPlanProposal` — закрытое описание разрешённого подмножества ParsePlan,
а не готовый executable plan. `SemanticSelector` не принимает expressions,
regex или callbacks. Неиспользуемые поля обязательны и содержат `null`/пустой
массив; лишние поля запрещены. В wire используется `log_piece`, который compiler
преобразует в существующий `LogTokenSelector` внутри SDK. Это сохраняет запрет
credential-bearing ключей generic LLM DTO.

Модель предлагает ASCII snake_case field/entity identifiers, semantic names,
тип из enum и locale из policy. SDK сохраняет raw values; hints не запускают
нормализацию чисел, дат или денег. Отдельные XML matching, optional tree fields,
несколько tree roots и document table selectors остаются ограничениями M5.

## Последовательность и безопасность

1. Проверить DTO, source lineage и budgets. Первый полный bounded replay
   подтверждает **значение**, location и batch fingerprint каждого sample.
2. Выбрать top-k candidates по confidence/candidate ID, включить их evidence,
   samples и оставшиеся profile refs до лимита. Порядок aliases определяется
   manifest, а не ответом модели. Caller выбирает sample deterministically;
   превышение лимита отклоняется без молчаливого обрезания raw values.
3. Создать minimized payload: source/profile/policy hashes, bounded scalar samples,
   physical facts, coverage и не более 32 projections observations. Source refs,
   table/block IDs заменены aliases `r0…`, candidates — `c0…`. Source filename,
   URI, metadata и полный extraction не отправляются.
4. Пометить payload `UNTRUSTED_SOURCE_DATA`, применить veto известных injection/
   code/SQL/command fragments, затем вызвать обязательный trusted scanner для
   **точного** payload. `SecurityApproval` связан с run/source/payload/
   classification/routing/redaction fingerprints. Отказ или чужой report
   запрещает egress; неизвестная locality и restricted cloud также запрещены.
5. Сделать один `generate_structured`. Проверить JSON/strict Pydantic schema,
   prompt/schema/provider/model binding, aliases, literal paths и family grammar.
   Regex-veto применяется к декодированным строкам; основная граница — закрытые
   selectors и source membership. Ответ модели нигде не исполняется.
6. SDK назначает producer, revision, hashes и confidence. Затем **обязателен**
   `ParsePlanValidator.validate_source` со вторым полным replay. После него
   проверяются полнота scope, неоднозначность и threshold. Executor при дальнейшем
   применении плана снова валидирует source и selectors.

Analyzer не имеет DB catalog, credentials, tools или filesystem API. Generic
LLM payload запрещает соответствующие поля; security scanner проверяет текстовые
данные. Production PII detection/redaction здесь не реализованы: host обязан
внедрить trusted scanner и подходящую policy. Analyzer не маскирует данные после
approval и не снижает classification. Fingerprint подтверждает согласованность,
но сам по себе не является разрешением или доказательством подлинности источника.

## Лимиты и outcomes

Default: 100 samples, 256 catalog refs, 8 candidates, 64 fields, 16 entities;
request/response по 65 536 UTF-8 bytes. Schema жёстко ограничивает paths 30 steps,
records 64 refs, scope 512 refs. Policy может только сузить wire grammar.
Размер готового ParsePlan и полный physical replay ограничены `ParsePlanOptions`;
общий processing deadline — `source_limits.max_processing_seconds` (default 300).
HTTP provider дополнительно проверяет context/token limits, включая prompt/schema
и output allowance; маленького context window может не хватить даже для sample.

Один invocation делает максимум один LLM call; число строк не создаёт новых calls.
Hidden retry, repair и deterministic fallback отсутствуют. Calls/token/time
budgets нескольких invocation принадлежат run coordinator. Router B пока
не подставляется вместо concrete provider: выбор provider и orchestration трёх
режимов описаны в [Hybrid M6-D](semantic-parsing.md). Session использует concrete provider;
автоматическая router/session composition остаётся отдельной интеграцией.

| Условие | Результат |
| --- | --- |
| `deterministic` или отсутствует replay | `StructureRejected`, 0 LLM calls; deterministic работу выполняет M5 analyzer |
| `llm_assisted` / `llm_first` | Явный один вызов после source/security checks; решение о необходимости вызова принимает caller |
| Валидный полный plan, один непротиворечивый candidate и score ≥ 0.85 | `StructurePlanCreated` |
| Ambiguous/unsupported response; конфликт candidates; низкий score; неполный scope | `StructureNeedsReview` (`kind="needs_review"`, соответствует `NEEDS_REVIEW`), без разрешения на execution |
| Malformed/oversized/unsafe plan, неизвестный ref/path, отказ validator | Typed `LLMProviderError`, без принятого плана |
| Timeout/rate limit/unavailable/policy denial | Typed error без retry; cancellation распространяется |

Коды `LLMProviderError` сохраняются только из закрытого `LLMErrorCode`; чужие
exception details, notes и цепочки ошибок provider не выдаются caller. Неизвестный
typed code становится `LLM_INVALID_RESPONSE`, обычная ошибка adapter —
`LLM_UNAVAILABLE`. Поле exception `call` может отсутствовать; безопасная история
попыток принадлежит provider или run/session coordinator.

`self_confidence` модели не влияет на итоговый score. Policy `candidate_min_v1`
использует minimum confidence связанных deterministic candidates; без candidate
score равен нулю. Модель не может скрыть competing candidate своим выбором `c0`.
Большие log/document scopes требуют отдельной стратегии M6-D: sample не считается
полным документом. Второй replay проверяет targets; локальная полнота дополнительно
проверяет конечный табличный диапазон, log/block scope и единственный tree root.

`ParsePlan` schema `1.1.0` сохраняет optional `semantic_analysis` с prompt version/hash,
request/generation/schema fingerprints и provider/model identity. `ParseField`
сохраняет optional `locale_hint`. При отсутствии этих полей legacy JSON/hash
не меняются; `1.0.0` с новыми полями отклоняется. Raw prompt/response и model score
не входят в provenance; usage/latency остаются в safe history provider.
Сам ParsePlan и semantic names могут содержать sensitive source paths — это
не содержимое безопасного audit log.

## Проверки и оставшийся scope

`test_llm_analysis.py` проверяет четыре семейства через настоящий M4/M5 validator,
две log variants, рост таблицы 10 → 100, низкий score, competing candidates,
устойчивые hashes и оба HTTP structured modes с MockTransport.
`test_llm_plans.py` проверяет malicious/unknown refs, forged samples, byte budgets,
source replay mutation, scanner denial, restricted cloud, timeout и cancellation.

Hybrid orchestration, chunk entity extraction и расширенный semantic report
реализованы в [M6-D API](semantic-parsing.md). Production PII scanner/redaction
и общая SDK/router facade остаются отдельными задачами. DB mapping в M6 не реализуется. Архитектурное решение:
[ADR 0013](adr/0013-validated-llm-structure-analysis.md).

Review дополнительно проверяет omission второй таблицы вне bounded index,
cooperative cancellation immediate replay и absolute deadline перед egress.
Все эти regression cases входят в security suite; deadline test использует
управляемое время event loop, без произвольных ожиданий.

[spec-fr-015]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#fr-015-llm-assisted-semantic-parsing
[spec-m6]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m6-llm-assisted-semantic-parsing
