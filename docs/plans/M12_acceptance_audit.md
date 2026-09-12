# M12 — аудит критериев приёмки

Дата: 2026-09-13. Основание — девять критериев общего
[плана M12](M12_validation_engine.md) и поставленные API A–D.
**Общий milestone выполнен частично.** Standalone-сервисы проверены; отсутствие
сквозной integration не заменяется тестом с вручную объявленными completed layers.
Границы поставки уже описаны в ADR
[0022](../adr/0022-conservative-normalization-and-validation.md),
[0023](../adr/0023-local-json-schema-validation.md),
[0024](../adr/0024-deterministic-record-constraints.md),
[0025](../adr/0025-provenance-replay-and-validation-report.md).

Последующий [security review текущего diff](M12_security_review.md) закрыл три Medium
findings и добавил 11 regressions. Его финальные gates указаны отдельно; числа ниже
сохраняют результаты исходного аудита приёмки.

## Критерий → наблюдаемый тест

Все пути тестов ниже относительно `packages/structuraguard/tests/`.
Параметризованные тесты считаются по собранным pytest cases, а не по числу функций.

| ID | Критерий плана | Наблюдаемая проверка | Статус |
|---|---|---|---|
| AC-01 | Примеры §15, явная policy, повторная обработка без новых изменений | `unit/normalization/test_normalizers.py::test_builtins_preserve_raw_and_record_every_change`; новый `contract/normalization/test_m12_idempotence.py::test_every_builtin_accepts_its_output_without_another_change` — все 11 built-ins | Покрыт для scalar API A; история исходного результата сохраняется |
| AC-02 | Неоднозначные дата/число, raw/origin/selected input, money только Decimal | `test_normalizers.py::test_invalid_and_ambiguous_values_are_not_repaired`, `test_numeric_normalizers_do_not_coerce_bool_or_float`; `unit/normalization/test_normalizer_registry.py::test_normalize_value_preserves_raw_parent_selection_and_provenance`; `property/normalization/test_normalization_properties.py` | Покрыт для A |
| AC-03 | Draft 2020-12, object/array projection, constraints/compositions, локальные refs без retrieval I/O | `contract/validation/test_draft202012.py::test_supported_draft_keywords`; `unit/validation/test_json_schema.py`; `security/validation/test_schema_boundary.py` | Частично: caller-provided object/array проверяется; автоматическая projection records/entities/children с обратным индексом отсутствует |
| AC-04 | Категории §16.4, локальные checks, read-only подтверждение либо unverified для CHECK/UNIQUE/FK | `unit/validation/test_db_constraints.py`; новый `security/validation/test_m12_unknown_constraints.py`; `integration/test_sqlite_record_validation.py`; `integration/database/test_postgresql_record_validation.py` | Покрыт в catalog/adapter scope C; неизвестная семантика не получает pass |
| AC-05 | 16 DSL-операций: positive/negative/null/missing/boundary, инертный SQL/Python | `contract/validation/test_rule_operations.py`, новый `test_m12_rule_edge_cases.py`, `unit/validation/test_business_rules.py`, `security/validation/test_rule_boundary.py` | Покрыт в C; пооператорная матрица ниже |
| AC-06 | Подмена pointers/raw/selector/trace/fingerprints/чужого batch даже после rehash | `security/validation/test_provenance_boundary.py`; `unit/validation/test_provenance_report.py::test_rehashed_fake_pointer_is_not_evidence`; новый `security/validation/test_m12_provenance_acceptance.py` | Частично: source → ParsePlan → normalization проверены; автоматического binding к актуальному MappingPlan/catalog после normalization нет |
| AC-07 | Независимые ошибки всех уровней в одном dataset, stable order, отсутствие выдуманных зависимых нарушений | `test_provenance_report.py::test_report_aggregates_all_layers_counts_records_once_and_orders_issues`; `property/validation/test_provenance_properties.py::test_issue_permutations_preserve_order_hash_and_aggregates`; новый `property/validation/test_m12_report_hash_seed.py`; null/missing cases DSL | Частично: проверены отдельные validators и trusted aggregation. Общий engine, автоматический dependency graph и per-check `blocked_by`/`not_applicable` не поставлены |
| AC-08 | Exact uniqueness/FK/sums между batches, завершение только после terminal/EOF/cleanup, limits исключают ACCEPTED | `test_db_constraints.py::test_late_incoming_parent_resolves_composite_fk`, `test_composite_uniqueness_is_ordered_and_marks_every_duplicate`; `test_business_rules.py::test_sum_uses_complete_parent_scope_and_exact_decimal`; `test_provenance_report.py::test_normalized_batch_boundaries_do_not_change_record_verification`; late failure/cancellation/budget tests D и новый cleanup regression | Частично: C работает на completed dataset, D проверяет завершённые snapshots. Автоматическая сборка межуровневого dataset из stream отсутствует |
| AC-09 | Legacy M2/M5/M8/M11, новые consumers, отсутствие DDL/записи/LLM repair/import I/O | `unit/contracts/test_m02_contracts.py`; suites `unit/structure`, `unit/profiling`, `unit/mapping`; `contract/mapping/test_m11_hashseed.py`; `smoke/test_dependency_boundary.py`, `smoke/test_m02_layer_boundaries.py`; SQLite/PostgreSQL read-only tests; packaging verifier | Частично: legacy regression gates и additive report 1.1 проверены; derived normalized schema 1.3 и миграция M8–M11 consumers не реализованы |

