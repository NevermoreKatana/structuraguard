# M06 — security review текущего diff

Дата: 2026-09-10. Scope: незакоммиченный diff M6 A–D и acceptance additions,
включая новые файлы providers/router, semantic DTO, analyzer/chunks/session,
изменения M5 validator/executor, optional dependencies и installed smoke.
Неизменённые parser/DB implementation повторно не разрабатывались.

Найдены и исправлены **три Medium проблемы**. Critical/High в проверенном scope
не обнаружены. Ни одна из находок не даёт модели tools, SQL или filesystem authority.
Ограничения production PII и полного report из [приёмки M6](M06_acceptance.md)
остаются явными; этот review не объявляет их реализованными.

## S1 — Medium: утечка недоверенных provider/transport exceptions

- **Места:** `packages/structuraguard/src/structuraguard/llm/router.py:260`,
  `llm/run.py:87`, `structure/llm_analysis.py:356`,
  `llm/openai_compatible.py:325` и `:555` (остальные пути от того же package root).
  Общий исправленный контроль: `llm/_boundary.py:17` и `:26`.
- **Путь эксплуатации:** сторонний совместимый adapter включает текст upstream
  ответа или credential в `RuntimeError`, `ValueError` при read/cleanup либо
  `LLMProviderError.add_note(...)`. Router/direct analyzer пропускали неожиданные
  exceptions; run/direct analyzer пропускали notes typed error. Неизвестный
  `error_code` также приводил к раскрытию исходного текста. HTTP stream/transport
  мог вернуть ненормализованную ошибку при чтении или shutdown.
- **Влияние:** секрет/source content выходит через обычный публичный traceback
  и может попасть в application logs; некоторые failed attempts помечались
  cancelled. Для эксплуатации нужен upstream/adapter error path, а не возможность
  выполнить код из ParsePlan.
- **Минимальное исправление:** общая обёртка только вокруг внешнего generation;
  новый exception сохраняет allowlisted error code и отбрасывает чужие
  notes/details/call/cause. Неожиданный error становится `LLM_UNAVAILABLE`, неизвестный
  typed code — `LLM_INVALID_RESPONSE`. HTTP generation и transport shutdown имеют
  такой же sanitization boundary. Cancellation распространяется; history собирается
  из собственных проверенных request/caps.
- **Security regression:**
  `tests/security/llm/test_m06_error_boundaries.py::test_foreign_provider_errors_are_sanitized_at_each_public_boundary`
  (router/run/analyzer × unexpected/typed-note/unknown-code),
  `test_http_stream_errors_are_typed_and_do_not_leak` (read/cleanup),
  `test_http_transport_shutdown_cannot_leak_foreign_exception`.
  Canary отсутствует в стандартном traceback, details, logs и safe history;
  failed outcome/code проверяются явно, validator не вызывается.

## S2 — Medium: неполная проверка token/context policy в run wrapper

- **Место:** `packages/structuraguard/src/structuraguard/llm/run.py:66` и `:120`.
- **Путь эксплуатации:** разрешённый provider объявляет неизвестный input/output
  token cap или context window. Прежнее `(cap or 0)` допускало generation с
  неполным reservation. Другой вариант: response usage укладывается в отдельные
  input/output caps, но сумма превышает context window; wrapper принимал ответ.
- **Влияние:** run policy не даёт заявленного upper bound, а metadata может
  подтверждать generation, нарушающую согласованный context contract. Это важно
  для provider-neutral adapters, которые могут неверно реализовать собственные
  проверки; HTTP adapter уже проверял эти ограничения самостоятельно.
- **Минимальное исправление:** все три token limits должны быть известны до
  provider invocation (`LLM_CAPABILITY_MISMATCH`, zero calls). Суммарный usage
  проверяется независимо (`LLM_CONTEXT_LIMIT`), без generation fingerprint.
- **Security regression:**
  `tests/security/llm/test_m06_run_limits.py::test_unknown_token_cap_denies_run_before_provider`
  (каждый из трёх caps),
  `test_combined_usage_cannot_exceed_context_window` (80 + 20 > context 90).

## S3 — Medium: повтор анализа после failure сбрасывал budget той же session

