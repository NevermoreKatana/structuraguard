# M05 — Отчёт о проверке критериев приёмки

Дата: 2026-09-10. Основание: [план M05](M05_parse_plan.md), раздел
«Критерии приёмки» и test matrix C3; фактическая policy ADR 0008–0010.

Это результаты отдельной проверки критериев до security review. Последующие
исправления production-кода и их проверки описаны в [security report](M05_security_review.md).

Проверяется полный M5-A/B/C. Полная автоматическая нормализация всех форматов
из первоначального C3 **не подтверждена**: XML element matching, параллельный
HTML tree scope, показанный Markdown с blank-line blocks и несколько JSONL roots
возвращают `NEEDS_SEMANTIC_ANALYSIS`. Это проверенные ограничения текущей policy,
а не успешное исполнение автоматического плана. Явные XML/HTML/Markdown планы
проходят validator/executor. Другие отложенные операции перечислены ниже.

В этой проверке добавлены 33 test cases и вспомогательный subprocess probe.
Production-код, существующие assertions и quality gates не изменялись.
Три существующих PDF/DOCX теста дополнительно помечены `integration`; они
продолжают входить в default suite. Новые native integration cases не дублируют
их: добавлен сквозной XLSX с raw lexemes и sheet coordinates.

## Матрица «критерий → наблюдаемый тест»

Номера K1–K7 соответствуют семи пунктам раздела «Критерии приёмки» плана.
Пути ниже относительны `packages/structuraguard/tests/`. Имена после `::` —
реальные pytest test functions; параметризованные cases указаны вместе.
«Новый» означает добавленный в текущей проверке тест; остальные существовали.

