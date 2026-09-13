# M14 — проверка критериев приёмки

Дата: 2026-09-13. Основание: [план M14](M14_security_layer.md), §30.4, §32.5
и M14 текущего ТЗ, [модель угроз](../threat-model.md), ADR 0030–0033.
Проверяется текущее рабочее дерево, включая ранее реализованные изменения M14.

Результаты ниже относятся к этапу test acceptance. Последующее
[security review](M14_security_review.md) содержит дополнительно выявленные
дефекты и исправления, включая High M14-SR-03 в SQLite constraint reader.
Актуальные команды и gates после всех исправлений находятся в
[плане передачи](M14_security_layer.md#m14-commit-checks).

**Полная приёмка исходного плана M14 не подтверждена.** Реализованные controls
имеют regression evidence; ниже отдельно указаны отсутствующие implementation,
непроверенные комбинации и гарантии, требующие host backend. Зелёный suite
не закрывает эти пункты. Критерии плана и quality gates не ослаблялись.

В этой проверке добавлены только tests и документация. Дефектов production-кода
новые тесты не выявили. Две ошибки типизации первоначальных tests исправлены до
финального прогона. Внешние платные LLM API не использовались.

## Общие критерии плана {#m14-acceptance-criteria}

Номера K1–K7 соответствуют порядку списка «Критерии приёмки» в плане.
Все пути tests ниже относительны `packages/structuraguard/tests/`.

| Критерий | Наблюдаемый тест / эффект | Результат приёмки |
|---|---|---|
| K1. A–D: отрицательный input → typed decision → отсутствие запрещённого I/O/DML → safe audit | `security/policy/test_resource_policy.py::test_declared_file_size_and_unknown_kind_fail_before_read`, `test_adapter_exception_text_is_not_chained_into_security_error`; `security/injection/test_injection_review_flow.py`; `integration/database/test_m14_audit_database.py::test_target_audit_outbox_share_transaction` | Частично: отдельные границы покрыты; общего source→DLP→routing→load→HMAC lifecycle пока нет |
| K2. Immutable maxima, narrowing, deny precedence, неизвестные modes/capabilities запрещены | Новые `security/policy/test_policy_narrowing.py` — все 16 caps и property сохранения authority; `security/database/test_m14_database_policy.py::test_denies_override_allow`; `security/sandbox/test_m14_runner_boundary.py::test_every_missing_capability_denies_before_backend_start` | Реализованные resource/DB/runner boundaries покрыты; единый permissions snapshot с roots/routes отсутствует |
| K3. Отсутствие signals не разрешает egress, exact payload/evidence binding | `security/injection/test_injection_boundaries.py::test_signal_absence_cannot_overrule_other_security_controls`, `test_foreign_self_consistent_report_is_denied`; `test_injection_routing.py::test_explicit_confidential_route_still_requires_unchanged_approval`, `test_source_risk_survives_redaction_and_later_clean_scans` | Покрыта signal/routing часть; автоматический полный DLP/redaction composition не подтверждён |
| K4. Existing parser/plan/LLM/DB/staging controls сохраняются; hash не выдаётся за authentication | Existing `security/parsers/`, `security/structure/`, `security/mapping/`, `security/loading/`; `security/audit/test_m14_audit_chain.py::test_hmac_known_vector_and_closed_payload`; полный default и PostgreSQL suites | Проверяется существующее поведение без отключения local guards |
| K5. Любой real load имеет transactional HMAC, audit failure → rollback; dry_run без data-plane mutations | Новый `security/loading/test_m14_audit_admission.py`; `integration/database/test_m14_audit_database.py::test_target_audit_outbox_share_transaction`; `integration/database/test_postgresql_loader.py::test_loader_dry_run_never_claims_staging_or_uses_writer` | Signed mode и central default покрыты. Буквальное «любой real load» не выполнено: ADR 0031 сохраняет legacy target без central policy и допускает trusted configuration. Это расхождение с исходным планом, а не недостающий mock |
| K6. Sandbox interface/admission/contract; strict execution без backend запрещён | `security/sandbox/test_m14_runner_boundary.py` и новый `test_runner_responses.py`; оба runners проверяются существующим `test_runners_share_existing_validation_contract` | SDK interface покрыт; fake backend не доказывает OS isolation |
| K7. Все §30.4 и измерения §32.5 | Подробные таблицы ниже | Не закрыт: export sink, filesystem safe-open и performance baseline отсутствуют |

## Controls A–D и trust-model traceability

| Control / threat-model | Критерий → наблюдаемый тест | Покрытие и незакрытая часть |
|---|---|---|
| A1 / TB-01, SR-29 | Frozen/narrowing → новый `test_policy_narrowing.py::{test_narrowing_all_caps_preserves_authority_and_original,test_one_expanding_cap_rejects_otherwise_narrower_policy}`; forged policy → `test_resource_policy.py::test_policy_can_only_narrow_and_forged_input_does_not_serialize` | Resource maxima проверены; roots/routes/full effective snapshot остаются планом |
| A2 / TB-02, TB-04, SR-25/27 | Bytes/stream → `test_resource_policy.py::{test_source_bytes_check_before_buffer_extension,test_transport_over_return_is_rejected_before_buffering,test_session_does_not_accumulate_multiple_source_snapshots}` | Только bounded in-memory snapshot; path roots/no-follow/TOCTOU/FIFO/device/expiry не реализованы и не проверены |
| A3 / TB-02–07, SR-07/17 | N/N+1 caps, cancellation, shared budget → `security/policy/test_resource_policy.py`, `test_llm_database_resources.py`, `test_database_batches.py`, `test_document_columns.py`; late limit rollback → `integration/database/test_m14_resources.py::test_query_limit_after_dml_rolls_back_target_and_staging` | Реализованные лимиты покрыты. Native CPU/RSS enforcement требует OS backend |
| A4 / TB-02/03, SR-01–06 | XXE/YAML/HTML/OOXML и запрет unsafe fallback → `security/parsers/test_markup_security.py`, `test_document_security.py::test_ooxml_security_corpus`, `test_m04_probe_regressions.py::test_yaml_probe_never_downgrades_unsafe_tags_to_text` | Existing safety покрыта; общего warning→review/audit bridge ещё нет |
| A5 / TB-02/07, SR-12 | Exact source review перед remote parser → `security/parsers/test_tika_security.py::{test_egress_requires_source_bound_secret_review,test_entire_request_is_scanned_before_first_upload,test_reader_cannot_replace_approved_snapshot}` | Tika transport evidence сохраняется; центральный DLP veto поверх локального opt-in не подключён |
| B1 / TB-01/05/07, SR-12/20 | Email/phone/INN/SNILS/passport/card/key/token/password/DSN, floor, FP/FN → `security/privacy/test_privacy_detection.py`, `test_privacy_boundaries.py::test_documented_false_positive_and_false_negative_fixtures`, `test_match_survives_every_chunk_split`, `test_privacy_policy.py::test_redaction_cannot_lower_caller_classification` | Заявленные категории и ограниченность detector покрыты; ФИО/косвенные identifiers/произвольная обфускация не гарантированы |
| B2 / TB-01/07 | Unsafe regex и bounded work → `test_privacy_detection.py::test_unsafe_or_empty_custom_pattern_is_rejected`, `test_privacy_boundaries.py::test_scan_budget_exact_and_one_over`; existing `security/validation/test_schema_boundary.py` | Safe subset и aggregate scan budget покрыты; arbitrary regex не поддерживается |
| B3 / TB-07/08, SR-22 | Encrypted separate map, run/principal/TTL/capacity, property round-trip → `security/privacy/test_privacy_reversible.py`; spoof/unsupported input → `test_privacy_boundaries.py`, `test_privacy_policy.py` | Map/redaction port покрыт. Полный source→masked payload→approval lifecycle зависит от composition owner; физическое стирание/key compromise не моделируются |
| B4 / TB-08/09, SR-20/23 | Safe repr/errors/reports → privacy boundary tests и `unit/contracts/test_m02_security_regressions.py`; safe HMAC metadata → новый `security/audit/test_audit_acceptance.py` | Safe summaries покрыты; sink policy/CSV-XLSX formula neutralization отсутствуют |
| C1 / TB-01/05/07, SR-11 | Multilingual/direct/indirect/benign signals, severity/location → `security/injection/test_prompt_signals.py`, `test_injection_boundaries.py` | Покрыт declared signal detector. ADR 0033 допускает configured LOCAL_ONLY/OBSERVE вместо безусловного review любого signal из исходной таблицы; отсутствие signals не означает абсолютной безопасности |
| C2 / TB-07, SR-12 | Primary/fallback/local-only/RESTRICTED/CONFIDENTIAL/no tools → `security/injection/test_injection_routing.py`; shared calls/tokens → `security/policy/test_llm_database_resources.py` | Existing explicit routing и evidence gates покрыты; full DLP composition отдельно |
| C3 / TB-03/06/07, SR-10/13/16 | Review без generation/records, invalid plans без authority → `test_injection_review_flow.py`; `security/structure/test_llm_plans.py`, `test_plan_execution_security.py`; `security/mapping/test_m11_validation_security.py` | Отдельные bridge/validator boundaries покрыты; end-to-end orchestrator не поставлен |
| D1 / TB-01/05/06, SR-14–16/26/29 | Allow/deny, system/unsafe IDs, full columns, roles → `security/database/test_m14_database_policy.py`; SQLite reader rebound catalog/deny/no I/O/N+1 → `security/database/test_m14_constraint_policy.py`; `integration/database/test_m14_audit_database.py::test_postgresql_column_scope_before_full_reflection`; existing PostgreSQL inspector/loader security suites | Реальные PostgreSQL grants/state проверены. SQLite bypass M14-SR-03 исправлен после test acceptance; 15 новых cases проходят. Partial column catalog преднамеренно запрещён |
| D2 / TB-08, SR-20 | Typed stages/required evidence/counts/canonical UTC/invalid-before-store → новый `security/audit/test_audit_acceptance.py::{test_each_audit_stage_round_trips_and_authenticates,test_invalid_stage_evidence_never_reaches_store,test_canonical_timestamp_offset_does_not_change_signature}`; canaries → `test_m14_audit_chain.py` | Все 8 AuditKind проходят round-trip; 18 invalid stage cases отказывают до store. Подпись подтверждает заявление trusted owner, не истинность metadata |
| D3 / TB-08, SR-21/22 | Mutation/reorder/delete/splice/rotation/CAS → `test_m14_audit_chain.py`; общий `contract_suites/audit.py` применяется к memory и PostgreSQL; новые pagination 256/257, lazy N+1, cancellation tests → `test_audit_acceptance.py` | Core chain/store контракт покрыт. Без external anchor усечение suffix не доказуемо; key owner может переподписать историю |
| D4 / TB-06/08, SR-17/18/28 | Commit/rollback event+intent, replay/tamper/grants/sign errors → `integration/database/test_m14_audit_database.py`; новый `test_m14_audit_store_contract.py` — новый connection, rollback, transaction reuse deny, concurrent connections | Durability SDK intent подтверждена. Внешний dispatcher/duplicate delivery/ack/retry не поставлен; signed-specific ambiguous commit/restart процесса целиком отдельно не проверены, existing M13 outcome suite сохраняется |
| D5 / TB-02/03/04, SR-08–10/19/24 | All capabilities/no-start, provenance/frame/result size, cancel/start races/cleanup → `security/sandbox/test_m14_runner_boundary.py`; новый `test_runner_responses.py` — malformed probe/frame/exit и JSON depth N/N+1 | Interface с fake process; нет подтверждения network/temp/secrets/UID/cgroups/PID isolation настоящим host backend |

## Обязательные сценарии §30.4

| Сценарий | Тест → наблюдаемый эффект | Статус |
|---|---|---|
| XXE | `security/parsers/test_markup_security.py::test_xml_dtd_xxe_billion_laughs_rejected_without_io` → отказ без внешнего I/O | Покрыт |
| YAML object injection | Там же `test_yaml_no_python_or_custom_objects` → нет создания Python/custom objects | Покрыт |
| HTML script payload | Там же `test_html_xss_remains_data_and_safe_json_round_trips` → inert data | Покрыт для ingestion/safe JSON |
| Prompt injection | `security/injection/test_prompt_signals.py::test_multilingual_attacks_and_documented_false_positives`; `test_injection_routing.py` → сигналы не дают tools/DB authority | Покрыт в declared coverage |
| SQL injection in values | `integration/database/test_postgresql_loader.py::test_quoted_identifiers_and_string_parameters_are_data` → реальные значения остаются data | Покрыт PostgreSQL |
| Malicious column names | `security/mapping/test_m11_validation_security.py::test_unlisted_malicious_identifiers_remain_inert`; D1 policy tests | Покрыт |
| Path traversal | `security/parsers/test_document_security.py::test_ooxml_security_corpus` → container entries отклонены | Частично: source filesystem traversal/TOCTOU не реализованы |
| Archive bomb | Там же OOXML guards; `test_markup_security.py::test_yaml_alias_bomb_is_bounded_not_expanded` — отдельная alias threat | Покрыт для OOXML; общие архивы не поддержаны |
| Oversized file | `security/policy/test_resource_policy.py::test_declared_file_size_and_unknown_kind_fail_before_read`, `test_source_bytes_check_before_buffer_extension` | Покрыт |
| Excessive nesting | Там же `test_json_nesting_boundary`; новый `security/sandbox/test_runner_responses.py::test_ipc_json_depth_exact_and_one_over` | Покрыт |
| Unauthorized table mapping | `security/mapping/test_m09_preflight.py::test_nonwritable_and_denied_targets_never_reach_scoring`; M11 и D1 negative tests | Покрыт |
| Secret leakage in logs | `security/parsers/test_tika_transport_logging.py`, privacy exception/canary tests, `security/audit/test_m14_audit_chain.py::test_foreign_key_error_and_cancel_do_not_leak_raw_secret` и новый cancellation test | Покрыты SDK boundaries; raw caller sinks вне гарантии |
| Formula injection on export | `security/parsers/test_delimited_security.py::test_formula_like_cells_remain_raw_and_inert` подтверждает лишь ingestion | **Не проверен:** exporter/sink policy отсутствует |
| Schema drift before load | `integration/database/test_postgresql_loader.py::test_execution_rechecks_schema_and_grants_after_successful_dry_run`; `test_postgresql_load_outcomes.py::test_schema_drift_after_dml_before_commit_rolls_back_ledger` | Покрыт actual DB state |

## Performance §32.5

| Метрика / invariant | Evidence | Ограничение |
|---|---|---|
| p50/p95 latency, records/s, peak memory, technical parser throughput, ParsePlan throughput | Benchmark corpus/baseline и `benchmark_m14_security.py` из плана отсутствуют | Не измерены. Время pytest не подменяет performance baseline; нет сохранённого paired reference с commit/runtime/configuration |
| Database queries per batch | `test_database_batches.py`, `test_llm_database_resources.py::test_database_exact_query_count_then_next_adapter_is_blocked`, `integration/database/test_m14_resources.py` | Детерминированные caps проверены; статистическое измерение query overhead на benchmark corpus не выполнено |
| Bounded HMAC streaming | Новый `test_audit_acceptance.py::test_verify_pages_history_and_anchor_prefix_at_256_boundary` и `test_verification_limit_stops_lazy_input_after_one_excess_record` | Проверены размер страницы и прекращение потребления iterable; это не RSS measurement |
| Bounded DLP и отсутствие опасного накопления | Existing privacy/resource N/N+1 и chunk split tests | Проверены logical caps; CPU/RSS hard limits требуют OS runner |

## Новые файлы и команды

Добавлены 63 test cases: 55 default unit/property/security и 8 PostgreSQL cases
(4 сценария × PostgreSQL 16/18). Общий helper не считается отдельным test case.

- `tests/contract_suites/audit.py` — общий memory/PostgreSQL store contract.
- `tests/security/audit/test_audit_acceptance.py` — stage, history, cancellation.
- `tests/security/policy/test_policy_narrowing.py` — property narrowing и каждый cap.
- `tests/security/sandbox/test_runner_responses.py` — malformed responses/depth.
- `tests/security/loading/test_m14_audit_admission.py` — mandatory audit admission.
- `tests/integration/database/test_m14_audit_store_contract.py` — durable store.

Команды выполняются из корня workspace. `uv run` использует `--locked --no-sync`;
для sandbox-compatible запусков cache вынесен в
`UV_CACHE_DIR=/private/tmp/structuraguard-m14-uv-cache`. PostgreSQL и document workers
запускались с разрешённым доступом к локальному runtime/Docker. LLM — fakes/локальный HTTP.

| Команда | Фактический результат |
|---|---|
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/security/audit/test_audit_acceptance.py packages/structuraguard/tests/security/policy/test_policy_narrowing.py packages/structuraguard/tests/security/sandbox/test_runner_responses.py` | 53 passed |
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/security/loading/test_m14_audit_admission.py` | 2 passed |
| `uv run --locked --no-sync pytest -q -m database_integration packages/structuraguard/tests/integration/database/test_m14_audit_store_contract.py` | 8 passed, PostgreSQL 16/18 |
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/security/audit packages/structuraguard/tests/security/policy packages/structuraguard/tests/security/sandbox packages/structuraguard/tests/security/database/test_m14_database_policy.py` | 260 passed |
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/security/loading` | 14 passed |
| `uv run --locked --no-sync pytest -q -m database_integration packages/structuraguard/tests/integration/database/test_m14_audit_store_contract.py packages/structuraguard/tests/integration/database/test_m14_audit_database.py packages/structuraguard/tests/integration/database/test_m14_resources.py` | 42 passed, PostgreSQL 16/18 |
| `make lint typecheck` | Ruff passed; mypy: 561 source files |
| `make test` | 4278 passed, 430 deselected, 5 existing SWIG deprecation warnings; 141.63 s |
| `make test-integration` | 30 passed, 4678 deselected, 5 existing SWIG deprecation warnings; 8.20 s |
| `make test-security` | 1366 passed; 35.70 s |
| `make test-database` | 430 passed на PostgreSQL 16/18; 205.66 s; SQLAlchemy SAWarning считается ошибкой |
| `make docs` | Strict build passed; INFO о ранее не включённых в nav страницах не являются errors |

Review добавленных tests не выявил существенных correctness/security findings:
mock/spy стоят на adapter/port boundaries, PostgreSQL assertions проверяют actual
storage state, cancellation синхронизируется Events, существующие suites не
изменены. Ссылки на test files/functions в матрице проверены; `git diff --check`
прошёл. Полнота приёмки ограничена перечисленными ниже сценариями.

## Непроверенные сценарии и причины

- A2 safe-open/TOCTOU/temp expiry, A4 общий warning bridge, A5 central egress veto,
  B4 export sinks и полная K1/K3 composition требуют implementation. Тесты на
  отсутствующий API не объявлялись passed/xfail/skip; добавление функций выходит
  за текущую задачу проверки и исправления только выявленных дефектов.
- K5 исходного плана строже ADR 0031. Central default audit admission протестирован;
  обязательность подписи legacy load не утверждается. Требуется отдельное решение
  о scope при закрытии milestone, а не ослабление regression assertions.
- C1 уточнён ADR 0033 и пользовательским запросом configurable restrictions:
  LOCAL_ONLY может разрешать локальную модель после base checks. Исходная формула
  «любой suspicious signal → минимум review» не является гарантией всех configs.
- SR-09 требует реальный sandbox backend/deployment. Его нет в core SDK;
  capability declarations и fake process не заменяют эту приёмку.
- D4 external outbox dispatcher, duplicate delivery и process crash/restart всего
  ingest workflow отдельно не проверены: host implementation отсутствует. SDK store
  проверен после commit/rollback через новый connection, включая concurrent CAS.
- §32.5 не выполнен без benchmark corpus и baseline. Не вводился произвольный SLA.
- Компрометация HMAC key/store, физическое удаление masking map, provider retention
  и семантическая полнота detectors не доказываются этими tests. Ограничения
  описаны в [security controls](../security-controls.md) и [DLP](../privacy-redaction.md).
