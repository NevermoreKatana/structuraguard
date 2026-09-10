# M05 — Security review текущего diff

Дата: 2026-09-10. Область: текущие изменения M5-A/B/C, включая новые untracked
`structure/`, contracts, tests и documentation. Неизменённые parsers/DB/LLM
не подвергались самостоятельному аудиту; их regression suites запускаются как
проверка совместимости. Review следует [плану M05](M05_parse_plan.md) и ADR 0008–0010.

Найдены и исправлены **1 High и 2 Medium**. Critical не обнаружены. Ниже указаны
координаты исправленных мест; пути `structure/` и `contracts/` относительны
`packages/structuraguard/src/structuraguard/`. Все новые regression tests находятся
в `packages/structuraguard/tests/security/structure/test_m05_security_review.py`.

## M05-SEC-01 — High: materialization fan-out до проверки record budget

- **Место:** `structure/_runtime.py:109`, `_RecordBudget`; ранее проверка
  `_record_limit` вызывалась после построения целого `SelectedRecord`.
- **Путь эксплуатации:** недоверенный допустимый TreeParsePlan назначает много
  разных field IDs одному source value в повторяемой child collection. Plan
  и input проходят собственные size limits, но произведение числа fields на
  children материализуется до проверки `max_record_items/max_record_bytes`.
  Аналогичное позднее ограничение существовало для tabular/LOG/document fields.
  Сериализуемый validation wrapper не является capability, поэтому endpoint
  executor также обязан пресекать такой plan при runtime verification.
- **Влияние:** значительное расходование памяти и CPU сверх настроенного record
  budget; синхронная сборка record блокирует event loop до checkpoint. На больших
  допустимых входах возможно завершение worker из-за нехватки памяти.
- **Доказательство:** безопасный fixture с 24 копиями field и четырьмя children
  создал **97 SelectedValue** при `max_record_items=10`; byte-budget 1200 также
  проверялся только после всех 97 allocations. Большие payload не запускались.
- **Минимальное исправление:** локальный инкрементальный `_RecordBudget` на record,
  проверяемый при добавлении каждого entity/value и общий для child entities.
  Во всех четырёх семействах проверка предшествует удержанию очередного value.
  Максимальное превышение при вычислении кандидата ограничено одним value,
  который дополнительно ограничен input/pending-record budgets.
- **Regression:** `test_record_budget_stops_fanout_before_materializing_all_field_values`
  — items/bytes × validator/executor, счётчик allocations, typed limit issue,
  отсутствие output до отказа. Executor проверяется с forged serialized wrapper.

## M05-SEC-02 — Medium: raw source exception в diagnostics profiler/analyzer

- **Место:** `structure/profiling.py:40`, `_source_failure`, а также
  `_source_iterator` и `_next_batch`.
- **Путь эксплуатации:** содержимое файла вызывает ошибку parser adapter/source
  iterator с PII/secret в message/details. Исключение из `__aiter__`/`__anext__`
  раньше выходило из profiler и использующего его analyzer без безопасной замены.
- **Влияние:** обычное логирование exception/traceback вызывающим приложением
  может раскрыть sensitive raw input. Для воспроизведения использован только
  synthetic `secret-canary`, без реальных secrets.
- **Минимальное исправление:** sanitization только на внешнем вызове iterator:
  фиксированный message/reason, только code из `BuiltInErrorCode`, подавленный
  исходный exception context. Сохраняются стандартные категории `ParserError`
  и `SecurityPolicyError`; неизвестные ошибки становятся `StructuralProfilingError`.
  Cancellation/процессные исключения не подавляются; iterator после чтения закрывается.
- **Regression:** `test_source_exceptions_do_not_leak_into_structural_diagnostics`
  — profiler/analyzer × open/first/late read, проверка полного обычного traceback;
  `test_source_sdk_error_preserves_only_allowlisted_code` и
  `test_source_error_sanitization_preserves_parser_and_security_categories` —
  контроль code allowlist, очистки details и совместимости категорий ошибок.

## M05-SEC-03 — Medium: непроверенный normalized manifest fingerprint

- **Место:** `contracts/normalized.py:329`, `NormalizedDatasetManifest.validate_batch`,
  и `:395`, `validate_batches`.
- **Путь эксплуатации:** `model_copy`/`model_construct` позволяет заменить
  `normalized_fingerprint` в manifest и установить тот же manifest в terminal batch.
  Constructor validation обходится, а прежние методы сверяли batch hashes/summary
  и равенство terminal manifest, не перепроверяя hash самого bound manifest.
- **Влияние:** проверка целостности ошибочно сообщает успех для stale/чужого
  fingerprint. Downstream cache/audit может связать dataset с неверным fingerprint.
  Это не доказательство обхода DB allowlist: DB operations в M5 отсутствуют.
- **Минимальное исправление:** публичные `validate_batch`/`validate_batches`
  перепроверяют canonical manifest hash schema 1.1.0. При проверке целого stream
  hash manifest считается один раз, чтобы не вводить O(B²). Legacy schema 1.0.0
  сохраняет прежний контракт без обещания новых проверяемых hashes.
- **Regression:** `test_normalized_verification_rejects_a_forged_manifest_fingerprint`
  — оба публичных entrypoints отклоняют forged fingerprint. До исправления оба
  принимали payload без исключения.