| Критерий / наблюдаемое поведение | Тест | Что подтверждено |
| --- | --- | --- |
| K1: пустой, versioned profile с coverage | `unit/structure/test_profiling.py::test_empty_source_has_explicit_empty_profile` | Пустые observations/evidence/candidates, полное нулевое coverage, round-trip |
| K1: bound A→B→C и terminal manifest | `unit/structure/test_execution.py::test_tabular_ranges_raw_values_and_physical_provenance` | Настоящий parser, analyzer, validator, executor; manifest сверяет все batches |
| K1: accepted/rejected, sync/async, decoded JSON, wrapper binding | **Новый:** `unit/structure/test_validation_contract.py::test_sync_async_validation_has_identical_binding_and_round_trips`; `unit/structure/test_execution.py::test_validator_requires_source_and_checks_serialized_request` | Одинаковый validation fingerprint, round-trip, без replay нет acceptance |
| K1: B `rejected`, отсутствие fallback режима | `security/structure/test_analysis_security.py::test_protocol_request_and_unsupported_mode_never_fallback` | Unsupported mode отклоняется явно |
| K2: однозначная и неоднозначная таблица | `unit/structure/test_analysis.py::test_csv_meta_header_repeated_header_footer`; `unit/structure/test_analysis.py::test_empty_unknown_ambiguous_and_threshold_are_explicit` | Автоматический plan либо несколько кандидатов; threshold не игнорируется |
| K2: однозначное/неоднозначное дерево | `unit/structure/test_analysis.py::test_nested_json_child_collections_use_literal_steps`; `unit/structure/test_analysis.py::test_independent_nested_arrays_are_ranked_without_scope_loss` | Child collections либо ranked review без потери scope |
| K2: однозначный/смешанный LOG | `unit/structure/test_analysis.py::test_log_uniform_multiline_and_mixed_variants` | Multiline plan либо несколько ранжированных templates |
| K2: положительный и неоднозначный document | `unit/structure/test_analysis.py::test_pdf_and_docx_physical_block_plans`; **новый:** `unit/structure/test_m05_acceptance.py::test_document_with_table_and_sections_keeps_competing_candidates` | PDF/DOCX plans; HTML document/table остаются конкурентами с evidence/score |
| K2: отрицательные примеры всех семейств | `security/structure/test_plan_execution_security.py::test_false_tabular_claims_never_receive_validated_plan`; `security/structure/test_analysis_security.py::test_tree_missing_fields_require_explicit_policy`; `security/structure/test_analysis_security.py::test_unknown_log_scope_and_ambiguous_footer_do_not_disappear`; **новый:** `unit/structure/test_m05_acceptance.py::test_unsupported_automatic_scope_requires_semantics_without_losing_data` | Отказ для missing fields/refs и неподдержанного scope, без угаданного результата |
| K3: header/data/meta/footer/repeated header, raggedness, поля | `unit/structure/test_profiling.py::test_tabular_headers_regions_raggedness_and_fields`; `unit/structure/test_analysis.py::test_csv_meta_header_repeated_header_footer` | Реальные row indices, ragged rows, исключаемые repeated/footer rows |
| K3: неоднозначный summary/footer | `security/structure/test_analysis_security.py::test_unknown_log_scope_and_ambiguous_footer_do_not_disappear` | Неподтверждённый footer не отбрасывается как доказанный total |
| K3: merged cells | `unit/structure/test_profiling.py::test_xlsx_merged_ranges_keep_sheet_evidence` | `A1:B1` и sheet evidence сохранены |
| K3: record roots, distributions, parent/child collections | `unit/structure/test_profiling.py::test_nested_tree_collections_preserve_parent_paths`; `unit/structure/test_profiling.py::test_xml_repeated_siblings_and_namespaces_are_collections` | Counts, nesting, namespace evidence; XML profiling не равнозначен automatic XML plan |
| K3: repeated key/value, templates, multiline, timestamp/level | `unit/structure/test_profiling.py::test_text_templates_key_values_and_multiline_candidates` | Counts, signatures и hints без executable regex |
| K3: headings/sections, nearby key/value, tables, repeated groups | `unit/structure/test_profiling.py::test_mixed_document_preserves_multiple_families` | Typed document roles и несколько структурных семейств |
| K3: primitive hints с альтернативами | **Новый:** `unit/structure/test_m05_acceptance.py::test_mixed_primitive_hints_preserve_all_observed_alternatives` | Смешанное поле хранит integer/string counts, evidence и confidence |
| K3: связь evidence и candidate | `security/structure/test_profile_security.py::test_candidate_links_are_exact_and_not_only_shared_table_refs` | Проверяются конкретные observation links, недостаточно общего table anchor |
| K4: объявленные fields/exclusions, raw values | `unit/structure/test_execution.py::test_tabular_ranges_raw_values_and_physical_provenance`; `unit/structure/test_execution.py::test_log_groups_cross_batches_and_preserve_each_raw_line` | Metadata/footer не становятся records; raw и каждый line origin сохраняются |
| K4: parent/child multiplicity | `property/structure/test_execution_properties.py::test_nested_child_count_equals_source_without_cartesian_product` | По независимым counts children, без cartesian product |
| K4: literals/occurrences и явные пропуски blocks | **Новый:** `unit/structure/test_explicit_markup_plans.py::test_explicit_markup_scope_preserves_literal_paths_and_raw_text` | XML occurrence 0/1 через segments; явные HTML/Markdown paragraph scopes, raw newline сохраняется |
| K4: несогласованный или поддельный plan не исправляется | `security/structure/test_plan_execution_security.py::test_forged_checked_wrapper_does_not_authorize_missing_rows`; **новый:** `security/structure/test_m05_acceptance_security.py::test_changed_policy_invalidates_a_serialized_acceptance` | Late mismatch не даёт terminal; смена options не переиспользует acceptance |
| K4: normalized schema/provenance tamper | **Новый:** `unit/structure/test_validation_contract.py::test_normalized_extensions_round_trip_and_reject_broken_provenance`; `security/structure/test_plan_execution_security.py::test_normalized_fingerprint_detects_post_execution_payload_changes` | Round-trip, reject foreign location/missing origins/unknown operation/legacy extension/stale hash |
| K5: большой source и bounded retained state | `unit/structure/test_profiling.py::test_large_input_keeps_configured_sample_bound_and_determinism`; `unit/structure/test_execution.py::test_explicit_large_scope_streams_beyond_profile_samples_with_bounded_state` | 5000 строк при sample budget 8; 300 rows вне sample, первый output до EOF, bounded field evidence |
| K5: N/N+1 и terminal output budget | **Новые:** `property/structure/test_m05_acceptance_properties.py::test_source_item_budget_accepts_exact_n_and_rejects_n_plus_one`; `security/structure/test_m05_acceptance_security.py::test_output_batch_budget_includes_terminal_manifest` | Full-source limit включает header, число output batches включает terminal |
| K5: pattern overflow, большой реальный scalar | **Новые:** `security/structure/test_m05_acceptance_security.py::test_pattern_overflow_is_visible_and_does_not_expand_profile_unboundedly`; `security/structure/test_m05_acceptance_security.py::test_oversized_real_value_is_excluded_instead_of_becoming_truncated_evidence` | Bounded observations/candidates, неполное coverage, oversized sample не превращается в ложное evidence |
| K5: cancellation/timeout/cleanup | `security/structure/test_profile_security.py::test_timeout_has_typed_outcome_and_cleanup`; `security/structure/test_analysis_security.py::test_analysis_cancellation_closes_stream`; `security/structure/test_plan_execution_security.py::test_cancellation_and_consumer_close_release_source`; **новый:** `security/structure/test_m05_acceptance_security.py::test_validator_interruption_closes_source_and_never_accepts` | Controlled clocks/events, propagation отмены, typed timeout, закрытие source |
| K5: backpressure и partial errors | `security/structure/test_plan_execution_security.py::test_executor_deadline_is_typed_and_has_no_timer_during_consumer_pause`; `security/structure/test_plan_execution_security.py::test_late_source_error_has_typed_issue_no_terminal_and_closes_iterator`; `security/structure/test_profile_security.py::test_incomplete_and_reordered_streams_fail_closed` | Пауза consumer не отменяется timer; повреждённый/оборванный stream не завершается успешно |
| K6: code/SQL/shell/regex/callbacks/XPath/CSS | `security/structure/test_plan_execution_security.py::test_malicious_plan_payload_is_rejected_without_source_code_execution`; **новый:** `security/structure/test_m05_acceptance_security.py::test_expression_operators_are_not_part_of_plan_grammar` | Закрытая grammar, безопасный typed reject, payload не публикуется |
| K6: no network и инертный source text | `security/structure/test_profile_security.py::test_network_is_not_used_and_regex_text_stays_data`; `security/structure/test_analysis_security.py::test_no_network_and_source_code_is_inert_data`; `security/structure/test_plan_execution_security.py::test_source_literals_do_not_trigger_code_or_network` | Сеть заменена запретом на внешней границе, literal strings не исполняются |
| K6: слои и dependencies | `smoke/test_m02_layer_boundaries.py`; `smoke/test_import_side_effects.py`; `packaging/test_metadata.py` | Smoke/packaging regressions; новые production dependencies не добавлены |
| K7: детерминизм и разбиение | `property/structure/test_profile_properties.py::test_tabular_projection_does_not_depend_on_batch_boundaries`; `property/structure/test_analysis_properties.py::test_log_explicit_groups_cover_every_physical_line_once`; **новый:** `property/structure/test_m05_acceptance_properties.py::test_profile_ranked_candidates_and_execution_are_hash_seed_independent` | Semantic projection и record coverage; subprocess seeds 1/137/99991 дают одинаковый canonical output |
| K7: Unicode, raw/provenance независимым oracle | `property/structure/test_execution_properties.py::test_raw_values_and_coordinates_survive_rebatching`; **новый:** `property/structure/test_m05_acceptance_properties.py::test_duplicate_unicode_keys_keep_both_values_and_physical_occurrences` | Source values/row coordinates и оба duplicate occurrences; порядок semantic fields определяется plan |
| C3: остальные форматы automatic path | **Новые:** `unit/structure/test_m05_acceptance.py::test_remaining_formats_preserve_values_through_m05`; `integration/test_structure_documents.py::test_xlsx_pipeline_preserves_raw_lexemes_and_sheet_coordinates` | TSV/YAML/XLSX проходят A→B→C; XML/HTML/Markdown/JSONL ограничения проверены отдельно |