## Дополнительные требования A–D

| Требование | Тесты и наблюдаемый результат |
|---|---|
| Custom normalizer protocol, version/config, immutable snapshot | `unit/normalization/test_normalizer_registry.py`, `contract/normalization/test_protocol.py`: один protocol для built-ins/custom, exact version, frozen options, trace и вход не мутируют |
| Unicode, separators, round-trip, ambiguous cases | Hypothesis в `property/normalization/test_normalization_properties.py`: Unicode trim/digits, RU grouping, неизвестные separators, неоднозначная дата, ISO date/UUID, email case и phone digits |
| Нормализаторы без I/O и process locale | `security/normalization/test_boundary.py`: запрещённые I/O hooks не вызываются; текст инертен; ограничения input/trace и протокола дают отказ |
| Meta-validation, malformed/recursive/oversized schemas, bounded cache | `unit/validation/test_json_schema.py`, `security/validation/test_schema_boundary.py`: все malformed issues, guarded recursion, refs/resources, depth/properties/regex/input/issue/cache limits, per-instance cache и изменение fingerprints |
| JSON path/code, all-errors без restricted values | `unit/validation/test_json_schema.py`, `property/validation/test_schema_properties.py`: несколько независимых failures, Unicode paths, стабильность порядка и безопасное представление |
| NOT NULL, length, numeric bounds/scale, enum/CHECK | `unit/validation/test_db_constraints.py`: ошибки собираются вместе; неизвестный CHECK остаётся unverified; bool/float/строки не чинятся под целевой тип; Decimal не округляется |
| Composite UNIQUE/FK, read-only catalog/adapter boundary | SQLite и PostgreSQL integration выше; `security/validation/test_constraint_reader.py`: scope/type до connection, parameterized values, catalog drift, forged response, timeout/query budgets |
| Неизвестные key semantics | Новый `test_m12_unknown_constraints.py`: partial/expression index, collation/operator class, deferred unique; неизвестный/deferred/MATCH PARTIAL FK не подтверждается даже входным parent. ForbiddenReader фиксирует отсутствие неподтверждённого lookup |
| Source ref существует и принадлежит source ID/fingerprint | Реальный M5 replay в `test_provenance_boundary.py::test_foreign_source_even_with_consistent_hashes`, `test_forged_value_evidence_is_rejected`; JSON/CSV/LOG/XML/HTML/PDF/DOCX/XLSX через `contract/validation/test_provenance_formats.py` |
| Required provenance и normalization evidence | `test_provenance_report.py::test_non_null_without_origins_needs_review`, `test_missing_normalization_is_unverified_and_not_silently_skipped`, `test_normalization_trace_is_replayed_and_links_raw_locations`; новые policy/registry/step подмены не достигают callback |
| Raw/normalized references, safe summary | `test_provenance_report.py::test_replay_report_preserves_references_and_safe_summary`; `test_provenance_boundary.py::test_locations_are_never_opened_and_safe_summary_hides_arbitrary_codes`: нет значений, IDs, pointers, fingerprints или произвольных codes в summary |
| Deterministic issues, valid/invalid/unresolved/warnings | Permutations property test, новый hash-seed regression (два отдельных процесса), `unit/validation/test_m12_report_edges.py`: пустой dataset, warning-only result, missing layer и issue budget |
| Поддельные IDs не приводят к исключению report DTO | Новый `test_m12_provenance_acceptance.py::test_rehashed_cross_batch_duplicate_ids_return_failed_report`: record/entity/value duplicates после rehash дают rejected report с нулевой verification; verified duplicate IDs запрещены отдельной проверкой |

