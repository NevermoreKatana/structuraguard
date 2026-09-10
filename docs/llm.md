# LLM providers и routing M6-A/B

Для offline-проверок semantic parsing используйте `FakeLLMProvider` с явно
заданными responses и часами. Он реализует тот же `LLMProvider`, что и
`OpenAICompatibleProvider`. Для полного запрета generation передайте
`NoLLMProvider`: его capabilities объявляют `disabled`, а прямой вызов всегда
возвращает `LLMProviderError` с `LLM_POLICY_DENIED`.

Эта страница описывает provider foundation, HTTP adapter и отдельный policy-aware
router. Проверяемое создание ParsePlan описано в [M6-C](llm-semantic-parsing.md),
а полный bounded flow, document extraction и report — в
[M6-D](semantic-parsing.md). Production PII scanner/redaction, общая SDK facade,
автоматическая композиция router с session и DB mapping не реализованы в M6.
Канонические требования: [LLM-агностичность в ТЗ][spec-llm] и [milestone M6][spec-m6].

## Исполняемый offline-пример {#m06-provider-example}

`SecurityApproval` здесь создаёт тестовая заглушка для заведомо synthetic payload.
В приложении разрешение обязан выдавать trusted scanner после classification,
redaction и routing policy checks. Самостоятельно вычисленный hash разрешения
не заменяет эти проверки.

<!-- example:m06-provider:start -->
```python
import asyncio
import hashlib
from datetime import UTC, datetime

from structuraguard.contracts import (
    DataClassification,
    LLMPrompt,
    LLMRequest,
    PipelineStatus,
    ProducerMetadata,
    SecurityApproval,
    SecurityReport,
)
from structuraguard.llm import FakeLLMProvider, ScriptedResponse

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


def fingerprint(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


async def main() -> None:
    payload = '{"sample":"synthetic"}'
    source_hash = fingerprint("synthetic source")
    payload_hash = fingerprint(payload)
    report = SecurityReport(
        request_id="scan-1",
        run_id="run-1",
        purpose="llm_input",
        content_fingerprint=source_hash,
        payload_fingerprint=payload_hash,
        data_classification=DataClassification.PUBLIC,
        routing_policy_id="test-local",
        routing_policy_fingerprint=fingerprint("test policy"),
        redaction_fingerprint=fingerprint("synthetic: no PII"),
        producer=ProducerMetadata(
            component_id="test_scanner",
            component_version="1.0.0",
            sdk_version="0.3.0",
        ),
        decision="allowed",
        status=PipelineStatus.COMPLETED,
        artifact_fingerprints=(source_hash, payload_hash),
        scanned_items=1,
        generated_at=NOW,
    )
    prompt = LLMPrompt(
        prompt_id="semantic_parse_plan",
        version="1.0.0",
        fingerprint=fingerprint("trusted prompt v1"),
    )
    request = LLMRequest(
        request_id="request-1",
        run_id="run-1",
        purpose="semantic_parsing",
        response_schema_id="example-result",
        response_schema_version="1.0.0",
        payload_json=payload,
        payload_fingerprint=payload_hash,
        content_fingerprint=source_hash,
        data_classification=report.data_classification,
        routing_policy_id=report.routing_policy_id,
        routing_policy_fingerprint=report.routing_policy_fingerprint,
        redaction_fingerprint=report.redaction_fingerprint,
        security_approval=SecurityApproval(
            report=report,
            report_fingerprint=fingerprint(report.canonical_json()),
        ),
        prompt=prompt,
        prompt_fingerprint=prompt.fingerprint,
        max_output_bytes=4096,
    )
    provider = FakeLLMProvider(
        (ScriptedResponse(output_json='{"fields":[]}', input_tokens=12, output_tokens=4),),
        clock=lambda: NOW,
    )
    response = await provider.generate_structured(request)
    assert response.prompt == prompt
    assert provider.calls[0].generation_fingerprint == response.generation_fingerprint
    print(response.output_json)
    print(f"{provider.call_count} call; prompt {response.prompt.version}")


asyncio.run(main())
```
<!-- example:m06-provider:end -->

Пустой `fields` в примере — тестовый JSON, не валидный `ParsePlan`.
Provider result остаётся недоверенным: downstream обязан проверить semantic schema,
source references и plan по physical replay перед execution.