## Изменённые файлы текущей проверки

- Новые: `tests/unit/structure/test_m05_acceptance.py`,
  `test_explicit_markup_plans.py`, `test_validation_contract.py`.
- Новые: `tests/property/structure/test_m05_acceptance_properties.py`,
  `_hashseed_probe.py`; `tests/security/structure/test_m05_acceptance_security.py`;
  `tests/integration/test_structure_documents.py`.
- Добавлены только integration markers: `tests/unit/structure/test_analysis.py`,
  `tests/unit/structure/test_execution.py`.
- Этот отчёт и ссылка на него в `docs/plans/M05_parse_plan.md`.

## Ограничения и непроверенные сценарии

1. Успешный automatic A→B→C для перечисленных XML/HTML/Markdown/JSONL fixtures
   не реализован текущей policy. Проверен явный отказ и отдельно XML/HTML/Markdown
   execution caller-authored plan. Composite plan для нескольких JSONL roots
   в M5 не добавлялся. Это ограничивает приёмку первоначального C3.
2. Identity, conversions, optional-missing policy, отдельные ParseRule,
   include-descendants и ambiguous legacy starts не проверяются как успешное
   execution: ADR 0010 явно оставляет их неподдержанными. Отказы не означают
   реализацию этих возможностей; существующие negative tests сохранены.
