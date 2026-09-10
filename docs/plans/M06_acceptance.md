# M06 — проверка критериев приёмки

Дата: 2026-09-10. Основание: [план M06](M06_llm_semantic_parsing.md), K1–K8,
уточнения runtime в [ADR 0014](../adr/0014-hybrid-semantic-parsing.md).
Проверяется текущая рабочая копия. DB mapping не входит в аудит.

Итог по исходному design: K5 и K7 подтверждены частично. Production PII redaction
и объединение router с semantic session отложены; весь report не является безопасным
audit payload. Зелёные тесты ограниченного runtime не закрывают эти требования.

## Матрица «критерий → наблюдаемый тест»

Пути ниже относительно `packages/structuraguard/tests/`. Тесты используют реальные
M4/M5/validator/executor; fake расположен на границе provider/HTTP/scanner.

| Критерий | Тесты | Наблюдаемый результат и граница |
| --- | --- | --- |
| K1 — три режима, zero-call deterministic/no_llm | `unit/structure/test_hybrid.py::test_modes_control_calls_and_plan_reuse`; `test_deterministic_document_does_not_need_provider_and_no_llm_is_explicit`; `unit/llm/test_router.py::test_no_llm_does_not_even_validate_payload`; **новый** `unit/structure/test_m06_acceptance.py::test_empty_document_has_no_plan_calls_or_success` | Три режима, default assisted, кэш plan, ноль calls для deterministic/disabled. Empty source во всех режимах: только `SOURCE_EMPTY`, без plan/records/fingerprint, coverage 0. Отсутствие HTTP extra проверяет installed smoke K8. |
| K2 — общий provider contract, замена adapter | `unit/llm/test_providers.py::test_shared_contract_for_scripted_outcomes`; `test_disabled_provider_runs_same_contract_without_usable_capability`; `unit/llm/test_http_provider.py::test_http_runs_shared_contract_and_separates_untrusted_input`; **новый** `unit/structure/test_m06_document_acceptance.py::test_document_provider_swap_preserves_validation_and_source_values` | Общий `contract_suites/llm.py` для fake/HTTP/disabled; typed outcomes. Fake → HTTP MockTransport сохраняет values, refs, origins, validation и coverage. Проверены native JSON Schema и JSON object fallback, identity/version metadata. |
| K3 — общий план без per-row LLM | **новый** `unit/structure/test_m06_acceptance.py::test_table_row_and_batch_growth_keeps_one_plan_call`; `unit/structure/test_llm_analysis.py::test_tenfold_row_growth_keeps_one_call_and_a_bounded_sample` | 4/40 строк × physical batch 1/7/100 × output batch 1/100: ровно один provider/scan call, точные записи и bounded payload. Отдельная C-регрессия увеличивает число строк в десять раз. |
| K4 — strict schema, refs, grammar, lineage, physical reproduction | `unit/structure/test_llm_analysis.py::test_malformed_suggestion_cannot_reach_validator`; `security/structure/test_analysis_security.py::test_unknown_operators_and_plan_fields_are_forbidden`; `security/structure/test_llm_plans.py::test_unknown_source_alias_or_path_is_rejected`, `test_commands_and_injection_in_output_are_rejected`, `test_forged_sample_value_fails_replay_before_egress`, `test_real_validator_veto_cannot_be_overridden_by_model_confidence`, `test_second_replay_change_is_not_accepted`, `test_oversized_request_response_or_compiled_plan_is_bounded`; `security/structure/test_document_semantics.py::test_model_cannot_invent_values_or_provenance`, `test_validator_rechecks_saved_span_hash_against_full_source`; **новый** `unit/structure/test_m06_document_acceptance.py::test_multispan_value_is_reconstructed_from_both_physical_sources` | Закрытый DSL/schema и реальная physical validation обязательны; неизвестные aliases/paths/quotes/offsets и executable fragments отвергаются. Независимый oracle восстанавливает составное значение по двум исходным spans. Проверяются поддержанные grammar и известные attack fixtures, не универсальное распознавание любого текста программы. |
| K5 — classification, masking, destination, budgets до egress | `security/llm/test_routing_policy.py::test_restricted_never_reaches_cloud_even_if_allowlisted`, `test_privacy_fallback_cannot_escape_local`, `test_classification_allowlist_is_enforced`, `test_approval_for_other_policy_cannot_authorize_destination`; `unit/llm/test_router.py::test_failed_attempt_does_not_refund_call_or_token_budget`, `test_deadline_covers_idle_time_and_denies_before_egress`; `security/structure/test_llm_plans.py::test_scan_denial_or_reused_approval_prevents_egress`; `security/llm/test_http_boundary.py::test_config_credentials_are_only_sent_in_headers_and_never_persist_in_metadata`, `test_actual_httpcore_logging_is_redacted_with_fake_network` | **Частично:** destination/classification/exact approval, calls/tokens/deadline и безопасный fallback подтверждены. Retry одного provider отсутствует. Тестовый scanner выдаёт approval для synthetic data, не доказывает production masking. Router и session проверяются отдельно. |
| K6 — bounded chunks, relations, dedup, остаток | `unit/structure/test_document_flow.py::test_document_exact_values_have_physical_spans`, `test_xml_parent_child_entities_keep_xpath_and_stable_record`; `unit/structure/test_document_merge.py::test_overlap_dedup_stable_order_and_reproducible_report`, `test_budgets_leave_explicit_unresolved_source`; **новые** `property/structure/test_m06_chunk_properties.py::test_unicode_chunks_reconstruct_source_without_gaps`; `unit/structure/test_m06_document_acceptance.py::test_equal_values_at_distinct_refs_survive_overlap_and_batch_changes`, `test_conflicting_fields_and_parents_never_produce_orphans` | Prose/HTML/XML/PDF/DOCX; exact spans, parent/child, byte/count caps, Unicode без разрывов, повторяемый merge. Одинаковые values с разными refs сохраняются; field/parent conflicts, cycles/missing parents дают review без orphans. Calls/chunks/tokens/entities exhaustion оставляет unresolved scope. Hypothesis: 20 фиксированных генераций. |
| K7 — terminal-only success, report/usage/privacy | `unit/structure/test_hybrid.py::test_fallback_is_explicit_preview_without_terminal`; `unit/structure/test_semantic_session.py::test_cancelled_llm_keeps_safe_attempt_and_closes_replays`, `test_close_early_stream_is_cancelled_and_not_reusable`, `test_closing_after_received_terminal_preserves_completed_report`; `security/structure/test_hybrid_review.py::test_issue_limit_cannot_hide_later_document_injection`; **новый** `security/structure/test_m06_late_failure.py::test_late_source_failure_never_commits_or_leaks_error` | **Частично:** review/rejection/exhaustion/cancel/read failure/malformed batch/cleanup failure не подтверждают normalized fingerprint. Late failure после выданного preview закрывает source и санитизирует exception canary. Report считает attempts/usage/coverage; safe attempt metadata не содержит raw content. Полный plan/report содержит source refs и semantic names: гарантия «весь report без PII» не выполнена. |
| K8 — совместимость и установленный wheel | Существующие suites `unit/contracts`, `unit/structure`, `unit/parsers`, `security`, `packaging`; `docs/test_m06_examples.py`; **новый** `test_m06_session_example_runs_offline`; `scripts/verify_distribution.py::_probe_source` через `make test-build` | M2/M4/M5 не ослаблены. Installed smoke проверяет exports/import isolation без HTTP extra и исполняет самодостаточные provider/session examples с запретом sockets. C example с test fixtures проверяется отдельно из checkout. Legacy DTO/hash и saved-plan replay покрыты существующими suites. |