## Проверенные угрозы

| Угроза | Результат review в границах diff |
| --- | --- |
| Attacker-controlled input | DTO/dict/profile/plan/source/wrapper недоверенны. Проверены preflight, discriminated unions, повторная validation и physical replay. Fixed M05-SEC-01/02/03 |
| Resource exhaustion | Source/sample/plan/depth/record/output budgets конечны; fixed раннее ограничение fan-out. Проверены cancellation, partial stream, timeout, N/N+1 и backpressure. Sync iterator остаётся cooperative |
| Parser exploit / unsafe deserialization | M5 не открывает raw containers и не вызывает pickle/YAML object construction. DTO revalidation и bounded traversal используются до применения plan. Неизменённые native parsers остаются отдельной trust boundary |
| XXE/DTD/network | Новых XML resolvers, DTD readers или network clients нет. Literal tree steps не являются XPath. Существующие `test_xml_dtd_xxe_billion_laughs_rejected_without_io`, YAML object/alias tests сохраняются |
| Prompt injection / excessive agency | LLM/provider/tools отсутствуют в M5. Source instructions остаются данными; неподдержанный режим не вызывает fallback. Callback/code operations отсутствуют в grammar |
| PII/secrets | Fixed M05-SEC-02. Model validation errors редактируются, новые issues содержат codes/reasons/counters. Profile/normalized output намеренно могут содержать sensitive data и не являются безопасными логами |
| SQL / identifier injection | SQL/DB calls отсутствуют. Имена source — literal comparisons либо данные; произвольные SQL/shell/regex/XPath/CSS operators отклоняются |
| DB allowlist/denylist bypass | Неприменимо как операция этого diff: M5 не получает DB adapter/credentials/catalog и не пишет в БД. Глобальная DB policy этим review не сертифицируется |
| Schema drift | Source/plan/profile/manifest versions, hashes, references и batch continuity проверяются. Fixed M05-SEC-03 для normalized manifest. Foreign rows/paths/blocks и forged wrappers покрыты отрицательными тестами |
| Logs/audit | Новых logging/audit sinks в M5 нет. Fixed raw exception path; default traceback без raw canary. Не гарантируется редактирование произвольного пользовательского логирования locals или raw DTO |
| Path traversal / temporary files | M5 не выполняет filesystem I/O, не создаёт temporary files и не интерпретирует source paths как пути ОС. Новый test probe запускает фиксированный модуль с `shell=False`; source data не входят в command |
| Supply chain | Production dependency manifests/lockfile и technical parser implementations не изменены. Новых production dependencies нет; внешний CVE audit неизменённых зависимостей вне области текущего diff |

## Проверки

Первичные regression reproductions завершились отказами: raw canary вышел наружу,
оба manifest entrypoints приняли forged hash, оба fan-out budgets проверились после
97 allocations. После исправления **17 security regression cases прошли**.
Узкие M5 + DTO suites: **758 passed**, 12.21 s.

Команды запускались с `UV_CACHE_DIR=/private/tmp/structuraguard-m05-uv-cache`.
Новые tests запускались до узких suites и полных gates; существующие tests,
lint/typecheck/security checks не ослаблялись.

| Команда | Фактический результат |
| --- | --- |
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/security/structure/test_m05_security_review.py` | **17 passed**, 0.84 s |
| Узкий `pytest -q` по каталогам ниже | **758 passed**, 12.21 s |
| `make lint typecheck docs` | Ruff format: **172 files already formatted**; Ruff check: **All checks passed**; mypy: **170 source files**, без ошибок; strict MkDocs build: **exit 0** |
| `make test test-integration test-security` | **1832 passed**, 61.49 s; **16 passed / 1816 deselected**, 3.48 s; **319 passed**, 15.15 s; **exit 0** |
| `git diff --check` | **exit 0**, замечаний нет |

Точная команда узкого прогона:

```sh
UV_CACHE_DIR=/private/tmp/structuraguard-m05-uv-cache uv run --locked --no-sync pytest -q \
  packages/structuraguard/tests/unit/structure \
  packages/structuraguard/tests/property/structure \
  packages/structuraguard/tests/security/structure \
  packages/structuraguard/tests/unit/contracts \
  packages/structuraguard/tests/integration/test_structure_documents.py
```

Документные tests запускались с доступом watchdog к `/bin/ps`, без отключения
isolation/limits. Полные unit/integration прогоны содержат пять предупреждений
SWIG `DeprecationWarning`; падений нет. Платные LLM API и сетевые сервисы
не использовались. После внесения результатов повторно проверена документация.

## Оставшиеся границы

- Дополнительных подтверждённых Critical/High/Medium findings в проверенном
  diff не осталось; это не утверждение о безопасности неизменённых подсистем.
- Non-terminal batches требуют downstream staging/rollback. Fingerprints
  доказывают согласованность snapshot, а не подлинность произвольного внешнего source.
- Bounded state проверялся счётчиками и малыми adversarial fixtures; гигабайтные
  stress/RSS tests, другая ОС и версии optional native backends не запускались.
- Ограничения automatic plans из [acceptance report](M05_acceptance.md) сохранены;
  LLM, DB access, новый storage и небезопасный expression engine не добавлялись.