3. OCR, сложные PDF layouts, DB staging/rollback и внешние LLM не входят в эту
   проверку. Платные API не вызывались; rollback остаётся ответственностью downstream.
4. Нет benchmark на многогигабайтных файлах и измерения пикового RSS native
   библиотек. Проверены bounded counters/state, N/N+1 и небольшие реальные document
   fixtures; эти проверки не заменяют нагрузочные измерения production workload.
5. Проверка проведена в доступном Python 3.12/macOS окружении. Матрица других
   ОС/версий Python и всех optional backend versions в этой задаче не запускалась.

## Команды и фактические результаты

Все команды запускались из корня repository с
`UV_CACHE_DIR=/private/tmp/structuraguard-m05-uv-cache`.
Порядок: отдельные новые tests → все новые → модуль M5 → полный набор.
Для document tests разрешён запуск вне sandbox: watchdog использует `/bin/ps`.
Изоляция parser и проверки лимитов не отключались.

Все новые tests:

```bash
uv run --locked --no-sync pytest -q \
  packages/structuraguard/tests/unit/structure/test_m05_acceptance.py \
  packages/structuraguard/tests/unit/structure/test_explicit_markup_plans.py \
  packages/structuraguard/tests/unit/structure/test_validation_contract.py \
  packages/structuraguard/tests/property/structure/test_m05_acceptance_properties.py \
  packages/structuraguard/tests/security/structure/test_m05_acceptance_security.py \
  packages/structuraguard/tests/integration/test_structure_documents.py
```

Модуль M5:

```bash
uv run --locked --no-sync pytest -q \
  packages/structuraguard/tests/unit/structure \
  packages/structuraguard/tests/property/structure \
  packages/structuraguard/tests/security/structure \
  packages/structuraguard/tests/integration/test_structure_documents.py
```

| Команда | Фактический результат |
| --- | --- |
| Новые tests, команда выше | **33 passed**, 3.62 s |
| Модуль M5, команда выше | **146 passed**, 11.23 s |
| `make lint` | 171 файлов соответствуют Ruff format; Ruff checks passed |
| `make typecheck` | Success, 169 source files |
| `make test` | **1815 passed**, 61.01 s; 5 SWIG deprecation warnings |
| `make test-integration` | **16 passed**, 1799 deselected, 3.46 s; те же 5 warnings |
| `make test-security` | **302 passed**, 14.55 s |
| `make docs` | Strict clean build прошёл |
| `git diff --check` | Прошёл |

Проверка AST дополнительно подтвердила существование всех 58 ссылок
`файл::test_function` для unit/property/security строк матрицы.
Пять warnings относятся к SWIG bindings PDF backend, а не к failed tests.

В первых прогонах новых fixtures уточнены ожидания по публичному physical contract:
YAML сохраняет также key nodes, XLSX — исходные числовые lexemes, а semantic field
order задаёт plan. Property для duplicate keys поэтому сравнивает оба значения
по независимым occurrence 0/1, а не предполагает порядок semantic fields.
Проверки неподдерживаемого automatic scope отделены от успешного explicit execution.
Эти уточнения не меняли существующие тесты и не потребовали production fixes.

Review: новых существенных correctness/security findings в изменениях тестов нет.
Тесты не исполняют source/plan code: subprocess запускает только фиксированный
test probe; network/LLM не подключались. Результат подтверждает фактическую закрытую
policy M5 с указанными ограничениями, не полную первоначальную спецификацию C3.