## Scripted outcomes и воспроизводимость

| Fixture | Поведение |
| --- | --- |
| `ScriptedResponse(output_json=...)` | Возвращает `LLMResponse`, если это canonical bounded JSON object и он проходит существующую M2 boundary. |
| Malformed response | Передайте invalid JSON в `ScriptedResponse`. При вызове получите `LLM_INVALID_RESPONSE`, без попытки repair. Duplicate keys, NaN, лишний текст и неканонические пробелы также отклоняются. |
| Injected payload | Инструкции внутри допустимых строк JSON возвращаются как inert data. Fake не исполняет текст, не открывает URL и не подтверждает его безопасность. Credential canaries и forbidden keys M2 отклоняются. |
| `ScriptedFailure(code=LLMErrorCode.TIMEOUT)` | `LLM_TIMEOUT`, retryable; реального ожидания нет. |
| `ScriptedFailure(code=LLMErrorCode.RATE_LIMIT)` | `LLM_RATE_LIMIT`, retryable. |
| `ScriptedFailure(code=LLMErrorCode.UNAVAILABLE)` | `LLM_UNAVAILABLE`, retryable. |

`LLMErrorCode`, `LLMPrompt`, `LLMExecutionEnvironment` и `LLMCallRecord` доступны
через `structuraguard.contracts`, `LLMProviderError` — через
`structuraguard.exceptions`. Vendor SDK types в этих API отсутствуют.

Clock обязателен, вызывается один раз перед резервированием step и возвращает
timezone-aware UTC `datetime`. `elapsed_ms` — заданная сценарием длительность;
response timestamp равен clock + elapsed. IDs приходят из request и capabilities,
случайные IDs и системные часы provider не использует. Для async cancellation
есть trusted `before_response` checkpoint: используйте Events, а не sleep.
Checkpoint принадлежит тестовому host code; он не формируется из source/model output.

Step резервируется до checkpoint. Cancelled attempt расходует step, попадает в
history и распространяет `CancelledError`. Concurrent calls в одном event loop
резервируют разные steps; `calls` перечисляет завершившиеся попытки в порядке
завершения. Использование instance из нескольких threads/event loops не поддержано.

`expected_request_fingerprint` позволяет привязать step к hash всего canonical
request. Несовпадение даёт `LLM_SCRIPT_MISMATCH`, конец сценария —
`LLM_SCRIPT_EXHAUSTED`; оба отказа происходят без расходования нового step.
Invalid request, неподдержанный purpose и input byte limit также проверяются
до начала попытки. Поддельный `model_copy()` повторно валидируется.

## Capabilities, limits и metadata

`ProviderCapabilities` содержит provider/model identity, purposes, structured JSON,
JSON Schema/tool-calling flags, `execution_environment`, byte limits и optional
input/output/context token limits. `unknown` не означает local; `None` token
limit не означает неограниченный context. Local/cloud задаёт trusted composition
owner, а не анализ URL. Объявленная tool capability не разрешает передачу tools.

Fake имеет `local`, `structured_output=True`, `json_schema=False`,
`tool_calling=False`. Его default limits: по 65 536 input/output bytes,
8192/2048 input/output tokens, 16 384 context tokens. Он проверяет размер payload
и scripted usage; он не реализует tokenizer, реальный transport overhead,
run-wide reservations или routing. Output ограничен также `request.max_output_bytes`.

Сценарий ограничен 1000 steps и 4 MiB raw fixture bytes суммарно; каждая fixture
ограничена 1 048 576 символами до проверки byte budget. `max_history` — 64 по умолчанию,
диапазон 1–1000. История содержит только hashes, trusted prompt metadata,
attempt number, outcome/code, counters и timestamps. Неполученный usage записывается
как `None`, не ноль; значения scripted success должны быть заданы fixture корректно.
`call_count` учитывает все начатые attempts, даже вытесненные из history.

Запрещены secrets в metadata, IDs, версиях и названиях модели. DTO отклоняют
распространённые credential/DSN canaries и extra fields; это дополнительный guard,
не универсальный secret classifier. `repr(request/response/step)` исключает raw
JSON. Сериализация целого request/response/fixture по явному вызову сохраняет raw
payload для транспортного/тестового использования; её нельзя считать безопасным
логом. Для diagnostics используйте `LLMCallRecord`. Provider сам ничего не логирует.

