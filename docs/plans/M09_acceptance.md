# M09 — Приёмка deterministic mapper

Статус: реализация, review и локальная приёмка M9 завершены 2026-09-11.

Матрица ниже сохраняет результаты тестового аудита. После security/final review
добавлены regressions; текущие результаты и checklist ручного commit/PR —
в [плане M9](M09_deterministic_mapper.md#checklist-commit-pr).

Последующий [security review](M09_security_review.md) выявил и исправил обход
operation budget через FK/identity fan-out. Его свежие результаты дополняют
сохранённые ниже результаты тестового аудита.

## Результат и границы

Реализованы `CandidateMapper`, `DeterministicMapper`, явный `MappingScope`,
Semantic Catalog, immutable результат и explanations. Старый `MappingCandidate`
и binding к normalized manifest сохранены. SDK вычисляет score, отбирает bounded
top-k, сохраняет runner-up для ambiguity и не разрешает type/policy mismatch
компенсировать весами. MappingPlan, SQL, embeddings и LLM не создаются.

Новые модули включены в explicit distribution manifest, новые contracts/port —
в публичные exports и их проверки. Production dependencies не добавлялись.
Чистые вычисления отделены от async checkpoints. См.
[руководство](../deterministic-mapping.md) и
[ADR 0019](../adr/0019-deterministic-mapping-candidates.md).

## Уточнения реализации относительно плана

- Во избежание pytest import collisions contract/property tests имеют уникальные
  имена `test_m09_mapping_contracts.py`, `test_mapper_ports.py`,
  `test_mapper_properties.py`. Они сохраняют запланированные уровни проверки.
- Final score/gap имеют 6 знаков, промежуточные произведения — 12. Это устраняет
  преждевременное округление contributions при настраиваемых weights.
- Async orchestration находится в `mapper.py`, итоговые DTO собираются отдельно
  в `_results.py`, а арифметика — в `_scores.py`. I/O в этих вычислениях нет.
- Используется полный bounded scan и heap, без optional upper-bound pruning:
  лишний heuristic shortlist не нужен для текущих ceilings и затрудняет проверку recall.
- Числовые defaults бюджета являются hard ceilings. Caller может сузить их;
  большая схема требует сужения scope. Неподдержанные dialects отклоняются явно.
- Дробные native значения не становятся INTEGER candidates. Native observed
  kinds M8 дополняют строковые pattern counts; частичный semantic pattern
  блокирует auto recommendation даже при большом score.

## Evaluation

Команда: `uv run --locked --no-sync python scripts/evaluate_deterministic_mapping.py`.
24 синтетических случая, по 12 в calibration/holdout; в каждом split 8 positive
и 4 negative. Схемы splits не пересекаются, корпус включает ru/en, aliases,
normalization, patterns, transliteration и generic ambiguity.

| Baseline | Recall@1 / @5 / @10 | MRR | False auto | Auto / review coverage |
|---|---|---|---:|---|
| Calibration | 1.000000 / 1.000000 / 1.000000 | 1.000000 | 0 | 0.083333 / 0.083333 |
| Holdout | 1.000000 / 1.000000 / 1.000000 | 1.000000 | 0 | 0.083333 / 0.000000 |

Auto precision в обоих splits — 1.000000, но каждый split содержит всего одно
auto решение. Эти цифры не подтверждают production precision или probability
calibration. Большинство sparse cases остаются rejected, сохраняя правильный
top-k для анализа. Это ожидаемое следствие фиксированных весов без перенормировки.

Детерминированная сетка выбирает на calibration weights
`0.25 / 0.30 / 0.20 / 0.10 / 0.10 / 0.05`; metrics selected совпадают с baseline.
Default weights сохранены: явного улучшения на holdout нет. Evaluator выводит
также срезы ru/en и групп; тест проверяет независимость выбора от порядка cases.

## Аудит покрытия по plan-файлу

Повторная проверка 2026-09-11 сопоставляет все семь критериев раздела
«Критерии приёмки» и все 13 групп обязательной test matrix исходного плана
с исполняемыми assertions. Добавлены 76 сценариев pytest в восьми test files;
существующие assertions, lint и typecheck не ослаблялись.

Новые тесты обозначены **новый**. Пути ниже относительны
`packages/structuraguard/tests/`; параметризованные варианты входят в число
сценариев, а Hypothesis examples не считаются отдельными pytest tests.

| Критерий плана | Наблюдаемый тест и проверяемый результат |
|---|---|
| Каждый SemanticFieldRef; ≤ k уникальных targets либо unmapped; entity types не смешиваются | **Новый** `unit/mapping/test_m09_ranking_gaps.py::test_same_field_name_in_distinct_entities_is_not_merged`; `test_ranking.py::test_empty_scope_and_unrelated_names_are_explicitly_unmapped`; `property/mapping/test_mapper_properties.py::test_catalog_order_preserves_ranking_ids_and_top_k`; **новый** `test_m09_heap_oracle.py::test_heap_matches_independent_exhaustive_sort` |
| Bindings, версии, воспроизводимый SDK breakdown; порядок и IDs независимы от hash seed/Decimal/concurrency | **Новые** `unit/mapping/test_m09_ranking_gaps.py::test_sdk_breakdown_reconstructs_final_score_and_preserves_inputs`, `contract/mapping/test_m09_lineage.py`, `test_m09_hashseed.py::test_hashseed_preserves_full_result_and_candidate_ids`; `test_mapper_ports.py::test_mapper_contract_roundtrip_and_concurrent_reuse`; `unit/mapping/test_ranking.py::test_top_one_preserves_ambiguity_and_stable_tie` |
| Ru/en имена, aliases, транслитерация; близкие конкуренты ambiguous | `unit/mapping/test_names.py`; **новые** `test_m09_ranking_gaps.py::test_same_scoped_alias_keeps_both_targets_in_review`, `test_bilingual_common_names_remain_ambiguous_across_schemas`; `test_m09_acceptance.py::test_bilingual_holdout_recall_and_false_auto` |
| Allow/deny/writability до scoring; веса не обходят type/FK ограничения | **Новые** `security/mapping/test_m09_preflight.py::test_nonwritable_and_denied_targets_never_reach_scoring`, `test_m09_fk_scope.py::test_out_of_scope_parent_cannot_remove_fk_blocker`, `unit/mapping/test_m09_ranking_gaps.py::test_name_only_weight_cannot_restore_hard_type_mismatch`; `test_context.py::test_composite_graph_checks_every_ordered_pair` |
| Отсутствующий/сокращённый evidence видим; нет I/O и изменений входа | **Новые** `unit/mapping/test_m09_evidence_gaps.py::test_missing_evidence_is_visible_under_adversarial_weights`, `test_identity_hint_requires_exact_unique_nonnull_db_key`, `test_m09_ranking_gaps.py::test_sdk_breakdown_reconstructs_final_score_and_preserves_inputs`; `security/mapping/test_boundaries.py::test_rank_has_no_file_network_or_raw_repr` |
| Top-k после context, hidden runner-up, typed failure целиком при budget | **Новые** `unit/mapping/test_m09_ranking_gaps.py::test_context_winner_survives_top_one_and_weights_change_ranking`, `test_margin_uses_hidden_runner_up_at_exact_boundary`, `property/mapping/test_m09_heap_oracle.py`, `security/mapping/test_m09_preflight.py::test_each_preflight_budget_rejects_whole_call`; `test_boundaries.py::test_budgets_raise_without_partial_success` |
| Высокий score — только кандидат; не MappingPlan/SQL/approval/import | **Новый** `contract/mapping/test_m09_lineage.py::test_changed_bindings_change_ids_without_losing_legacy_contract` проверяет точный тип результата и legacy DTO; `smoke/test_mapping_boundaries.py` запрещает adapter/provider imports и исполнение кода; `integration/test_deterministic_mapping.py` проверяет SQLite inspection → profiling → ranking и неизменность числа DB rows |

### Детализация обязательной test matrix

Финальные дополнения к критериям ambiguity, scope и budgets:
`security/mapping/test_m09_review_regressions.py::test_confusable_target_alias_cannot_resolve_neighbor_ambiguity`
сохраняет неоднозначность соседнего поля при заблокированном anchor;
`test_table_name_limits_apply_only_to_candidate_scope` проверяет omitted/denied/
empty scope и сохранение лимитов для разрешённой таблицы и её aliases.
`security/mapping/test_m09_resource_accounting.py` ограничивает FK/identity fan-out.
Все эти tests входят в итоговый набор M9.

| Группа плана | Тесты и конкретные наблюдения |
|---|---|
| Names | `test_names.py::test_normalized_forms_match_compact_name`, `test_empty_forms_and_partial_substrings_are_not_exact_matches`; exact и normalized signals проверяются в `test_ranking.py::test_wrong_type_cannot_win_by_exact_name` и новом тесте SDK breakdown |
| Transliteration | `test_names.py::test_transliteration_is_capped` (Имя клиента, Счёт/Счет); **новый** `test_m09_ranking_gaps.py::test_confusable_or_generic_exact_name_cannot_auto_map` проверяет реальный blocker при score=1 |
| Aliases | `test_names.py::test_builtin_aliases_are_bilingual` (ИНН организации/Tax ID); `test_ranking.py::test_russian_aliases_are_scoped_and_do_not_hide_competitors`; **новый** `test_same_scoped_alias_keeps_both_targets_in_review` (Контрагент/Покупатель/Customer, два targets) |
| Типы/локали | `test_compatibility.py::test_numeric_locale_evidence_remains_conditional`; **новый** `test_m09_evidence_gaps.py::test_localized_dates_remain_conditional_or_explicitly_ambiguous` (ru_RU/en_US/en_GB/unspecified, DATE против INTEGER) |
| Patterns | **Новые** `test_m09_evidence_gaps.py::test_patterns_require_target_semantics_and_full_evidence`, `test_invalid_inn_checksum_never_receives_valid_inn_bonus`, `test_category_and_identity_patterns_use_distinct_profile_flags`, `test_leading_zero_identifier_and_explicit_null_are_not_coerced`; существующие native и partial pattern tests в `test_compatibility.py` |
| Source context | `test_context.py::test_entity_context_distinguishes_same_column_names`, `test_explicit_source_labels_select_table_without_io` (восемь label kinds); **новые** `test_same_field_name_in_distinct_entities_is_not_merged`, `test_context_winner_survives_top_one_and_weights_change_ranking` |
| Отсутствующий evidence | **Новые** `test_missing_evidence_is_visible_under_adversarial_weights` (labels/null-only/unknown type/skipped scan/mixed kinds/pair_limit), `test_identity_hint_requires_exact_unique_nonnull_db_key` (exact/estimated/nullable/no constraint) |
| FK | `test_context.py::test_composite_graph_checks_every_ordered_pair` и `test_unrelated_cycle_with_reused_fk_name_does_not_mark_valid_edge_cyclic`; **новые** `test_m09_graph_gaps.py` (цепочка customers→orders→items←products, обратное направление, payload без graph bonus, isolated, actual self-FK/SCC), `security/mapping/test_m09_fk_scope.py` |
| Неоднозначность | **Новые** `test_same_scoped_alias_keeps_both_targets_in_review`, `test_two_source_fields_keep_collision_without_greedy_reassignment`, `test_bilingual_common_names_remain_ambiguous_across_schemas`; существующий top-one tie test |
| Числовые границы | `test_scores.py::test_threshold_boundaries_are_inclusive`; **новый** `test_margin_uses_hidden_runner_up_at_exact_boundary` (.099999/.100000, k=1); `test_m09_heap_oracle.py` (k=1/5/10, ties и неравные scores) |
| Детерминизм | `test_mapper_properties.py` (перестановки tables); **новые** `test_m09_lineage.py` (порядок columns/schemas/aliases, изменение bindings/config, rebatching/sampling), `test_m09_hashseed.py` (два процесса); Decimal/concurrency tests указаны выше |
| Pruning/лимиты | **Новый** `test_m09_heap_oracle.py` — независимый sort по исходным integer scores, 60 Hypothesis examples, bounded heap и runner-up; context winner и восемь отдельных preflight ceilings в новых unit/security tests; cancellation и остальные budgets в `test_boundaries.py` |
| Security | **Новый** `test_m09_preflight.py`: view/readonly/deny не достигают scoring; stale policy/foreign refs/labels/semantic targets; hostile aliases/labels/comments не попадают в diagnostics/logs. Существующие tests проверяют forged models/hash, generated/system targets, отсутствие file/network calls и отмену |

### Выявленный дефект и локальное исправление

`test_out_of_scope_parent_cannot_remove_fk_blocker` сначала дал **3 failed**:
при parent omitted/denied/read-only дочерняя колонка теряла `FK_UNRESOLVED`.
При weight(name)=1 это давало необоснованный `auto_candidate`.

Исправлен только `mapping/_context.py`: индекс ограничений child FK теперь
строится из всего переданного graph; положительное graph evidence по-прежнему
ограничивается разрешёнными endpoints. После исправления три сценария дают
score=1, явный `FK_UNRESOLVED`, статус `review`, graph signal=0.
Запуск regression вместе с существующими context tests: **15 passed**.

Review по `structuraguard-review`/`structuraguard-security` не выявил других
существенных findings в правке. В SDK не менялись scoring weights, thresholds,
DTO, SQL/LLM adapters или public API. Вспомогательные изменения находятся
только в tests и документации.

## Проверки

| Проверка | Результат этапа тестового аудита |
|---|---|
| Узкие M9 unit/contract/property/security/integration/smoke tests | 139 passed (76 новых сценариев) |
| `make lint` | 332 файла отформатированы, Ruff без ошибок |
| `make typecheck` | 328 файлов, mypy без ошибок |
| `make test` | 2878 passed, 84 database integration tests deselected |
| `make test-integration` | 23 passed |
| `make test-security` | 624 passed |
| `make docs` | Strict MkDocs build успешно |
| `make test-build` | Wheel/sdist построены offline; distribution verification OK |
| Evaluation | Baseline/selected, calibration/holdout и языковые срезы воспроизводятся |
| `PYTHONHASHSEED=1 / 987654` | Разные процессы дают одинаковые canonical result и candidate IDs при перестановке таблиц |
| Diff | `git diff --check` и whitespace новых файлов без ошибок |

### Команды повторного аудита

Новые test files запускались отдельно до общего M9 suite. Первый security
regression дал 3 failed, затем после исправления — 15 passed вместе с context
suite. Замечания mypy относились к именам локальных переменных новых tests;
исправлены без изменения assertions или конфигурации проверок.

Узкий итоговый прогон (139 passed):

```bash
env UV_CACHE_DIR=/private/tmp/structuraguard-m09-uv-cache uv run --locked --no-sync pytest \
  packages/structuraguard/tests/unit/mapping \
  packages/structuraguard/tests/security/mapping \
  packages/structuraguard/tests/property/mapping \
  packages/structuraguard/tests/contract/mapping \
  packages/structuraguard/tests/unit/contracts/test_m09_mapping_contracts.py \
  packages/structuraguard/tests/smoke/test_mapping_boundaries.py \
  packages/structuraguard/tests/integration/test_deterministic_mapping.py -q --tb=short
```

Quality gates:

```bash
env UV_CACHE_DIR=/private/tmp/structuraguard-m09-uv-cache make lint typecheck
env PYTEST_ADDOPTS='-q --tb=short' make test test-integration test-security docs test-build
# После трёх последних pattern tests — повтор полного pytest:
env PYTEST_ADDOPTS='-q --tb=short' make test
```

`PYTEST_ADDOPTS` меняет только краткость вывода; markers/skip/xfail и assertions
не менялись. Итоговые количества приведены в таблице выше. Offline evaluator
повторно запущен командой из раздела Evaluation; его JSON совпадает с результатом
первичной приёмки. Локальные журналы: `/private/tmp/structuraguard-m09-audit-gates.log`
и `/private/tmp/structuraguard-m09-audit-test-final.log`.

### Непроверенные сценарии

- Живая PostgreSQL: 84 database integration tests deselected штатным `make test`;
  отдельный `make test-database` не запускался, поскольку DB adapters не менялись.
  PostgreSQL metadata/FK поведение mapper проверено на DTO, сквозной путь — SQLite.
- Production precision, реальные пользовательские схемы и нагрузка вплотную к
  hard ceilings: корпус синтетический, tests проверяют ограничения и отказ,
  а не production accuracy, throughput или предельный RSS.
- Все комбинации сложных графов и различающиеся версии Python/Unicode не
  перебирались. Проверены указанные в матрице графы и фиксированная текущая
  Unicode version; исчерпывающего доказательства для произвольного графа нет.
- MappingPlan validation, SQL/load, фактические FK values и identity strategy
  отложены scope M9. Реальные LLM/embedding API не вызывались: они запрещены
  для milestone и не являются его проверяемым поведением.

Для полного прогона использован разрешённый запуск вне sandbox: document
watchdog, loopback fixtures и `uv build` требуют недоступных в sandbox
возможностей. Security controls не отключались. Сохранились пять имеющихся
DeprecationWarning от SWIG/PyMuPDF, не относящихся к M9. Отдельный
`make test-database` не запускался: DB adapters не менялись.

Review выполнен по `$structuraguard-review` и `$structuraguard-security`.
После исправлений существенных замечаний в текущем diff не осталось:
проверены bindings/allowlist, Decimal arithmetic, запрет type override,
составные FK и локальная уникальность FK IDs, cancellation, budgets,
отсутствие I/O/provider imports и раскрытия raw values в diagnostics.
Два новых fixtures уточнены по действующим contracts: generated expression
требует storage metadata; blocker цикла проверяется у конкретного target.

## Оставшиеся риски

- Малый синтетический корпус и коррелированные name/alias signals не доказывают
  accuracy на пользовательских схемах. Sparse profiles дают мало auto решений.
- Проверяется переданный DB snapshot; schema drift после inspection, grants,
  фактические FK values и identity strategy проверяются перед будущей загрузкой.
- Profile extrema и patterns не доказывают все domain/enum/precision constraints.
  Неизвестность остаётся blocker; исходные значения mapper не преобразует.
- Allocation budgets консервативны и не заменяют OS/RSS isolation. Большая схема
  может быть отклонена целиком; truncation результата не скрывается как успех.