## Дополнительные E2E из задачи

| Сценарий | Тест |
| --- | --- |
| CSV metadata/header/footer | `unit/structure/test_hybrid.py::test_csv_regions_use_deterministic_plan_without_llm` |
| Mixed LOG и nested JSON | `unit/structure/test_hybrid.py::test_nested_json_and_mixed_log_preserve_records`; `unit/structure/test_llm_analysis.py::test_two_log_variants_preserve_disjoint_records` |
| Nested XML с parent/child и XPath | `unit/structure/test_document_flow.py::test_xml_parent_child_entities_keep_xpath_and_stable_record` |
| PDF/DOCX договора с текстовым слоем | `unit/structure/test_document_flow.py::test_document_exact_values_have_physical_spans[pdf-contract]`, `[docx-contract]` — integration marker |
| Injection в документе | `security/structure/test_document_semantics.py::test_document_injection_never_reaches_provider`; `security/structure/test_hybrid_review.py::test_issue_limit_cannot_hide_later_document_injection` |
| Prompt version/fingerprint, controlled clocks/IDs | `unit/llm/test_providers.py::test_script_order_usage_and_prompt_are_reproducible`; `unit/structure/test_llm_analysis.py::test_identical_inputs_and_controlled_clocks_produce_identical_lineage`; `unit/structure/test_document_merge.py::test_overlap_dedup_stable_order_and_reproducible_report` |