## Матрица 16 DSL-операций

Обозначения: `C` — `contract/validation/test_rule_operations.py`, `E` — новый
`contract/validation/test_m12_rule_edge_cases.py`, `U` —
`unit/validation/test_business_rules.py`. Во всех cases используется публичный
`BusinessRuleValidator`; rules проходят закрытый DTO intake. Общие injection,
wrong types, unknown operations/fields, cycles и budgets проверяет
`security/validation/test_rule_boundary.py`.

| Операция | Positive / negative / boundary | Null / missing / типы |
|---|---|---|
| `equals` | C `test_comparisons`, включая равные значения | U `test_all_errors_and_null_are_explicit`, `test_explicit_null_comparison_policy` |
| `not_equals` | C `test_comparisons`, включая равенство | E `test_comparison_missing_null_and_types_have_exact_issues` |
| `gt` | C `test_comparisons`, строгое равенство — failure | E comparison cases; wrong type также U all-errors |
| `gte` | C `test_comparisons`, равенство — pass | E comparison cases |
| `lt` | C `test_comparisons`, строгое равенство — failure | E comparison cases |
| `lte` | C `test_comparisons`, равенство — pass | E comparison cases |
| `date_lt` | C `test_dates`, високосный день/равенство | E comparison cases |
| `date_lte` | C `test_dates`, равенство/граница года | E comparison cases |
| `required_if` | C `test_presence`; E `test_required_if_does_not_invent_dependent_presence_error`, false condition не требует поле | C missing/null dependent field и present zero; E missing/null/wrong-type condition без ложной dependent ошибки |
| `at_least_one` | C `test_presence`, ноль является значением | C missing; E `test_presence_counts_non_null_values_including_zero` |
| `mutually_exclusive` | C `test_presence`, два значения нарушают правило | C missing; E presence cases, два null не считаются значениями |
| `sum_equals` | U `test_sum_uses_complete_parent_scope_and_exact_decimal`; C `test_tolerance_requires_trusted_policy`; Hypothesis exact Decimal sums | U null item/empty zero sum; E `test_sum_unavailable_operand_does_not_create_false_sum_violation` — missing total/term и wrong-type term |
| `min_items` | C `test_item_count`; E `test_item_count_empty_scope_and_null_valued_child`, точная граница 0 | У count нет scalar operand: пустая коллекция считается 0, ребёнок с null field — 1; E `test_item_count_rejects_orphan_instead_of_hiding_it` |
| `max_items` | C `test_item_count`; E empty/null-child, точная граница 0 | Та же collection semantics; orphan не пропускается |
| `unique_by` | U all-errors; E `test_parent_scoped_uniqueness_keeps_identical_keys_of_other_parents`; составные Unicode keys в property tests | C `test_unique_nulls` для distinct/equal/reject; E `test_keys_require_typed_present_operands` |
| `matches_reference` | C `test_reference_is_typed_and_fingerprint_bound`; E absent-key case | E `test_keys_require_typed_present_operands`: missing/null/wrong type; stale fingerprint в C |