## Совместимость и проверки

Сигнатура `LLMProvider` не меняется. Legacy request/response без `prompt` и
capabilities без новых полей сохраняют прежнюю canonical serialization; в новом
fake `LLMPrompt` обязателен. Изменение prompt metadata создаёт новый request hash.
HTTP adapter проверяет trusted prompt registry и точный hash текста шаблона.
Старый consumer может отвергнуть новые поля —
используйте legacy wire shape либо обновите consumer, не удаляя metadata молча.

`NoLLMProvider` — специальный disabled variant с нулевыми byte limits и пустыми
purposes. Активные providers сохраняют требования положительных budgets и
structured output. Foundation A не добавляет dependencies; HTTP adapter использует
отдельный opt-in extra `llm`. Изменений DB нет.
Решение зафиксировано в [ADR 0011](adr/0011-llm-provider-foundation.md).

Общий suite находится в `tests/contract_suites/llm.py`: каждый будущий adapter
подключает controlled backend к тому же `LLMProviderContractCase`. Suite проверяет
exact DTO types, identity/schema/prompt binding, canonical output, byte limits,
typed failures и неизменность request. Дополнительные unit/security tests
проверяют cancellation, concurrency, scripted sequence, budgets и утечки.

## HTTP provider

Установите `structuraguard[llm]`. Extra использует HTTPX и закреплённый
`httpcore==1.0.9` с BSD-3-Clause лицензиями, уже применяемые Tika adapter.
Vendor SDK и новые core dependencies не добавлены. Import `structuraguard.llm`
не импортирует HTTPX, не читает environment и не создаёт client.

`OpenAICompatibleProvider` принимает `OpenAICompatibleConfig`, tuple trusted
`LLMPromptTemplate` и `LLMResponseSchema`, optional fake `transport`, UTC `clock`
и `monotonic`. Откройте provider через `async with` либо `await start()`;
после использования вызовите `await aclose()`. Закрытие отменяет активные вызовы,
дожидается cleanup и закрывает client/transport ровно один раз. Provider владеет
переданным transport; повторное открытие закрытого instance запрещено.

Config задаёт credential-free endpoint `/v1/chat/completions` либо
`/chat/completions`, capabilities и classification allowlist. Допускается HTTPS
или HTTP numeric loopback. Userinfo, query, fragment, escaped credentials и
произвольные paths запрещены до client creation. Local/cloud задаёт владелец
deployment: локальный адрес сам по себе не доказывает trust domain.
`api_key` и значения `OpenAICompatibleHeader` используют `SecretStr`, исключены
из repr/serialization и добавляются только в HTTP headers. Endpoint также не
входит в safe config serialization. TLS verification включена; environment
proxies, redirects, cookies, tools, streaming generation и HTTP retries отключены.

Для `json_schema=True` передаётся native strict JSON Schema. Иначе используется
JSON object mode с той же schema в trusted system message и строгой локальной
Pydantic validation. Registry model наследует `FrozenContract`, использует
закрытые objects и required fields, без default filling. Nullable required поля
разрешены; open dictionaries, external schema references и произвольные схемы
из response запрещены. Не все JSON Schema keywords поддерживаются каждым
совместимым backend: несовместимость не запускает скрытое ослабление формата.
Wire-контракт основан на [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs/).

Первое system message содержит точный versioned template. Второе — версионируемую
границу недоверенного ввода и schema. User message содержит только одобренный
canonical payload. `LLMPrompt.fingerprint` — SHA-256 точных UTF-8 bytes шаблона.
`capabilities.deployment_fingerprint` связывает endpoint, wire version, registry
prompt/schema hashes, header names, classification allowlist и token overhead;
credentials не участвуют. Изменение registry требует новой policy approval.
Prompt является инструкцией, а не security control: response остаётся недоверенным.

Output должен содержать один assistant result, известный model ID, `stop`,
обязательный неотрицательный integer usage и JSON object. Whitespace canonicalizes;
duplicate keys, NaN, trailing text, refusal/tools, неизвестный model, лишние поля
schema и coercion отклоняются. Backend aliases следует согласовать с точным model ID
в capabilities. Отсутствующий usage даёт `LLM_INVALID_RESPONSE`, не фиктивный ноль.
Дальнейшая проверка ParsePlan, identifiers и provenance принадлежит M6-C/D.

