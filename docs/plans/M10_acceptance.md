# Приёмка M10 — Semantic DB Mapper

Дата: 2026-09-11. Основание: восемь критериев и обязательная test matrix
[плана M10](M10_llm_database_mapping.md), с уточнениями
[ADR 0020](../adr/0020-llm-semantic-mapping-proposals.md).
Проверяется предложение mapping; execution, DB writes и создание MappingPlan
не входят в этот milestone. Платные API не использовались.

Этот отчёт фиксирует этап приёмки до отдельного
[security review](M10_security_review.md); последующие исправления и актуальные
результаты того этапа приведены в security report. Последние review fixes,
итоговые gates и checklist ручной передачи находятся в
[плане M10](M10_llm_database_mapping.md#m10-handoff) и
[PROJECT_STATE](../codex/PROJECT_STATE.md#m10-review-fix-checks).

## Критерий → наблюдаемый тест {#m10-acceptance-matrix}

Пути тестов ниже относительны `packages/structuraguard/tests/`.
Пометка **новый** означает добавленное при этой приёмке покрытие.

| № | Критерий плана | Тест и наблюдаемый эффект |
|---|---|---|
| 1 | Одна bounded группа по parent_child, scope/type/writability и top-k до egress; без полного catalog/profile | **Новые** `unit/mapping/test_m10_acceptance.py::test_only_parent_child_groups_entities_into_one_request`, `test_semantic_top_k_is_a_ceiling_even_with_larger_ranking_top_k`, `test_group_limit_rejects_before_any_scanner_or_provider`; **новый** `security/mapping/test_m10_security_acceptance.py::test_scope_type_writability_pruning_happens_before_scan_and_llm`. Счётчики calls/scan, состав payload и отсутствие denied/generated/readonly/incompatible targets. Существующий `security/mapping/test_semantic_boundary.py::test_projection_omits_full_catalog_raw_samples_extrema_and_masks_comments` проверяет минимизацию. |
| 2 | Strict SemanticMappingDecision, source/candidate membership, tables/columns и ordered FK | `unit/contracts/test_m10_semantic_mapping.py::test_every_response_object_is_closed_and_required`, `test_self_score_requires_bounded_decimal_wire_string`; `unit/mapping/test_semantic_mapper.py::test_unknown_target_is_rejected_without_partial_decision`, `test_entity_split_requires_complete_related_table_candidate`, `test_composite_relation_cannot_ignore_one_source_component`; **новый** `security/mapping/test_m10_security_acceptance.py::test_composite_relation_rejects_crossed_source_anchors_even_with_known_ids`. Последний сохраняет все IDs и обе компоненты, но перекрещивает source anchors: SDK отвергает relation. |
| 3 | Reuse M6 provider/router, capabilities, budget, approval и call metadata | `unit/llm/test_semantic_mapping_contract.py` запускает общий `assert_llm_provider_contract` для Fake, NoLLM и обоих HTTP JSON modes через MockTransport; `unit/mapping/test_semantic_routing.py::test_two_groups_share_router_history_and_reservations`; `unit/mapping/test_semantic_mapper.py::test_unambiguous_choice_keeps_sdk_score_and_provider_metadata`; **новые** `test_second_group_cannot_reset_run_budget_or_return_partial_success` и `security/mapping/test_m10_security_acceptance.py::test_foreign_approval_binding_never_authorizes_m10_egress`. |
| 4 | Classification после masking не снижается; credentials не попадают даже local; запрещённый fallback без egress | `security/mapping/test_semantic_policy.py::test_cloud_fallback_cannot_lower_classification` проверяет restricted, confidential/privacy_first и route allowlist, с local backup и без; **новые** `security/mapping/test_m10_security_acceptance.py::test_incomplete_pii_evidence_escalates_classification_and_penalty`, `test_metadata_pii_and_injection_controls_cover_each_projection_source`, `test_raw_pii_categories_are_never_sent_even_without_known_regex`. Сверяются classification, captured payload, repr/summary/history и ноль вызовов запрещённого provider. |
| 5 | Confidence считает SDK; hard blockers, ambiguity и review нельзя снять self-score | `unit/mapping/test_semantic_confidence.py`: сумма всех шести M9 signals, ограниченный LLM weight, penalties, Decimal context, thresholds 0.70/0.90, скрытый competitor, model review, clamp и нулевые штрафы; **новые** `unit/mapping/test_m10_acceptance.py::test_gap_equal_to_margin_is_distinct_from_gap_below_margin`, `test_self_fk_cannot_be_auto_approved_by_maximal_llm_score`; **новый** `property/mapping/test_semantic_properties.py::test_semantic_candidates_and_scores_are_stable_under_permutations` — 12 Hypothesis examples перестановок catalog/assessments и k=1–3. |
| 6 | Явные исходы no_llm, empty, malformed, отсутствующего approval и исчерпания бюджета; без partial success | `unit/mapping/test_semantic_routing.py::test_no_llm_has_explicit_outcome_without_scan_or_generation`; `unit/mapping/test_semantic_mapper.py::test_malformed_structured_response_is_not_repaired`; `security/mapping/test_semantic_boundary.py::test_unbound_or_failed_scanner_never_approves_egress`; **новые** `unit/mapping/test_m10_acceptance.py::test_empty_admissible_set_is_explicit_without_egress`, `test_second_group_cannot_reset_run_budget_or_return_partial_success`, `test_scanner_timeout_is_typed_without_egress_or_raw_details`, `test_cancellation_during_scan_never_enters_provider_and_releases_mapper`. |
| 7 | Default Fake/controlled HTTP без внешних API; SQLite не меняется | `unit/llm/test_semantic_mapping_contract.py::test_http_modes_keep_schema_prompt_and_no_tools` проверяет wire request; `integration/test_semantic_mapping.py::test_sqlite_profile_candidates_semantic_proposal_never_write` выполняет M7→M8→M9→Fake→M10 и сравнивает bytes файла БД. Новое integration покрытие не требовалось. |
| 8 | M6–M9 API/fingerprints сохраняются; proposal не даёт полномочий импорта | **Новый** `unit/mapping/test_m10_acceptance.py::test_m10_preserves_m9_result_and_input_fingerprints` сравнивает M9 результат и serialization входных snapshots до/после M10. `smoke/test_mapping_boundaries.py` запрещает DB/execution imports; `security/mapping/test_semantic_boundary.py::test_extra_execution_capabilities_never_enter_decision` отвергает SQL/tools/operations/transformations; package verification проверяет публичные exports и изолированный wheel install. |

## Дополнительные требования policy/confidence

| Требование | Наблюдаемое покрытие |
|---|---|
| Source values и names недоверенные, injection не становится инструкцией | `security/mapping/test_semantic_policy.py::test_source_injection_never_becomes_an_instruction`; новые metadata tests проверяют labels, aliases, descriptions. Active content даёт veto, raw values заменяются `[MASKED]`. |
| Retention только после безопасного ответа, PII не восстанавливается | `test_retention_only_keeps_validated_ids_never_raw_or_restored_pii`, `test_metadata_only_retention_does_not_skip_cross_group_collision` в том же файле; `unit/contracts/test_m10_semantic_mapping.py::test_raw_response_retention_cannot_be_enabled`. Два режима retention, unsafe response, отсутствие raw text в result/history. |
| Warning scanner — численный штраф и review, с provenance | `security/mapping/test_semantic_policy.py::test_approved_security_warning_penalizes_sdk_score_and_prevents_auto`; новый incomplete-PII test. Проверяются score 0.798, security penalty 0.20 и report fingerprint. |

Общие M6/M9 сценарии не дублировались: provider transport/status/stream limits —
`unit/llm/test_http_provider.py`; классификация, capabilities drift, route policy —
`security/llm/test_routing_policy.py`; exception/log redaction —
`security/llm/test_provider_boundary.py`, `test_http_boundary.py`,
`test_m06_error_boundaries.py`; stale scope/schema и input budgets —
`security/mapping/test_m09_preflight.py`; FK scope и unresolved parent —
`security/mapping/test_m09_fk_scope.py`. Они входят в полный прогон.

## Выявленные и исправленные дефекты

Каждое исправление внесено после падения нового regression test.

| Дефект | Наблюдение до исправления | Минимальное исправление |
|---|---|---|
| M9 ranking_options расширяли M10 column top-k | При M10 k=1 возвращались три columns | Эффективный M9 top-k ограничен M10 ceiling после проверки M9 options; competitor count сохраняется |
| Timeout scanner ошибочно считался policy denial | Ожидался `LLM_TIMEOUT`, получен `LLM_POLICY_DENIED` | `TimeoutError` обрабатывается перед родительским `OSError`, без raw details |
| Не сверялся scan request_id | Report с чужим request_id приводил к provider call и `LLM_SCHEMA_VIOLATION` вместо отказа до egress | Проверка report.request_id против SecurityScanRequest.request_id на границе M10; M6 формат не меняется |

Из production-кода изменены только `mapping/_semantic_candidates.py` и
`mapping/semantic.py`. Добавлены три test-файла и fixture `related_profile`;
существующие assertions, lint/typecheck настройки не ослаблены.

## Команды и результаты

Команды выполняются из корня repository. Для sandbox-прогонов использован
`UV_CACHE_DIR=/private/tmp/structuraguard-m10-uv`; это не меняет код или проверки.

```bash
uv run --locked --no-sync pytest packages/structuraguard/tests/security/mapping/test_m10_security_acceptance.py packages/structuraguard/tests/unit/mapping/test_m10_acceptance.py packages/structuraguard/tests/property/mapping/test_semantic_properties.py -q
uv run --locked --no-sync pytest packages/structuraguard/tests/unit/mapping packages/structuraguard/tests/unit/contracts/test_m10_semantic_mapping.py packages/structuraguard/tests/unit/llm/test_semantic_mapping_contract.py packages/structuraguard/tests/security/mapping packages/structuraguard/tests/property/mapping/test_semantic_properties.py packages/structuraguard/tests/integration/test_semantic_mapping.py -q
make lint typecheck
make test
make test-integration test-security test-build
make docs
git diff --check
```

| Прогон | Фактический результат |
|---|---|
| Новые unit/security/property tests, первая команда | **39 passed** за 1.16 s; property test выполняет до 12 Hypothesis examples |
| Узкие тесты затронутых модулей, вторая команда | **264 passed** за 5.16 s |
| `make lint` | 355 файлов отформатированы; Ruff checks passed |
| `make typecheck` | Success, 351 source files |
| `make test` | **3057 passed**, 84 deselected, 5 SWIG deprecation warnings; 102.67 s |
| `make test-integration` | **24 passed**, 3117 deselected; 5.64 s |
| `make test-security` | **705 passed**; 26.00 s |
| `make test-build` | wheel и sdist собраны, isolated install и `distribution verification OK` |
| `make docs` | Strict build passed |
| `git diff --check` | Passed; дополнительно проверен whitespace новых untracked файлов |

Полный и integration/security/build прогоны выполнялись вне filesystem sandbox:
существующим тестам нужны localhost servers, subprocess memory watchdog (`/bin/ps`)
и локальный uv cache. Внешние LLM API не вызываются. 24 явные ссылки на test node
в этой матрице дополнительно сверены с AST файлов. Review production diff:
изменено девять строк логики в двух файлах, только по трём regression failures;
существенных нерешённых findings в этом diff не осталось.

## Непроверенные сценарии и причины

- Реальные облачные/local LLM deployments, auth и vendor retention: запрещены
  внешние платные вызовы, используются Fake/MockTransport. Это не проверка качества
  ответов конкретной модели или её server-side хранения.
- PostgreSQL-specific adapter operations и grants: M10 не меняет M7 SQL/adapter;
  план допускает SQLite и catalog fixtures. 84 database_integration теста исключены
  стандартным default suite, Docker/PostgreSQL прогон в этой задаче не выполнялся.
- Полнота production DLP, распознавание пользовательских PII patterns в произвольном
  тексте и adversarial recall: внешний trusted SecurityScanner не реализуется M10.
  Тесты подтверждают обязательное approval, bindings и минимизацию canaries, а не
  полноту детектора. Raw source values исключены независимо от regex.
- Калибровка confidence на реальном holdout и recall при потере правильного target
  из top-k: текущая приёмка проверяет формулу и conservative review, не accuracy
  модели. План относит этот benchmark к последующему этапу.
- Load/MappingPlan validation, grants/parent rows, live schema drift и стратегия
  циклов: за пределами M10; proposal явно сохраняет требование review.
