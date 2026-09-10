# ADR 0012 — HTTP LLM adapter и policy-aware routing

Статус: принято для минимального M6-B. Дата: 2026-09-10.

## Контекст

После foundation A нужен сетевой adapter с тем же `LLMProvider` port и безопасное
переключение deployments. Host application уже передаёт immutable `SecurityApproval`,
связанное с payload/source/classification/redaction/routing fingerprints. Scanner
и semantic analyzers ещё не реализованы. Текущая задача требует пять routing modes,
общие budgets и запрет restricted cloud egress; DB mapping не входит в M6.

## Решение

- Сохранить provider-neutral port и DTO. HTTPX находится только в optional
  infrastructure adapter и extra `llm`; импорт не создаёт client или I/O.
  Provider явно владеет lifecycle client/transport. Default tests используют
  MockTransport или настоящий HTTP parser с fake network backend.
- Выбрать Chat Completions wire с `max_tokens`, native strict JSON Schema при
  объявленной поддержке или JSON object mode с обязательной локальной Pydantic
  validation. Никаких tools, executable output, repairs или hidden retries.
  Trusted registry принимает закрытые required DTO; ParsePlan grammar и source
  validation остаются C/D. Response model ID совпадает с configured identity;
  usage обязателен, отсутствие не интерпретируется как ноль.
- Версия/hash trusted prompt сохраняются. Deployment fingerprint дополнительно
  связывает endpoint, schema/prompt registry, wire version и token overhead,
  без credentials. Секреты передаются только через отдельные SecretStr headers.
- Endpoint userinfo/query/fragment запрещены; HTTPS или numeric loopback HTTP,
  только два известных paths. Redirects/proxies/cookies отключены. Локальный
  trace очищает небезопасные diagnostics HTTPX/httpcore; закреплённый httpcore
  гарантирует порядок callback до DEBUG serialization. Глобальные logging settings
  не изменяются.
- `PolicyAwareLLMRouter` — run coordinator, а не фиктивный provider с одной model
  identity. Он вызывает только существующий port. Policies содержат весь порядок
  concrete deployment fingerprints и classification allowlists. `fixed`, `no_llm`,
  `local_only`, `privacy_first`, `fallback` имеют детерминированный выбор.
  Restricted cloud запрещён независимо от allowlist; privacy_first также блокирует
  confidential cloud. Неизвестная locality запрещает egress.
- Для минимального B approval разрешает **весь immutable allowlisted route set**.
  Перед каждым attempt заново валидируются exact request, та же policy и текущие
  capabilities. Router не фабрикует per-destination scanner report и не меняет
  payload/classification. Добавление destination или redaction требует новой
  approval. Это конкретизация предварительного плана B: полноценный per-destination
  PII scanner остаётся будущей работой, обязательная проверка каждого fallback
  сохраняется. Сам fingerprint не заменяет доверенный scanner.
- Transient timeout/rate-limit/unavailable допускают один attempt следующего
  разрешённого provider. Другие ошибки терминальны; policy никогда не ослабляется.
  `quality_first` и повторы того же provider отложены согласно текущему scope.
- До await резервируется полный token allowance provider и один call. Резерв не
  возвращается при ошибке/отмене/малом usage. Общий monotonic deadline действует на
  весь run. Concurrent generation отвергается до egress. Safe metadata сохраняет
  provider/model/latency/usage/reservation/fallback reason, без raw inputs.

## Последствия и ограничения

Нет новых vendor-specific типов в contracts/ports и новых core dependencies.
Прямой adapter проверяет classification/approval DTO; полную policy проверяет
router, поэтому host application использует его для enforcement и один instance
на run. Providers и их trusted config принадлежат composition root.

Token reservation намеренно консервативна. UTF-8 bound + настроенный chat-template
overhead предполагает byte-based tokenizer; корректность deployment declaration
обеспечивает оператор. SDK ограничивает свои запросы, но не контролирует биллинг
или честность стороннего сервера. Real backend compatibility не подтверждается
offline contract tests; требуются отдельные opt-in deployment checks.

Ни PII scanner, ни LLM/HybridStructureAnalyzer, ни semantic report не считаются
реализованными. `NEEDS_REVIEW`/deterministic fallback остаются обязанностью C/D;
router возвращает typed failure без пустого успешного результата. DB не затронута.

Связанные документы: [ADR 0011](0011-llm-provider-foundation.md),
[API и limits](../llm.md), [план M6](../plans/M06_llm_semantic_parsing.md).