Типизированные отказы: `LLM_TIMEOUT`, `LLM_RATE_LIMIT`, `LLM_UNAVAILABLE`,
`LLM_AUTHENTICATION_FAILED`, `LLM_INVALID_RESPONSE`, `LLM_SCHEMA_VIOLATION`,
`LLM_CONTEXT_LIMIT`, `LLM_CAPABILITY_MISMATCH`, `LLM_REQUEST_INVALID` и
`LLM_POLICY_DENIED`. Cancellation распространяет стандартный `CancelledError`
после освобождения response и оставляет `cancelled` record. Raw HTTP exceptions,
body, credentials и upstream model strings не копируются в errors/history.
HTTPX/httpcore logging не выключается глобально: локальный trace убирает опасные
response/exception diagnostics до DEBUG serialization; HTTPX diagnostic extensions
очищаются до INFO log. Этот механизм проверяется на настоящем HTTP parser с
in-memory network backend, без sockets и внешних вызовов.

## Policy-aware router и бюджеты

`PolicyAwareLLMRouter` — координатор одного run; providers остаются за существующим
`LLMProvider` port. Router принимает `run_id`, immutable `LLMRoutingPolicy`, tuple
providers в порядке routes и управляемые clock/monotonic. Он не владеет lifecycle
providers. Его capabilities не выдаются за один deployment; `LLMResponse` всегда
содержит identity фактически ответившего provider.

Каждая `LLMRoutePolicy` содержит hash полных capabilities и точную classification
allowlist. `LLMRoutingPolicy` связывает порядок routes, mode и `LLMBudget`.
Trusted scanner должен разрешить `router.policy_fingerprint` вместе с точными
payload/source/redaction hashes и classification. Не копируйте approval с другой
policy: router проверяет её перед каждым attempt и проверяет capabilities drift.
Минимальный router не выполняет redaction и не выдаёт новые approvals. После
изменения payload или destinations требуется новая проверка trusted scanner.
Прямой provider проверяет DTO approval и свой classification allowlist; проверку
полной routing policy выполняет router. Для policy enforcement используйте router.

| Mode | Выбор и fallback |
| --- | --- |
| `fixed` | Ровно один configured provider; один attempt, без переключения. |
| `no_llm` | `LLM_POLICY_DENIED`, ноль вызовов; позволяет caller выбрать deterministic branch. |
| `local_only` | Только явно объявленные local deployments, порядок policy. |
| `privacy_first` | Local перед cloud; confidential/restricted допускаются только local. |
| `fallback` | Разрешённые deployments в порядке policy. |

Restricted никогда не уходит cloud, даже если ошибочно включён в cloud allowlist.
Local restricted требует явного разрешения. Unknown/disabled locality не допускает
egress. Отсутствие разрешённого provider даёт `LLM_POLICY_DENIED`; отсутствие нужных
capabilities среди разрешённых — `LLM_CAPABILITY_MISMATCH`.
В трёх режимах с переключением следующий разрешённый provider вызывается только
после timeout/rate-limit/unavailable. Schema, policy, auth и context failures
терминальны. Один provider не повторяется; retry delay отсутствует. Fallback
передаёт тот же request, не меняет classification и не понижает требования.

`LLMBudget` ограничивает calls, tokens и time всего run, включая idle time между
запросами и failed/cancelled attempts. До await резервируется сумма объявленных
`max_input_tokens + max_output_tokens` выбранного provider. Резерв не возвращается:
повторное использование малой/неизвестной usage не позволяет превысить бюджет.
Token limits должны быть известны. HTTP adapter ограничивает output через
`max_tokens`; input оценивает сверху по UTF-8 bytes полного wire и заданному
`input_token_overhead` (default 1024, минимум 256). Этот bound рассчитан на
byte-based tokenizer; оператор обязан покрыть скрытый deployment chat template
overhead и выбрать согласованные token limits. Это консервативный лимит SDK,
не гарантия тарификации стороннего сервера. Headers, envelope, body и context
также имеют byte/token limits. Общий deadline прерывает зависший вызов.

Router допускает максимум один активный запрос и 1000 attempts на run; конкурентный
запрос отвергается до egress с `LLM_BUDGET_EXCEEDED`. Такой же code возвращается
при исчерпании calls/tokens/time. Создание нового router означает новый budget;
host application должна использовать один instance на run.

