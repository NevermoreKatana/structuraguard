# Security review M10

Дата: 2026-09-11. Scope: текущий diff относительно HEAD, включая untracked
production/test/doc файлы M10. Проверены semantic contracts, preparation,
prompt projection, response validation, confidence и coordinator, exports,
перенос active-content veto и packaging manifest. Остальные модули читались
только как существующие зависимости этих границ. Основание —
[план M10](M10_llm_database_mapping.md) и
[приёмка](M10_acceptance.md).

Найдены и исправлены **три Medium** проблемы. Critical/High не обнаружены.
Все исправления ограничены M10 и подтверждены предварительно падавшими tests.
Номера строк ниже актуализированы после расширения public docstrings; результаты
проверок относятся к security-этапу. Последующая проверка документации и общих
gates записана в [PROJECT_STATE](../codex/PROJECT_STATE.md).

## M10-SR-01 — утечка исключений scanner

- **Severity:** Medium, confidentiality/observability.
- **Location:** `packages/structuraguard/src/structuraguard/mapping/semantic.py:126`,
  вызов `SecurityScanner.scan` в `_approved_request`.
- **Путь эксплуатации:** attacker-controlled metadata обрабатывается внешним
  scanner; адаптер поднимает `KeyError`, `SecurityPolicyError`, собственное
  исключение с notes либо `CancelledError` с фрагментом исходного содержимого.
  Старый ограниченный список `except` пропускал эти ошибки наружу.
- **Влияние:** raw input/PII/secret fragments могут оказаться в traceback и логах
  встраивающего приложения. Утечка возможна даже при нуле provider calls.
  Сам адаптер остаётся доверенным кодом; проблема — отсутствие нормализации его
  штатных ошибок на новой границе M10.
- **Минимальное исправление:** изолирован только `await scanner.scan` по существующему
  шаблону M6: timeout сохраняет typed code; cancellation пересоздаётся без args;
  обычные внешние exceptions превращаются в `LLM_POLICY_DENIED` без notes/cause.
  `KeyboardInterrupt`/`SystemExit` и другие process-control исключения сохраняются.
  SDK validation вне этого вызова не скрывается общим exception handler.
- **Regression:** `tests/security/mapping/test_m10_security_review.py::test_scanner_exception_text_never_escapes_m10_boundary`
  — четыре варианта. До исправления 4 failed; после проверяется отсутствие canary
  в форматированном traceback/caplog и нулевые calls/reservations.

## M10-SR-02 — дорогой regex до лимита metadata

- **Severity:** Medium, availability/resource exhaustion.
- **Location:** `packages/structuraguard/src/structuraguard/mapping/_semantic_prompt.py:65`,
  `_Projection.text`; новый guard расположен перед redaction regex.
- **Путь эксплуатации:** допустимый DB comment/description содержит длинную
  последовательность букв без `@`. Unanchored email alternative повторяет поиск
  с каждого смещения. Раньше byte limit проверялся только после `re.subn`.
  Один comment повторно обрабатывается для разных retained field candidates.
- **Влияние:** CPU amplification внутри синхронного `group_payload`, блокировка
  event loop и задержка обработки async deadline. Бounded microbenchmark до фикса:
  1024/2048/4096 ASCII chars занимали примерно 5/20/79 ms за одну проекцию.
  Это наблюдение на текущей машине, не переносимый performance threshold.
- **Минимальное исправление:** проверять codepoint/UTF-8 byte limits до regex;
  сверхлимитное значение полностью заменять `[REDACTED]`. Classification не
  понижается. Redaction provenance обновлён до `semantic_masking_v2`.
- **Regression:** `tests/security/mapping/test_m10_security_review.py::test_overlong_metadata_never_enters_redaction_regex`
  — ASCII 4096 chars и UTF-8 600 bytes. До исправления 2 failed. Test перехватывает
  дорогую операцию и проверяет её bounded input; нестабильных wall-clock assertions нет.

## M10-SR-03 — reason codes не влияли на review policy

- **Severity:** Medium, integrity/review policy bypass.
- **Location:** `packages/structuraguard/src/structuraguard/mapping/_semantic_confidence.py:145`,
  цикл aggregation по model choices.
- **Путь эксплуатации:** недоверенный structured response использует `selected`
  и высокий score, но одновременно сообщает `MULTIPLE_PLAUSIBLE_TARGETS`,
  `INSUFFICIENT_EVIDENCE` либо `NO_MATCH` для выбранного target. Аналогично модель
  может дать низкое число непроверенной альтернативе. Schema принимает эти enums,
  а прежняя aggregation учитывала status/score/review_required, игнорируя reasons.
- **Влияние:** SDK возвращал `auto`/`COMPLETED` при явно заявленной неопределённости.
  Это не исполнение SQL и не выход за candidate set, но обходит требование review
  и может ввести downstream consumer в заблуждение о semantic корректности proposal.
- **Минимальное исправление:** `MULTIPLE_PLAUSIBLE_TARGETS` в choice/assessment
  включает ambiguity и соответствующий penalty; `INSUFFICIENT_EVIDENCE` сохраняет
  review также для альтернативы; `NO_MATCH` у выбранного target блокирует auto.
  Numeric score остаётся только одним signal. Schema и provider abstraction не менялись.
- **Regression:** `test_model_uncertainty_reason_cannot_be_hidden_by_selected_status`
  (6 cases) и `test_unassessed_alternative_cannot_be_dismissed_by_low_model_score`
  (2 cases) в том же security test-файле. До фикса 8 failed с фактическим `auto`;
  после ожидаются explicit review/ambiguity.

Пути `tests/` выше относительны `packages/structuraguard/`.

## Проверка применимых угроз

