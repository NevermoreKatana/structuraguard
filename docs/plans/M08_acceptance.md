# M08 — Проверка критериев приёмки

Дата: 2026-09-11. Основание: раздел «Критерии приёмки» [плана M08](M08_normalized_data_profiler.md),
его property matrix и фактические ограничения [ADR 0018](../adr/0018-bounded-normalized-profiling.md).

Это результаты до отдельного [security review M8](M08_security_review.md).
Последующие 15 security cases, исправления и актуальные gates описаны там.
Финальные исправления и состояние ручной передачи см. в
[актуальном checklist M8](M08_normalized_data_profiler.md#checklist-commit-pr).

Добавлены **97 test cases**: 23 unit, 5 property, 64 security и 5 contract.
Существующие assertions, markers, lint/typecheck и зависимости не ослаблялись.
Новые integration tests не понадобились: существующие четыре cases уже проверяют
реальный semantic parsing CSV/JSON/text и document session с `FakeLLMProvider`.
Все новые property-тесты используют отдельный oracle либо генератор валидного
входа с контролируемой мутацией; arbitrary sleep и платных LLM вызовов нет.

Единственное изменение production-кода — исправление распознавания PII в labels
в `profiling/pii.py`. Новые tests сначала воспроизвели три отрицательных результата,
затем подтвердили исправление. Узкий M8 suite: **194 passed**.

## Матрица «критерий → наблюдаемый тест»

K1–K7 соответствуют семи критериям плана. Пути относительны
`packages/structuraguard/tests/`; имена после `::` — pytest functions.
Пометка **новый** относится только к этому аудиту.

| Критерий / поведение | Тест | Проверяемый результат |
| --- | --- | --- |
| K1: completed empty и empty iterable | `unit/profiling/test_profiler.py::test_empty_completed_dataset`; **новый** `unit/profiling/test_m08_acceptance.py::test_empty_iterable_is_not_a_completed_dataset` | Пустой завершённый профиль; отдельный typed `missing_terminal` для отсутствующего dataset |
| K1: повторные entities, одинаковые field names в разных entity types, late/schema-only поля | **новый** `unit/profiling/test_m08_acceptance.py::test_repeated_entities_late_and_schema_only_fields_use_entity_denominator`; **новый** `property/profiling/test_m08_properties.py::test_ragged_repeated_entities_match_independent_count_oracle`; `unit/profiling/test_normalized_fingerprint.py::test_manifest_schema_only_field_is_profiled_and_hashed` | Раздельные группы, entity denominator, missing и explicit null, schema-only с нулевым и ненулевым числом entities |
| K1: profiler следует за semantic parsing, не читает исходный формат | `integration/test_normalized_profiling.py::test_real_semantic_output_and_plan_replay`; `::test_m6_document_session_profiles_normalized_entities` | Настоящий executor/session выдаёт NormalizedBatch; saved-plan replay и разные batch sizes сохраняют content |
| K2: полный проход counts/null ratios/extrema/Unicode lengths | `unit/profiling/test_profiler.py::test_sparse_mixed_unicode_and_null_denominators`; **новые** `property/profiling/test_m08_properties.py::test_ragged_repeated_entities_match_independent_count_oracle`, `::test_all_scalar_extrema_use_compatible_families_and_ignore_decimal_context` | Независимые точные oracle, integer/Decimal/float/bool/date/UTC datetime/string/null; глобальная Decimal precision не меняет профиль |
| K2: sampling не влияет на статистики; нулевые знаменатели | `property/profiling/test_properties.py::test_sampling_budget_and_global_decimal_context_do_not_change_statistics`; **новый** `unit/profiling/test_m08_acceptance.py::test_seed_and_redaction_change_samples_but_not_full_statistics`; `unit/profiling/test_inference.py::test_null_only_zero_denominators_and_literal_null_strings` | Изменение k/seed/redaction не меняет статистики/hash; raw/masked examples различаются; None при отсутствии знаменателя |
| K2: bounded exact/estimated distinct | **новый** `property/profiling/test_m08_properties.py::test_distinct_k_boundary_and_duplicates_match_exact_oracle`; `unit/profiling/test_profiler.py::test_kmv_fixed_ensemble_matches_independent_distinct_oracle`; `::test_distinct_overflow_is_explicit_and_samples_stay_bounded` | K−1/K/K+1, duplicate-heavy и all-distinct, algorithm/K в DTO, bounded sketch; фиксированный ensemble проверяет accuracy без обещания точности каждого estimate |
| K3: неоднозначность RU/EN separators и дат | `unit/profiling/test_profiler.py::test_ambiguous_locale_does_not_choose_a_number_or_date`; `unit/profiling/test_inference.py::test_date_locale_has_calendar_semantics`; `::test_foreign_number_format_is_not_silently_reinterpreted`; `property/profiling/test_properties.py::test_number_locale_policy_preserves_decimal_value` | Ranked/ambiguous вместо выбора, DMY/MDY зависят от policy, malformed/чужие grouping не переинтерпретируются |
| K3: mixed kinds, declared conflict, currencies, timezone | `unit/profiling/test_inference.py::test_incompatible_candidates_are_ranked_without_winner`; `::test_declared_email_conflicting_with_values_is_ambiguous`; `::test_conflicting_currency_extrema_are_not_merged`; `::test_currency_must_be_a_single_prefix_or_suffix`; `::test_datetime_is_utc_and_naive_string_is_ambiguous`; `::test_decimal_context_and_money_representation_are_stable` | Причины конфликта, нет currency/timezone guessing, money использует Decimal; dataset не преобразуется |
| K4: IDs, ID bytes, fields, entity types, batches | **новый** `security/profiling/test_m08_boundaries.py::test_exact_stream_budget_boundaries` | Отказ на необходимом бюджете−1, успех на границе и +1; typed resource reason |
| K4: UTF-8, numeric digits, exponent, items/depth/bytes | **новые** `security/profiling/test_m08_boundaries.py::test_scalar_limits_measure_utf8_digits_and_absolute_exponent`; `::test_preflight_rejects_only_values_above_structural_cap` | UTF-8 bytes вместо числа code points, положительный/отрицательный exponent, структурные caps−1/cap/cap+1 |
| K4: examples и global sample budget | **новый** `security/profiling/test_m08_boundaries.py::test_sample_byte_boundary_skips_whole_values_without_changing_stats`; `contract/profiling/test_ports.py::test_options_reject_inconsistent_sample_budgets`; `property/profiling/test_properties.py::test_kmv_mode_is_explicit_and_omit_retains_no_samples` | Oversized examples пропускаются целиком с coverage; несовместимое произведение budgets отвергается; OMIT ничего не сохраняет |
| K4: сокращённое pattern/context/relationship evidence | **новые** `security/profiling/test_m08_boundaries.py::test_pattern_byte_boundary_marks_incomplete_evidence`; `::test_dropped_context_prevents_raw_examples_and_claim_of_complete_scan`; `::test_relationship_limits_preserve_full_statistics_and_coverage` | Skipped/reasons, unknown/incomplete и masked examples; counts сохраняются при остановке pair evidence |
| K4: shared state, terminal manifest, serialized output, большие logical streams | `security/profiling/test_security.py::test_hard_limits_are_explicit`; `unit/profiling/test_large_stream.py::test_ten_thousand_records_use_bounded_sketch_and_samples`; `::test_trillion_record_source_is_not_materialized_before_limit` | Обязательный cap завершает вызов ошибкой; bounded samples/sketch; источник 10¹² обрывается после 40 сгенерированных records |
| K5: email/phone/UUID/URL/date/money/INN/boolean; positive/negative | `unit/profiling/test_inference.py::test_pattern_positive`; `::test_pattern_negative`; `::test_url_host_must_be_syntactically_valid`; **новые** `property/profiling/test_m08_properties.py::test_generated_dates_and_uuids_are_recognized_without_coercion`; `::test_generated_inn_check_digits_reject_one_digit_corruption` | Full-string syntactic recognition, календарная валидность, checksum и контролируемая порча ИНН; отсутствие silent normalization |
| K5: integer ID, natural key/code, categorical/free text | **новые** `unit/profiling/test_m08_acceptance.py::test_integer_identity_requires_names_uniqueness_and_no_nulls`; `::test_identity_pattern_and_code_hints_do_not_grant_approval`; `::test_categorical_and_free_text_have_independent_evidence` | Candidate требует имени/pattern, ≥20, отсутствия null/duplicates; small N и отрицательные cases; categorical/free_text независимы |
| K5: PII port, минимальный класс и отсутствие raw examples в request | **новые** `contract/profiling/test_ports.py::test_pii_contract_findings_only_raise_the_callers_classification`; `::test_classifier_port_receives_aggregates_without_raw_examples`; `::test_pii_categories_cannot_claim_a_permissive_classification` (существовал) | Все четыре входных класса; новые findings не понижают класс; classifier получает counts/categories вместо dataset/samples |
| K5: labels, поздняя PII, safe summaries/logs, no network | **новые** `security/profiling/test_m08_boundaries.py::test_each_context_label_is_classified_as_sensitive_input`; `::test_url_and_malicious_labels_are_not_executed_or_logged`; `security/profiling/test_security.py::test_late_pii_closes_previous_raw_samples_and_safe_summary`; `unit/profiling/test_profiler.py::test_safe_summary_drops_values_names_extrema_and_fingerprints` | Исправлены отдельные phone/INN-12/credential labels; поздняя находка закрывает прежние samples; canaries отсутствуют в summary/repr/logs, URL не открывается |
| K6: допустимый re-batching, ID remapping, redaction/seed | **новые** `unit/profiling/test_m08_acceptance.py::test_valid_link_groups_preserve_content_after_rebatching_and_id_remapping`; `::test_seed_and_redaction_change_samples_but_not_full_statistics`; `unit/profiling/test_profiler.py::test_sampling_and_content_hash_are_independent_of_batching_and_ids` | Целые record link groups проходят manifest validation; content hash не меняется, lineage/profile hash отделены |
| K6: equivalent representations и отдельный raw provenance | `unit/profiling/test_normalized_fingerprint.py::test_field_order_and_decimal_scale_are_irrelevant`; **новые** `unit/profiling/test_m08_acceptance.py::test_equivalent_utc_instants_have_equal_content_fingerprints`; `::test_raw_transformation_evidence_is_separate_from_normalized_content` | Перестановка fields, Decimal scale, UTC instant и raw/transformation evidence не меняют normalized content |
| K6: чувствительность к value/kind/schema/order/topology | `unit/profiling/test_normalized_fingerprint.py::test_scalar_type_and_record_order_change_content_hash`; `::test_manifest_schema_only_field_is_profiled_and_hashed`; `::test_empty_content_has_frozen_v1_golden_vector`; **новые** `unit/profiling/test_m08_acceptance.py::test_content_distinguishes_missing_null_empty_names_and_entity_types`; `::test_topology_mutation_changes_validated_complete_content_hash` | Missing/null/empty, field/entity names, entity parent, record parent/related меняют hash; versioned empty golden vector |
| K7: forged DTO, stream sequence/EOF/duplicate IDs | `security/profiling/test_security.py::test_corrupt_stream_never_completes_and_closes`; `::test_duplicate_field_values_after_model_copy_are_rejected`; `::test_forged_unicode_surrogate_is_a_safe_typed_error`; `::test_forged_manifest_preflight_error_remains_typed` | Typed failure без завершённого профиля; sanitization; obtained iterator закрыт |
| K7: classifier binding/coverage и failures | **новый** `security/profiling/test_m08_boundaries.py::test_classifier_cannot_forge_evidence_binding_or_coverage`; `security/profiling/test_security.py::test_custom_classifier_cannot_downgrade_or_forge_binding`; `::test_classifier_failure_is_redacted_and_source_closed` | Подмена field/hash, удаление findings, ложный complete и понижение класса отвергаются |
| K7: cancellation, run/cleanup deadlines и ownership | `security/profiling/test_security.py::test_cancellation_closes_owned_iterator`; `::test_repeated_cancellation_waits_for_owned_cleanup`; `::test_controlled_deadline_stops_a_logically_huge_source`; **новые** `security/profiling/test_m08_boundaries.py::test_cancellation_during_classification_closes_iterator_without_result`; `::test_cleanup_deadline_cancels_close_without_publishing_profile`; `::test_only_obtained_iterator_is_closed_and_caller_resource_stays_open` | Отмена при чтении/classification и повторная отмена cleanup; закрывается iterator, а не caller resource; нет результата до cleanup |
| K7: primary error и повторный/concurrent вызов | **новые** `security/profiling/test_m08_boundaries.py::test_primary_failure_survives_secondary_cleanup_failure_without_pii`; `unit/profiling/test_m08_acceptance.py::test_concurrent_calls_on_one_profiler_have_independent_state`; `contract/profiling/test_ports.py::test_profiler_contract_roundtrip_and_reuse` | Primary code сохраняется, secondary failure — статическая note; независимые accumulators и валидный round-trip |

## Выявленный и исправленный дефект

**Medium — пропуск PII в отдельном context label.** Классификатор склеивал
field name и labels до применения recognizers, которым нужна полная строка.
Например, `x +7 (999) 123-45-67` переставал распознаваться как phone.
То же происходило с ИНН-12 и HTTP URL с userinfo. В режиме `LOCAL_RAW` поле
получало `not_detected`, недостаточный класс и доступные raw examples.

Regression: `test_each_context_label_is_classified_as_sensitive_input` —
**3 failed до исправления**, **3 passed после**. Теперь каждый label и имя поля
сканируются отдельно, а объединённый текст используется только для keyword hints.
Фикс не расширяет budgets: максимум одно имя и восемь labels; внешних вызовов нет.
Baseline проверки внешнего PII adapter использует ту же исправленную policy.

Review через `structuraguard-review` и `structuraguard-security` ограничен этим
исправлением и новыми tests. Неисправленных подтверждённых существенных findings
в этом diff не осталось. Публичные DTO, fingerprint projection, dependency/lock,
парсеры, DB adapters и LLM providers в аудите не менялись.

## Команды и фактические результаты

Python 3.12.9, macOS 26.1 arm64. Новые tests запускались до suite модуля и gates.
Временный uv cache использован только для локальных `--no-sync` проверок;
полные gates используют основной cache, включая offline distribution build.

| Команда | Результат |
| --- | --- |
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/unit/profiling/test_m08_acceptance.py` (первый валидный набор) | 19 passed; после добавления topology/provenance — 23 cases в узком suite |
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/property/profiling/test_m08_properties.py` | 5 passed |
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/security/profiling/test_m08_boundaries.py` | 64 passed после regression fix |
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/contract/profiling/test_ports.py packages/structuraguard/tests/unit/profiling/test_m08_acceptance.py` | 32 passed |
| Узкий M8 suite, команда ниже | 194 passed |
| `make lint` | 293 files formatted; Ruff passed |
| `make typecheck` | 290 source files, passed |
| `make test` | **2684 passed**, 84 database cases deselected; 5 прежних SWIG warnings, 92.94 s |
| `make test-integration` | **22 passed**, 2746 deselected; 5 прежних SWIG warnings, 5.43 s |
| `make test-security` | **580 passed**, 23.26 s |
| `make test-build` | Wheel/sdist, offline installation/import/examples: **distribution verification OK** |
| `make docs` | **Strict build passed** после исправления ссылки |
| Проверка матрицы через AST + `git diff --check` | **80 test references verified**, whitespace check passed |

Полная последовательность: `make lint typecheck test test-integration test-security test-build`.
Exit code 0. Запуск вне sandbox нужен существующим document workers и loopback
серверам; security controls не отключались. `make docs` выполнен отдельно
после обновления отчёта. В первом docs build strict-проверка обнаружила неверный
anchor в ссылке нового отчёта; исправлена ссылка, настройки проверок сохранены.

Логи (не включены в repository):
`/private/tmp/structuraguard-m08-audit-narrow.log`,
`/private/tmp/structuraguard-m08-audit-gates.log`,
`/private/tmp/structuraguard-m08-audit-docs.log`.

```bash
uv run --locked --no-sync pytest -q \
  packages/structuraguard/tests/unit/profiling \
  packages/structuraguard/tests/property/profiling \
  packages/structuraguard/tests/security/profiling \
  packages/structuraguard/tests/contract/profiling \
  packages/structuraguard/tests/integration/test_normalized_profiling.py \
  packages/structuraguard/tests/docs/test_m08_examples.py
```

## Повторный benchmark

После окончания gates сценарии запущены последовательно на той же машине.
Команда `uv run --locked --no-sync python scripts/benchmark_normalized_profiler.py`
с аргументами из таблицы; все пять вызовов завершились успешно.

| Аргументы | Время | Values/s | Retained ledger, bytes | Samples, bytes | Peak tracemalloc, bytes |
| --- | --- | --- | --- | --- | --- |
| `--records 10000 --batch-size 100` | 3.439 s | 5816 | 12121824 | 480 | не включён |
| `--records 50000 --batch-size 1000` | 18.127 s | 5517 | 59379552 | 488 | не включён |
| `--records 10000 --batch-size 100 --allocations` | 19.706 s | 1015 | 12121824 | 480 | 9163572 |
| `--records 10000 --batch-size 100 --scenario distinct` | 3.546 s | 5640 | 12316684 | 503 | не включён |
| `--records 1000 --batch-size 100 --scenario unicode` | 0.468 s | 4275 | 1508072 | 239 | не включён |

При росте records в 5 раз время увеличилось в **5.27 раза** при указанных batch
sizes: меньше планового порога 7×. 50000-record case укладывается в default 30 s.
Режим allocations отдельно использует deadline 120 s; его timing не сравнивается
с обычным прогоном. Это наблюдение одной машины, не универсальный performance SLA.
Peak tracemalloc ниже retained ledger в этом сценарии; ни одно из них не является RSS quota.
Логи: `/private/tmp/structuraguard-m08-audit-benchmark-{small,large,allocations,distinct,unicode}.log`.

## Непроверенные сценарии и пределы подтверждения

- PostgreSQL-specific suite не запускался: audit меняет profiler/PII, DB adapters
  и транзакции не затронуты. Они не имитируются SQLite-тестами.
- Production/paid LLM, DNS/HTTP lookup распознаваемых значений не вызываются.
  M6 integration использует fake provider; отсутствие network проверяется отдельно.
- Другие Python/OS и remote CI в этой сессии не проверялись.
- Cooperative cancellation проверена при чтении, classification и cleanup, но не
  на каждой машинной инструкции синхронной validation. CPU-blocking source и
  coroutine, игнорирующая cancellation, не получают process isolation гарантий.
- Не доказаны отсутствие hash collisions и точность KMV на каждом adversarial
  distribution. Проверены bounded state, exact small oracle и fixed ensemble.
- Не выполнялась исчерпывающая комбинация всех budgets и labels/Unicode.
  Public byte limits используют консервативный accounting; test не утверждает,
  что ledger равен RSS. Общие state/manifest/output limits проверены на отказ,
  отдельные scalar/structure/ID/sample budgets — также на границах.
- PII и identity остаются эвристиками: RFC-complete email/phone, NER/DLP,
  фактическая регистрация ИНН, DB uniqueness и egress approval не входят в M8.