- **Место:** `packages/structuraguard/src/structuraguard/parsing/session.py:116`.
- **Путь эксплуатации:** document generation уже потратила один разрешённый call,
  затем source cleanup падает или анализ отменяется. `_analysis` остаётся пустым;
  caller повторяет `analyze_structure()` на той же session. Новый analyzer pass
  создавал новый `LLMRunProvider` с полным budget под прежним run ID.
- **Влияние:** повторные calls сверх `max_calls`, потенциально повторные затраты;
  повтор мог скрыть failure/cancellation исходного run. Это не исправляется
  ограничением «одно execution»: public analyze API доступен отдельно.
- **Минимальное исправление:** failed/cancelled analysis закрывает session для
  дальнейших операций. Cleanup и итоговый failure/cancel report остаются доступны;
  новый запуск требует отдельной session. Кэш успешного анализа сохранён.
- **Security regression:**
  `tests/security/structure/test_m06_failed_run.py::test_failed_analysis_cannot_restart_call_budget_in_same_session`
  (cleanup/cancelled). При `max_calls=1` повтор запрещён, фактический call остаётся
  один; report не содержит успешного normalized fingerprint.

## Применимость угроз и проверенные controls

Пути тестов ниже относительно `packages/structuraguard/tests/`.

| Угроза | Проверка текущего diff и результат |
| --- | --- |
| Attacker-controlled input | Source, source names/paths, model output, replay и response bodies недоверенные. Exact sample/manifest replay, closed Pydantic schemas, aliases и span hashes: `security/structure/test_llm_plans.py`, `test_document_semantics.py`. Config, callable plugins, clocks, schema code и scanner — trusted host dependencies. |
| Resource exhaustion | Bounded payload/response/chunks/plan/record buffer, fixed schema limits, calls/tokens/time reservations, cancellation/cleanup. Исправлены S2/S3; существующие `test_m06_chunk_properties.py`, `test_document_merge.py`, HTTP stream-limit и M5 source-limit tests сохранены. |
| Parser exploit / unsafe deserialization | M6 потребляет M4 DTO и не добавляет deserializers. JSON разбирается без duplicate keys/NaN, Pydantic использует закрытый trusted DTO; никаких `eval`, `exec`, pickle, YAML object constructors. Полный security gate повторяет `security/parsers/test_markup_security.py`, `test_document_security.py` и прочие M4 regressions. |
| XXE / DTD / network access | Нового XML/DTD resolver нет. XPath/JSON paths используются как literal source coordinates. Существующие M4 XXE/DTD/external resource tests включены в gate. HTTP endpoint задаёт host; source/model не могут выбрать URL. Запрещены redirects, environment proxies, userinfo/query/fragment; HTTP допускает numeric loopback. `security/llm/test_http_boundary.py`. |
| Prompt injection / excessive agency | Source только в untrusted user payload; trusted prompt/schema отдельно. Tools/DB/filesystem execution отсутствуют. Известные active-content fragments отвергаются, refs/quotes и scopes проверяются детерминированно. Injection regressions в document/LLM plan tests. Regex-фильтр не считается доказательством распознавания всех возможных инструкций; ограничение authority обеспечивает закрытый DSL. |
| PII / secrets leakage | Restricted cloud запрещён до egress/fallback, exact approval связан с classification/routing/redaction fingerprints. Исправлен S1. Credentials в HTTP headers исключены из repr/serialization; HTTPX/httpcore diagnostic hooks проверяются fake-network tests. Production PII scanner/redaction остаётся внешним, approved fingerprint сам по себе не доказывает masking. |
| SQL / identifier injection | M6 не исполняет SQL и не читает DB catalog. Model names имеют закрытую grammar, selectors не допускают callbacks/SQL/regex. `security/structure/test_analysis_security.py::test_unknown_operators_and_plan_fields_are_forbidden`, LLM command/output regressions. Source strings не превращаются в DB identifiers. |
| Обход DB allowlist/denylist | Не применим к исполняемому M6 пути: DB adapter/mapping/loader/allowlist logic в diff не менялись, DB connection модели не передаётся. Downstream DB allowlist не заменяется parsing policy и в этом review не сертифицируется. |
| Schema drift | Versioned trusted prompt/schema registries, deployment/caps fingerprints, exact response identity, mandatory plan validation, saved-plan revalidation. Старые DTO versions не принимают новые spans, hashes перепроверяются. `test_http_boundary.py`, `test_routing_policy.py::test_caps_drift_denies_before_call`, `test_hybrid_review.py`, legacy contract suites. |
| Небезопасные logs / audit | Исправлен S1; safe attempts содержат hashes/counters/allowlisted codes. Provider error body, notes и credentials не выходят через стандартный traceback. Полный plan/report содержит source refs/semantic names и не является безопасным aggregate audit payload — это ограничение K7 сохраняется. |
| Path traversal / временные файлы | Новые M6 runtime файлы не открывают filesystem paths из source/LLM. Логические paths — данные. Изменённый installed smoke исполняет только trusted repository examples в существующем `TemporaryDirectory`; архивные traversal/symlink checks сохранены. `security/parsers/test_document_security.py`, packaging suite. |
| Supply chain | Новый optional extra `llm` использует уже присутствующие HTTPX 0.28.1 / httpcore 1.0.9. Lock diff не добавляет новых package artifacts/versions; базовые production dependencies не расширены. Оба installed metadata указывают BSD-3-Clause. Exact httpcore pin важен для проверенного DEBUG trace hook; installed smoke подтверждает отсутствие optional imports в deterministic path. |