`at_least_one`/`mutually_exclusive` проверяют присутствие, поэтому не вводят
самостоятельное ограничение scalar type. `min_items`/`max_items` считают дочерние
records, поэтому искусственные null/missing scalar cases к ним неприменимы.

## Изменения аудита и воспроизведённый дефект

Добавлены только отсутствовавшие проверки: 78 cases в шести файлах:

| Новый файл | Cases |
|---|---:|
| `contract/normalization/test_m12_idempotence.py` | 11 |
| `contract/validation/test_m12_rule_edge_cases.py` | 44 |
| `security/validation/test_m12_provenance_acceptance.py` | 11 |
| `security/validation/test_m12_unknown_constraints.py` | 8 |
| `unit/validation/test_m12_report_edges.py` | 3 |
| `property/validation/test_m12_report_hash_seed.py` | 1 |

Дефект production-кода воспроизведён до исправления: `ProvenanceValidator` обнаруживал
повторный `value_id` между batches, но `DetailedValidationReport` отвергал даже
непроверенные evidence с повторным claimed ID. Вместо rejected report наружу выходил
Pydantic `ValidationError`. В `contracts/provenance.py` разделены уникальность
artifact coordinates и подтверждённых value IDs. Ошибочный snapshot сохраняет
непроверенные дубликаты для диагностики; verified duplicates и ACCEPTED с такими
дубликатами по-прежнему запрещены. Regression проверяет конкретный инвариант отказа,
JSON round-trip, rejected decision и нулевую coverage.

Первоначальные ошибки новых fixtures (неполная FK metadata и integer с ведущими
нулями) исправлены в самих новых тестах: существующая conservative grammar не
менялась. Старые tests, lint, typecheck, production dependencies и их настройки
в рамках аудита не изменены. Несвязанный refactor не выполнялся.

## Команды и фактические результаты

Запуск из корня workspace, macOS/Python 3.12.9. Новые tests выполнены до узкого
suite, затем gates. Все перечисленные финальные команды завершились с exit 0.

```bash
UV_CACHE_DIR=/private/tmp/structuraguard-m12-uv-cache uv run --locked --no-sync pytest -q --tb=short \
  packages/structuraguard/tests/contract/normalization/test_m12_idempotence.py \
  packages/structuraguard/tests/contract/validation/test_m12_rule_edge_cases.py \
  packages/structuraguard/tests/security/validation/test_m12_provenance_acceptance.py \
  packages/structuraguard/tests/security/validation/test_m12_unknown_constraints.py \
  packages/structuraguard/tests/unit/validation/test_m12_report_edges.py \
  packages/structuraguard/tests/property/validation/test_m12_report_hash_seed.py

UV_OFFLINE=1 uv run --locked --no-sync pytest -q --tb=short \
  packages/structuraguard/tests/{unit,contract,property,security}/{normalization,validation} \
  packages/structuraguard/tests/docs/test_m12_{json_schema_examples,rule_examples,provenance_example}.py \
  packages/structuraguard/tests/integration/test_sqlite_record_validation.py

UV_CACHE_DIR=/private/tmp/structuraguard-m12-uv-cache make lint typecheck
UV_OFFLINE=1 PYTEST_ADDOPTS='-q --tb=short' make test test-integration test-security test-database test-build lock-check
UV_CACHE_DIR=/private/tmp/structuraguard-m12-uv-cache make docs
git diff --check
```