| Угроза | Проверенная граница и результат |
|---|---|
| Attacker-controlled input | M7/M8 snapshots, names, aliases, descriptions, source samples и LLM response недоверенные. `validate_inputs` повторяет DTO, fingerprint и scope проверки, source data не задаёт routing policy. |
| Resource exhaustion | M9 input/pair/work budgets; M10 shape/group/top-k/state/payload/response ceilings. SR-02 исправляет CPU amplification до redaction limit. Контроль async timeout кооперативный, поэтому остаются обязательными детерминированные budgets. |
| Parser exploit / unsafe deserialization | Новых document parsers нет. Ответ — JSON object с запретом duplicate keys/NaN/trailing content и strict closed Pydantic schema; нет pickle, eval, exec, unsafe YAML, dynamic imports по input. |
| XXE/DTD/network | M10 не разбирает XML/DTD, не раскрывает entity references и не загружает URLs из metadata. Единственный предусмотренный egress — зарегистрированный M6 provider после exact approval. Перенесённый active-content veto AST-идентичен прежнему M6 helper. |
| Prompt injection / excessive agency | Trusted system template отделён от JSON data; regex — дополнительный veto, не sandbox. Нет tools/credentials/connection/SQL capability. SDK проверяет closed schema, candidate membership, table coverage и все FK pairs; SR-03 закрывает потерю explicit uncertainty. |
| PII/secrets leakage | Raw examples/extrema не отправляются, descriptors минимизируются, обязательны scanner и maximum classification. M6 дополнительно отвергает known credential canaries. Raw response не хранится; SR-01 закрывает error text/notes leakage. |
| SQL / identifier injection | Модель возвращает только bounded opaque IDs; SQL/code/commands и extra capability fields отклоняются. Targets разрешаются локально после validation. Исполнения MappingPlan/SQL в M10 нет. |
| DB allowlist/denylist bypass | Exact allow-minus-deny M9 применяется до ranking/egress; generated, non-writable, system и доказанно несовместимые targets исключены. Semantic hints и LLM не расширяют scope. |
| Schema drift | Проверяются fingerprint snapshot, semantic catalog binding и candidate-set fingerprint ответа. Подмена snapshot с прежним hash не допускается. Изменение живой БД после inspection не проверяется M10: у mapper нет DB connection; свежесть перед load относится к M11. |
| Logs/audit | SDK создаёт только safe summary/call metadata и report fingerprints; explicit result serialization sensitive. SR-01 предотвращает утечку через внешние exceptions. Собственное logging доверенного scanner/provider не sandboxed. |
| Path traversal / temporary files | Runtime M10 не открывает paths и не создаёт temporary files. Path/URL strings — metadata, не destinations. Test SQLite-файл создаётся pytest `tmp_path`; output docs/packaging используют фиксированные developer paths, не input модели. |
| Supply chain | Production dependencies/lockfile не менялись, новый provider adapter не добавлен. `pyproject.toml`, package manifest dependencies и `uv.lock` сверены с HEAD; отдельно проверяется wheel/sdist/isolated install. Это не повторный аудит всех существующих dependencies. |

## Проверки

```bash
uv run --locked --no-sync pytest packages/structuraguard/tests/security/mapping/test_m10_security_review.py -q
uv run --locked --no-sync pytest packages/structuraguard/tests/security/mapping packages/structuraguard/tests/unit/mapping packages/structuraguard/tests/unit/llm/test_semantic_mapping_contract.py packages/structuraguard/tests/unit/contracts/test_m10_semantic_mapping.py packages/structuraguard/tests/property/mapping/test_semantic_properties.py packages/structuraguard/tests/integration/test_semantic_mapping.py -q
make lint typecheck
make test
make test-integration test-security test-build
make docs
git diff --check
```

| Проверка | Фактический результат после исправлений |
|---|---|
| Новые security regressions | **14 passed** за 0.46 s; до исправлений воспроизведены 6 + 8 failures |
| Узкий набор M10/M9 и SQLite integration | **278 passed** за 5.34 s |
| `make lint` | 356 файлов отформатированы, Ruff checks passed |
| `make typecheck` | Success, 352 source files |
| `make test` | **3071 passed**, 84 deselected, 5 известных SWIG warnings; 111.31 s |
| `make test-integration` | **24 passed**, 3131 deselected; 6.38 s |
| `make test-security` | **719 passed**; 29.18 s |
| `make test-build` | wheel/sdist собраны, isolated install и `distribution verification OK` |
| `make docs`, `git diff --check` | Passed |

Для sandbox-прогонов использован отдельный `UV_CACHE_DIR`. Полный и
integration/security/build прогоны требуют localhost и `/bin/ps` существующего
memory watchdog и выполнены с разрешённым выходом из sandbox. Первый запрос
разрешения истёк по timeout; повтор был разрешён, все проверки завершены.
Отдельно сверены AST перенесённого active-content veto и отсутствие dependency/
lockfile changes. Нерешённых Critical/High/Medium findings в проверенном diff не осталось.

## Оставшиеся ограничения

- Реальные LLM endpoints/auth/vendor retention не проверялись: все вызовы — Fake
  или MockTransport. Live providers не нужны для воспроизведения findings.
- Production DLP и adversarial model accuracy не доказываются этими тестами;
  приложение внедряет доверенные scanner, metadata classification и routing policy.
- PostgreSQL-specific операции, актуальные grants, parent rows и live schema drift
  находятся вне diff/M10. Проверки M10 используют SQLite и metadata fixtures;
  84 database_integration tests исключены стандартной конфигурацией.
- SDK не изолирует произвольный код подключённых adapters. Нормализация ошибок
  защищает принадлежащий SDK канал диагностики, а не чужой logging или egress.