`provider.calls` и `router.calls` содержат только safe
`LLMCallRecord`: provider/model/version, prompt identity, request/capabilities hashes,
latency, success usage, outcome/error и reservation/fallback reason. Unknown usage
failed/cancelled attempts — `None`. Raw source/output/headers в history отсутствуют.
History HTTP provider ограничена `max_history`, router — `max_calls`.

`LLMProviderError.call` — optional record, а не гарантированная история: на границе
router/analyzer/run недоверенные exception details, notes, cause и attached record
отбрасываются. Используйте `code` для обработки ошибки и history для attempts.
Неизвестный typed code заменяется на `LLM_INVALID_RESPONSE`; произвольная ошибка
provider/HTTP transport становится `LLM_UNAVAILABLE`, timeout — `LLM_TIMEOUT`.
Cancellation сохраняет `asyncio.CancelledError` без backend diagnostics.

Тесты используют [HTTPX MockTransport](https://www.python-httpx.org/advanced/transports/)
и in-memory httpcore backend. Ни default suite, ни CI не обращаются к внешнему LLM.
Lifecycle следует [async HTTPX API](https://www.python-httpx.org/async/).
Решение: [ADR 0012](adr/0012-policy-aware-llm-routing.md).

Актуальная проверка всего M6: [матрица приёмки](plans/M06_acceptance.md) и
[security review](plans/M06_security_review.md). Результаты A/B ниже исторические.

[spec-llm]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#19-llm-агностичность
[spec-m6]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m6-llm-assisted-semantic-parsing

## Историческая локальная приёмка M6-A

Проверено 2026-09-10, Python 3.12.9. Полные gates запускались с `UV_OFFLINE=1`
вне desktop sandbox: document watchdog и локальный fake HTTP server требуют
доступа к локальным системным ресурсам. Внешние/платные LLM не вызывались.

| Проверка | Результат |
| --- | --- |
| `make lint` | Пройдено. |
| `make typecheck` | Пройдено, 183 файла. |
| `make test` | 1937 passed. |
| `make test-integration` | 16 passed. |
| `make test-security` | 334 passed. |
| `make docs` | Строгая сборка пройдена. |
| `make lock-check`, `make test-build` | Lockfile, wheel/sdist и установленный package проверены offline. |
| `git diff --check` | Пройдено; production dependencies и lockfile не менялись. |

Review выявил утечку через serializer warning для forged DTO. Исправление
`model_dump(warnings="error")` проверяет regression
`tests/security/llm/test_provider_boundary.py::test_forged_metadata_does_not_leak_through_serializer_warnings`.
Неисправленных существенных findings в проверенном diff не осталось. Ограничения:
trusted test clock/checkpoint; guards metadata не заменяют будущий PII scanner.
Результаты выше относятся к отдельному foundation A, до реализации B.

## Локальная приёмка M6-B {#m06-b-acceptance}

Проверено 2026-09-10 на Python 3.12.9. Полный запуск с `UV_OFFLINE=1`:

| Проверка | Результат |
| --- | --- |
| `make lint` | Пройдено, 194 файла. |
| `make typecheck` | Пройдено, 192 файла. |
| `make test` | 2005 passed; 5 прежних PyMuPDF/SWIG deprecation warnings. |
| `make test-integration` | 16 passed. |
| `make test-security` | 360 passed. |
| `make docs` | Строгая сборка пройдена. |
| `make lock-check`, `make test-build` | Offline lockfile, wheel/sdist и isolated installed-package smoke пройдены. |
| `git diff --check` | Пройдено. |

Security/review проверили URL/header/response boundaries, prompt injection,
classification/fallback, token reservations, cancellation и закрытие transport.
Выявленное при review повторное закрытие переданного transport устранено общей
shutdown task; regression проверяет concurrent/idempotent close и отмену active
request. Forged capabilities повторно валидируются с `warnings="error"` до hash,
что исключает сырой serializer warning при drift. Существенных неисправленных
findings в проверенном scope не осталось.

Real deployment и remote CI не запускались. Остаточные ограничения: trusted
classification/approval и token overhead, обязательный usage/exact model ID,
ограниченный Chat Completions wire, отсутствие PII scanner и semantic analyzers.