| Проверка | Фактический результат |
|---|---|
| Шесть новых файлов | 78 passed |
| Уточнённый regression verified duplicate IDs | 11 passed в provenance acceptance file |
| Узкий набор A–D, docs examples и SQLite | 377 passed |
| `make lint` | 450 файлов; format/check OK |
| `make typecheck` | strict mypy, 446 файлов; OK |
| `make test` | 3779 passed, 112 PostgreSQL cases deselected; 116.87 s |
| `make test-integration` | 30 passed, 3861 deselected; 6.93 s |
| `make test-security` | 911 passed; 28.74 s |
| `make test-database` | 112 passed, PostgreSQL 16/18, SAWarning как error; 17.05 s |
| `make test-build` | Offline wheel/sdist build, reinstall и `distribution verification OK` |
| `make lock-check` | 106 packages; lock актуален |
| `make docs` | Strict MkDocs build и проверка локальных links/anchors; OK |
| `git diff --check` | OK |

Main/integration вывели пять существующих DeprecationWarning от SWIG types PDF
backend; фильтры warnings не менялись. Integration/security входят также в main:
эти числа не суммируются как количество уникальных tests. Новых integration tests
не добавлено, поскольку необходимые SQLite/PostgreSQL и parser adapter cases уже
существовали и повторно прошли. Для watchdog, loopback и локального Docker использован
разрешённый запуск вне sandbox; dependencies и packaging проверялись offline.

Локальные журналы текущего прогона: `/private/tmp/structuraguard-m12-audit-new.log`,
`/private/tmp/structuraguard-m12-audit-narrow.log`,
`/private/tmp/structuraguard-m12-audit-gates.log`,
`/private/tmp/structuraguard-m12-audit-final-static-docs.log` и
`/private/tmp/structuraguard-m12-audit-docs-final.log` (финальная версия документации).

## Review

Применены `structuraguard-review` и `structuraguard-security`. Найденный и закрытый
Medium finding — ошибка report contract на повторном claimed value ID; исправление
и regression описаны выше. Сохраняются unique coordinates, запрет verified duplicates,
точные counters, rejected decision и безопасная summary. Производственный diff аудита
ограничен `contracts/provenance.py`; новые dependencies, I/O и execution hooks не
добавлены. Открытых существенных findings в этом diff нет. Незакрытые критерии общего
milestone перечислены в матрице и далее; зелёный test suite не отменяет эти пробелы.

## Непроверенные сценарии и причины

- Сквозной вызов от batch normalization через projection/type/schema/DB/rules до
  report, automatic MappingPlan/catalog revalidation, `blocked_by`/`not_applicable`:
  соответствующий coordinator ещё не реализован. Unit-тест builder проверяет
  только trusted composition, не доказывает выполнение каждого уровня.
- Derived normalized schema 1.3 и её consumers M8–M11 отсутствуют. Проверяются
  существующие legacy версии и normalization sidecar; migration acceptance не закрыта.
- Произвольно согласованная подмена source bytes и всех anchors неотличима от нового
  доверенного источника. Tests задают trusted physical snapshot/context и атакуют
  производные недоверенные DTO; криптографическая авторизация не заявлена.
- Для partial/expression/collation/operator-class/deferred key semantics проверен
  безопасный unverified/no-read исход на catalog fixtures. Эквивалентность произвольной
  DB семантике не заявлена; её evaluator отсутствует. Prechecks не заменяют constraints
  при финальной записи и не доказывают отсутствие последующей DB race.
- Пользовательские normalizers — доверенный pure code владельца, а не sandbox для
  враждебного Python. Проверены protocol и I/O поведение built-ins; изоляция произвольного
  callback процессом/ОС не реализована.
- Прогон выполнен на текущей macOS/Python 3.12. Отдельная матрица других Python/ОС
  и production-scale нагрузка не запускались. Resource limits проверены на малых
  детерминированных adversarial inputs; абсолютная гарантия CPU/memory не заявлена.
- Реальные платные LLM API и рабочие БД не вызывались по условиям задачи.
  Integration использует локальные SQLite/PostgreSQL fixtures.