На дату review официальные страницы
[HTTPX advisories](https://github.com/encode/httpx/security/advisories) и
[httpcore advisories](https://github.com/encode/httpcore/security/advisories)
не показывали опубликованных advisories. Это ограниченная проверка primary sources,
не полный CVE/SCA scan всех транзитивных dependencies и не гарантия отсутствия уязвимостей.

## Изменённые файлы этой проверки

- Runtime: `llm/_boundary.py`, `llm/router.py`, `llm/run.py`,
  `llm/openai_compatible.py`, `structure/llm_analysis.py`, `parsing/session.py`.
- Новые regressions: `security/llm/test_m06_error_boundaries.py`,
  `security/llm/test_m06_run_limits.py`, `security/structure/test_m06_failed_run.py`.
- Документация: этот отчёт, ссылка из M06 plan и уточнение lifecycle в semantic API.

## Проверки

- До исправлений: первый regression набор — **12 failed, 3 passed**; session budget
  regressions — **2 failed**; отдельный transport shutdown test — **1 failed**.
- После минимальных исправлений: **18 passed** (0.43 s), без сети и arbitrary sleeps.
- Узкие suites: **410 passed** (14.64 s). Lint: **224 files formatted, Ruff passed**;
  mypy: **222 files, no issues**.
- Полный `make lint typecheck test test-integration test-security docs lock-check test-build`
  завершился с **exit 0**: **2188 tests passed** (83.58 s), **18 integration passed**
  (5.29 s), **421 security passed** (19.83 s). Повторные lint/mypy прошли; strict
  MkDocs, lock-check (100 packages), wheel/sdist offline rebuild/install и installed
  executable examples успешны. Остались пять upstream SWIG deprecation warnings.
- `git diff --check` — **exit 0**, без замечаний.

```bash
UV_CACHE_DIR=/private/tmp/structuraguard-m06-uv-cache uv run --locked --no-sync pytest -q \
  packages/structuraguard/tests/security/llm/test_m06_error_boundaries.py \
  packages/structuraguard/tests/security/llm/test_m06_run_limits.py \
  packages/structuraguard/tests/security/structure/test_m06_failed_run.py

UV_OFFLINE=1 UV_CACHE_DIR=/Users/katana/.cache/uv uv run --locked --no-sync pytest -q \
  packages/structuraguard/tests/unit/llm packages/structuraguard/tests/security/llm \
  packages/structuraguard/tests/unit/structure packages/structuraguard/tests/security/structure

UV_OFFLINE=1 UV_CACHE_DIR=/Users/katana/.cache/uv PYTEST_ADDOPTS=-q \
  make lint typecheck test test-integration test-security docs lock-check test-build
git diff --check
```

Для document watchdog/loopback/packaging использовано разрешение на локальные
операции вне sandbox; LLM endpoints не вызывались. HTTP tests используют fake
transport, примеры запрещают socket events, build/install работают offline из cache.

## Остаточные ограничения

Production PII redaction и безопасный aggregate report целиком отсутствуют,
router/session facade отложена. Модельная семантическая точность и неизвестные
prompt-injection формулировки не оцениваются fake tests. Не проверялись реальные
платные LLM, live TLS/proxy, все транзитивные CVE и downstream DB permissions.
Проверка не является повторным аудитом неизменённых M4 parser libraries.