## Изменения этой проверки

Добавлены 22 параметризованных test cases: unit, property, security и executable
documentation. Существующий fake helper получил необязательный `batch_size`;
прежний default сохранён. Contract suite повторно использован без дублирования.
Installed smoke теперь исполняет offline examples и запрещает network audit events.

Единственное изменение production runtime — ранний `SOURCE_EMPTY` в Hybrid после
проверенного полного extraction. Критерий опирается на physical counts всех batches,
а не на ограниченный source index. Регрессия сначала упала во всех трёх режимах:
возвращались `NEEDS_SEMANTIC_ANALYSIS` либо `LLM_CONTEXT_LIMIT` вместо `SOURCE_EMPTY`.
Других production дефектов новыми тестами не выявлено.

## Команды и фактические результаты

Новые тесты сначала запускались отдельно; затем затронутые модули и quality gates.
Ни lint/typecheck configuration, ни существующие assertions не ослаблялись.

- Первый запуск `pytest -q unit/structure/test_m06_acceptance.py property/structure/test_m06_chunk_properties.py`
  (с полным префиксом `packages/structuraguard/tests/`): **3 failed, 7 passed**;
  три падения — обнаруженный `SOURCE_EMPTY` defect.
- После исправления runtime и добавления остальных тестов: **47 passed** для четырёх
  новых файлов и `docs/test_m06_examples.py`; затем добавлен multispan oracle,
  `pytest -q .../unit/structure/test_m06_document_acceptance.py`: **8 passed**.
  При разработке fixtures исправлена canonical JSON serialization; проверки
  ожидаемых conflict codes сохранены. В новом assertion устранено замечание mypy
  о сравнении разных типов через `is`, без изменения смысла проверки.
- Узкие suites: команда ниже — **457 passed**. Первый запуск в sandbox дал
  **450 passed, 7 failed** из-за локального watchdog и macOS runtime `uv`;
  повтор с разрешёнными локальными операциями прошёл. Код под эти ошибки не менялся.
- `make lint typecheck`: **221 files formatted, Ruff passed; mypy: 219 files, no issues**.
- Полный запуск `make lint typecheck test test-integration test-security docs lock-check test-build`:
  **exit 0**; `make test` — **2170 passed** (84.47 s), integration — **18 passed**
  (5.25 s), security — **403 passed** (19.40 s). Lint и mypy снова прошли;
  strict MkDocs, lock-check (100 packages), wheel/sdist offline rebuild/install
  и executable installed examples — успешно. Сохранились пять upstream SWIG
  deprecation warnings document backends.
- `git diff --check` — **exit 0**, без вывода.

```bash
UV_OFFLINE=1 UV_CACHE_DIR=/Users/katana/.cache/uv uv run --locked --no-sync pytest -q \
  packages/structuraguard/tests/unit/llm \
  packages/structuraguard/tests/unit/structure \
  packages/structuraguard/tests/property/structure \
  packages/structuraguard/tests/security/llm \
  packages/structuraguard/tests/security/structure \
  packages/structuraguard/tests/docs/test_m06_examples.py \
  packages/structuraguard/tests/packaging

UV_OFFLINE=1 UV_CACHE_DIR=/Users/katana/.cache/uv PYTEST_ADDOPTS=-q \
  make lint typecheck test test-integration test-security docs lock-check test-build
git diff --check
```

## Непроверенное и причины

- Production PII detection/redaction, placeholder offset drift и безопасный aggregate
  report целиком: соответствующий security runtime отсутствует. Fake approval не
  доказывает masking; K5/K7 остаются частичными. В production требуется внешний scanner.
- Единый router → session flow и `structure_analyzers.register(...)` из исходного D:
  публичной композиции/registry нет. Это функциональный scope, а не недостающий mock;
  в задаче аудита он не реализовывался. Регистрация analyzer остаётся незакрытым пунктом design.
- Реальные LLM endpoints, качество семантических выводов модели, live rate limits
  и сетевой TLS/proxy: по условию использованы только scripted providers/MockTransport;
  результаты не являются оценкой качества реальной модели.
- OCR, произвольная LOG/XML grammar, неограниченные cross-chunk relations и DB mapping:
  вне принятого bounded M6 scope; тесты проверяют поддержку/отказы описанного DSL.

Review изменений по `structuraguard-review`/`structuraguard-security`: исправление
пустоты не добавляет egress или dependencies, lifecycle проверяется через публичный
session API, late errors санитизированы. Новых незакрытых findings в изменённом коде
не обнаружено; ограничения критериев K5/K7 и registry перечислены выше.
