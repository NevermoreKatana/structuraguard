# Приёмка M11 — MappingPlan Validator

Дата реализации и повторного тестового аудита: 2026-09-12. Статус: реализован в scope
[плана M11](M11_mapping_plan_validator.md).
Все критерии подтверждены; итоговые команды и ограничения подготовки к ручному
commit/PR собраны в [checklist плана](M11_mapping_plan_validator.md#m11-commit-readiness).

## Результат

Добавлен async port и `mapping.MappingPlanValidator` с typed/JSON входами,
полным отчётом применимых независимых проверок и стабильными codes/locations.
Проверяются existence, schema/table/column policy, system/DDL/SQL veto,
writability, required targets, types, identity/upsert, FK/relation strategies,
confidence, hashes и schema drift. ACCEPTED создаёт wrapper с согласованным
versioned evidence; REJECTED/NEEDS_REVIEW wrapper не создают.

MappingPlan 1.1.0 содержит закрытые identity/relation descriptors. Legacy
serialization/hash и строгий constructor прежнего request сохранены. M7 и M11
используют общий алгоритм dependency order; цикл вне выбранных записей не блокирует
план. Добавлены экспорты и явные manifests дистрибутива без расширения зависимостей.

Публичный API и ограничения: [документация](../mapping-plan-validation.md).
Архитектурное решение: [ADR 0021](../adr/0021-mapping-plan-validation.md).
Последующий [security review текущего diff](M11_security_review.md) содержит
дополнительные findings, исправления и актуальные результаты gates после них.

## Исправления финального review — 2026-09-12 {#m11-review-fixes}

Исправлены только четыре Medium из финального review без изменения публичных
сигнатур, wire DTO, error codes и утверждённого scope. Production изменения
ограничены `_validation_types.py`, `_validation_identity.py`,
`_validation_relations.py` и `_validation_input.py`.

| Finding | Исправление и наблюдаемый regression test |
|---|---|
| `1e100 → real` ошибочно выдавал ACCEPTED | M11 проверяет extrema NumberScalar относительно конечного binary32 диапазона PostgreSQL и возвращает `MAPPING_NUMERIC_OVERFLOW`. `unit/mapping/test_m11_review_regressions.py::test_float_native_range_controls_acceptance` проверяет знаки, точные границы, ноль и double precision/float8 controls. |
| Unique index `(amount, amount)` вызывал исключение вместо отчёта | Индекс с повтором компоненты не становится кандидатом identity. `test_repeated_unique_index_component_does_not_interrupt_report` одновременно получает `MAPPING_IDENTITY_REQUIRED` и низкий confidence; обычный unique index проходит. |
| `integer → bigint` FK ошибочно отклонялся | Совместимость известных целочисленных PostgreSQL типов проверяется отдельно от полного равенства DTO. `test_integer_fk_widths_are_compatible_without_bypassing_value_range` проверяет обе стороны FK, алиасы, smallint, сохранение overflow veto и отказ для неизвестного native типа. |
| Неполный DTO вызывал AttributeError без SDK code | Preflight преобразует отсутствующий атрибут в `MAPPING_PLAN_INVALID`. `security/mapping/test_m11_incomplete_dto.py::test_missing_dto_fields_raise_safe_sdk_error` покрывает plan, manifest, catalog, profile, policy и вложенную column. |

Пути тестов в таблице относительно `packages/structuraguard/tests/`.
`integration/database/test_postgresql_m11_review.py` подтверждает первые три
исправления на реальном reflection PostgreSQL 16/18. Для `real` дополнительно
проверяется SQLSTATE `22003` от test-admin записи; `double precision` сохраняет
значение. Validator при этом не получает engine/connection. До исправлений новый
PostgreSQL suite дал **6 failed, 2 passed**, после — все восемь cases проходят.

Новые unit/security tests запускались до исправлений; итоговый набор —
**23 passed**. Затем mapping suite — **462 passed**, новые и существующие M11
PostgreSQL tests — **20 passed**, Ruff — **387 файлов**, mypy — **383 файла**,
ошибок нет. Проверки не ослаблялись; платные LLM API не вызывались.

Полный прогон исправлений:

| Команда | Фактический результат |
|---|---|
| `make test` | **3290 passed**, **104 deselected**, 5 существующих SWIG warnings |
| `make test-integration` | **26 passed**, **3368 deselected**, 5 существующих SWIG warnings |
| `make test-security` | **782 passed** |
| `make test-database` | **104 passed**, PostgreSQL 16/18, SAWarning как error |
| `make test-build` | Offline wheel/sdist verification — OK |
| `make docs` / `git diff --check` | Strict build и whitespace — OK |

Полная команда: `PYTEST_ADDOPTS='-q --tb=short' make test test-integration test-security test-database test-build`.
Узкие команды выполнялись через `UV_CACHE_DIR=/private/tmp/structuraguard-m11-uv-cache uv run --locked --no-sync`:

```bash
pytest -q --tb=short packages/structuraguard/tests/unit/mapping/test_m11_review_regressions.py packages/structuraguard/tests/security/mapping/test_m11_incomplete_dto.py
pytest -q --tb=short -W error::sqlalchemy.exc.SAWarning -m database_integration packages/structuraguard/tests/integration/database/test_postgresql_m11_review.py packages/structuraguard/tests/integration/database/test_postgresql_mapping_validation.py
```

После полного прогона список совместимых FK-типов дополнительно ограничен шестью
известными именами, для которых уже проверяются integer ranges. Повторно пройдены
**23 regression cases**, lint/typecheck и проверка дистрибутива.

Повторный correctness/security review исправлений не выявил существенных findings.
Проверены сохранение отказов для unknown типов/overflow, отсутствие новых I/O,
локальность accumulator и отсутствие перехвата cancellation. Неподдержанный
индекс не нормализуется молча в другой ключ. Расширение FK compatibility ограничено
известными PostgreSQL integer типами; другие неодинаковые типы по-прежнему
отклоняются. Нагрузочный benchmark на верхних лимитах отдельно не проводился;
record/load gates и TOCTOU остаются за scope M11.

## Матрица повторного аудита «критерий → тест» {#m11-acceptance-matrix}

Проверены все семь критериев из раздела «Критерии приёмки» плана.
Пути ниже относительно `packages/structuraguard/tests/`. Перечислены наблюдаемые
assertions; существующие тесты сохранены, добавлено недостающее покрытие.

| Критерий | Наблюдаемый тест и проверяемый результат |
|---|---|
| 1. Корректные insert/upsert, SQLite и PostgreSQL catalog-v1 | `integration/test_mapping_plan_validation.py::test_sqlite_fresh_inspection_drift_and_no_io` — обе операции; `integration/database/test_postgresql_mapping_validation.py::test_pg_composite_pk_and_real_drift` — обе операции на PostgreSQL 16/18, composite PK. |
| 1. Types и недоказанные предпосылки | `unit/mapping/test_m11_types.py::test_type_evidence_controls_decision_and_wrapper` — bool/int, Decimal/float, numeric precision/scale, length, timezone, enum/domain/array, границы; `test_missing_representation_evidence_needs_review_even_at_full_confidence` — NEEDS_REVIEW без wrapper. |
| 1. Identity/upsert | `unit/mapping/test_m11_rules.py::test_upsert_requires_confirmed_key_and_does_not_fallback_from_explicit_key`; `test_m11_relations_identity.py::test_unsafe_unique_evidence_cannot_authorize_upsert` — partial/expression/invalid/deferred/natural; `test_m11_composite_relations.py::test_unproven_identity_never_uses_profile_uniqueness_as_constraint` — nullable, ambiguity, incomplete key, wrapper отсутствует. |
| 1. FK и load order | `unit/mapping/test_m11_composite_relations.py::test_composite_fk_needs_exact_pairs_key_and_supported_strategy` — разрешённые source/lookup и отказ при перестановке/пропуске пары, отсутствии unique/lookup, generated/deferred/two_phase; `test_m11_relations_identity.py::test_complete_mapped_parent_relation_and_wrong_pair` — parent-before-child; `test_unrelated_catalog_cycle_does_not_block_selected_plan`; `test_m11_rules.py::test_fk_and_cycle_require_explicit_supported_resolution` — выбранный self-cycle блокирует. |
| 2. Все независимые issues | `unit/mapping/test_m11_acceptance.py::test_five_independent_failures_keep_exact_codes_and_locations` — точный ordered список source/deny/type/key/confidence; `test_missing_table_suppresses_dependent_type_and_key_issues` — два TABLE_NOT_FOUND в исходных locations без выдуманных зависимых ошибок. |
| 2. Повреждённый intake и исходные JSON locations | `security/mapping/test_m11_acceptance_security.py::test_malformed_descriptor_does_not_shift_later_issue_location` — malformed descriptor 0 не сдвигает независимый issue descriptor 1; `test_m11_validation_security.py::test_sql_forbidden_operation_and_unrelated_semantic_issues_are_collected` — SQL/DDL и low confidence в одном отчёте. |
| 3. Scope, deny precedence, writability | `unit/mapping/test_m11_rules.py::test_policy_veto`, `test_column_deny_wins_over_allow`, `test_column_metadata_veto`; `test_m11_acceptance.py::test_non_table_or_readonly_target_cannot_get_wrapper`; `test_m11_relations_identity.py::test_lookup_allow_does_not_override_parent_column_deny`. |
| 3. Реальная generated/identity metadata | `integration/database/test_postgresql_mapping_validation.py::test_pg_reflected_identity_generation_and_composite_fk` — BY DEFAULT разрешён, пропущенный ALWAYS даёт DB-generated identity, запись в ALWAYS/generated блокируется, partial composite FK unresolved; PostgreSQL 16/18. |
| 4. Drift и независимые bindings | Обе dialect integration проверки после настоящего ALTER + fresh inspection; `unit/mapping/test_m11_validation.py::test_detects_schema_drift_and_stale_catalog_claim`; `test_m11_acceptance.py::test_each_plan_lineage_binding_is_checked`, `test_target_and_inspection_policy_bindings_are_not_schema_hash`, `test_validation_policy_changes_evidence_even_when_both_policies_accept`, `test_rehashed_profile_with_wrong_lineage_cannot_supply_type_evidence`, `test_forged_snapshots_are_revalidated`. |
| 5. Детерминизм | `contract/mapping/test_m11_hashseed.py::test_hashseed_does_not_change_full_m11_result`; `unit/mapping/test_m11_validation.py::test_fingerprint_is_independent_of_validation_time`; `property/mapping/test_m11_validation_properties.py::test_catalog_and_policy_permutations_preserve_full_evidence`, `test_independent_check_completion_order_does_not_change_report` — равны ordered issues, evidence/hash при перестановках и порядке независимых проверок. |
| 5. Монотонность и полнота | В том же property-файле `test_all_independent_vetoes_are_present` и `test_scope_threshold_and_writability_restrictions_never_remove_a_veto` — независимый oracle codes; сужение allow, рост threshold и снятие writability не снимают veto даже при прежнем schema hash. |
| 6. Чистота, SQL/DDL/system veto | `security/mapping/test_m11_acceptance_security.py::test_validation_uses_no_io_or_sql_compilation_and_preserves_all_inputs` — typed/JSON входы при запрещённых file/socket/HTTP/DB/SQL compiler calls, пять snapshots не изменились; `test_closed_operations_reject_dml_and_ddl`; существующие `test_unlisted_malicious_identifiers_remain_inert`, `test_system_metadata_is_denied_but_exact_quoted_identifiers_are_inert`; SQLite DB bytes неизменны. |
| 6. Конечные budgets и cancellation | `security/mapping/test_m11_acceptance_security.py::test_catalog_report_and_work_limits_cannot_return_acceptance`, `test_typed_preflight_runs_before_model_dump`, `test_json_byte_limit_runs_before_decode`; существующие `test_resource_limit_does_not_return_partial_acceptance`, `test_constraint_work_is_charged_even_for_repeated_equivalent_keys`, `test_nonfinite_model_copy_and_cancellation_do_not_produce_evidence` — typed отказ без partial acceptance. |
| 7. Legacy DTO, snapshots и закрытый port | `unit/contracts/test_m11_validation_contracts.py::test_legacy_wire_omits_m11_fields_and_preserves_hash`, `test_result_and_wrapper_must_share_m11_evidence`; `unit/mapping/test_m11_acceptance.py::test_legacy_catalog_is_readable_but_cannot_authorize_validation` — DATABASE_METADATA_UNSUPPORTED; `contract/mapping/test_m11_validation_port.py::test_validator_port_and_json_produce_same_evidence`. |

## Дефекты, выявленные новыми тестами

1. **Medium:** intake удалял malformed identity/relation descriptor и затем заново
   нумеровал оставшиеся. Два regression-теста сначала падали: issue элемента 1
   получал index 0. Теперь `_validation_input.py`, `_validation_relations.py` и
   `validation.py` сохраняют исходный индекс. Acceptance/security veto не ослаблены.
2. **High:** известная по M8 extrema потеря numeric scale не блокировала wrapper:
   `1.234 → numeric(4,2)` и `12 → numeric(4,-1)` принимались. Оба regression-теста
   сначала падали. `_validation_types.py` возвращает существующий
   `MAPPING_TRANSFORMATION_REQUIRED`, если exact extrema требует округления.
   Точные значения и завершающие нули проходят; Decimal context не используется
   для преобразований. Поведение M9 и record/load engine не менялось.

Новые fixtures исправлены до приёмки: у view все колонки non-writable, а
`ColumnCatalog.type_name` содержит canonical type. Ruff/mypy обнаружили порядок
imports и типы тестовых переменных; исправления внесены без ignores и ослабления gates.

## Выполненные проверки

| Команда | Свежий результат |
|---|---|
| Узкий mapping suite, команда ниже | 421 passed, 8.49 s |
| Узкий PostgreSQL M11 suite, команда ниже | 12 passed, 4.84 s; PostgreSQL 16/18, без skips |
| `make lint` | Ruff format/check пройдены, 382 Python files |
| `make typecheck` | mypy: 378 source files, ошибок нет |
| `make test` | 3240 passed, 96 deselected, 103.53 s; PostgreSQL вынесен в отдельный gate |
| `make test-integration` | 26 passed, 3310 deselected, 5.57 s |
| `make test-security` | 758 passed, 26.82 s |
| `make test-database` | 96 passed, 14.60 s; PostgreSQL 16/18, SQLAlchemy warnings как errors |
| `make test-build` | Wheel/sdist, offline rebuild, isolated install/import и examples smoke пройдены |
| `make docs` | Strict MkDocs build пройден |
| Diff | Проверены whitespace и новые файлы; выполнены correctness/security review |

Python: 3.12.9. Полные tests запускались с `PYTEST_ADDOPTS='-q --tb=short'`;
selectors и обязательные проверки не отключались. Sandbox блокировал системный
memory watchdog, loopback и Docker socket, поэтому итоговый прогон выполнен
с разрешённым доступом к этим ресурсам и штатному UV cache. Старые провалы среды
не считались результатом приёмки. Остались пять известных deprecation warnings
PyMuPDF/SWIG в наборах документных тестов.

Команды узких прогонов из корня репозитория:

```bash
UV_CACHE_DIR=/private/tmp/structuraguard-m11-uv-cache uv run --locked --no-sync pytest -q --tb=short packages/structuraguard/tests/unit/mapping packages/structuraguard/tests/contract/mapping packages/structuraguard/tests/property/mapping packages/structuraguard/tests/security/mapping packages/structuraguard/tests/unit/contracts/test_m11_validation_contracts.py packages/structuraguard/tests/integration/test_mapping_plan_validation.py packages/structuraguard/tests/smoke/test_mapping_boundaries.py
uv run --locked --no-sync pytest -q --tb=short -W error::sqlalchemy.exc.SAWarning -m database_integration packages/structuraguard/tests/integration/database/test_postgresql_mapping_validation.py
```

Полные gates:

```bash
UV_CACHE_DIR=/private/tmp/structuraguard-m11-uv-cache make lint typecheck
PYTEST_ADDOPTS='-q --tb=short' make test test-integration test-security test-database test-build
UV_CACHE_DIR=/private/tmp/structuraguard-m11-uv-cache make docs
git diff --check
```

Новые тесты запускались отдельно до общего suite. Воспроизведение смещённых
locations: `pytest -q --tb=short` с файлами `test_m11_acceptance_security.py`,
`test_m11_acceptance.py`, `test_mapping_plan_validation.py` — **2 failed, 40 passed**;
после исправления первые два файла — **40 passed**. Воспроизведение numeric scale:
`pytest -q --tb=short packages/structuraguard/tests/unit/mapping/test_m11_types.py -k scale`
— **2 failed, 3 passed, 15 deselected**; после исправления весь файл — **20 passed**.
Команды pytest выполнялись через `uv run --locked --no-sync`.

В повторном аудите добавлено 88 собранных тестовых случаев: 78 в обычный suite,
10 в PostgreSQL suite. Production dependencies, существующие assertions и настройки
lint/typecheck/security gates не ослаблялись. Платные и реальные внешние LLM API
не вызывались.

Изменённые этим аудитом файлы:

- `packages/structuraguard/src/structuraguard/mapping/`: `_validation_input.py`,
  `_validation_relations.py`, `_validation_types.py`, `validation.py` — два исправления.
- `packages/structuraguard/tests/unit/mapping/`: новые `test_m11_acceptance.py`,
  `test_m11_types.py`, `test_m11_composite_relations.py`; дополнен
  `test_m11_relations_identity.py` проверкой load order.
- `packages/structuraguard/tests/security/mapping/test_m11_acceptance_security.py` — новый;
  `property/mapping/test_m11_validation_properties.py` — новые invariants;
  `fakes/mapping_validation.py` — входные scalar fixtures.
- `packages/structuraguard/tests/integration/test_mapping_plan_validation.py` и
  `integration/database/test_postgresql_mapping_validation.py` — обе операции и
  настоящая identity/generated/FK metadata.
- `docs/mapping-plan-validation.md`, `docs/plans/M11_acceptance.md` — поведение и аудит.

## Проверка требований и безопасности

- Тесты одного вызова подтверждают одновременные независимые policy/reference/
  confidence failures; повтор code в разных locations сохраняется.
- Проверены malicious/unlisted identifiers, Unicode confusables, запрещённые
  операции/SQL/DDL, реальные system names, точные инертные quoted identifiers,
  malformed JSON, duplicate JSON keys, non-finite numbers и invalid Unicode.
- Проверены source PK permission, неподтверждённый explicit/natural key,
  nullable/partial/expression/invalid/deferred arbiters, DB-generated identity,
  unresolved FK, mapped parent, lookup deny и циклы.
- Fresh inspection после изменения настоящей SQLite/PostgreSQL schema обнаруживает
  drift. Изменение writable при прежнем DB hash отдельно блокирует план.
- SQLite test запрещает вызовы connection/file API во время validate и сравнивает
  bytes БД до/после. Валидатор не интерполирует identifiers и не исполняет SQL.
- Hypothesis проверяет комбинации независимых veto. Отдельные процессы с разными
  `PYTHONHASHSEED` дают одинаковые ordered issues и evidence. Время исключено из hash.
- Проверены cancellation, input/depth/mapping/work budgets, в том числе учёт
  повторяющихся unique indexes. Превышение limits не даёт partial acceptance.

При review устранены пропуск domain NOT NULL при required coverage, возможность
lookup allow обойти column deny и недостаточный учёт работы по constraints/FK.
Добавлены соответствующие regression tests. Существенных незакрытых findings
в реализованном scope не осталось.

## Оставшиеся ограничения

Все доступные gates выполнены; пропущенных PostgreSQL cases нет. Отдельно не
проверяется реальное исполнение record/load pipeline: FK lookup по строкам,
enum/CHECK/UNIQUE на полном потоке, live grants, TOCTOU, rollback/idempotency,
generated-key propagation и deferred/two-phase cycles. Эти компоненты находятся
за scope M11; запрет неподдержанных plan strategies проверен.
Enum/domain/array representation проверена unit-тестами M11 и metadata-тестами M7;
фактическая запись значений этих типов не является проверкой чистого валидатора.

Hash/evidence не удостоверяют автора snapshot и не дают права записи. Перед load
нужны свежий inspection, проверка grants и policy, защита TOCTOU, record validation,
staging, транзакция и rollback. Значения FK, enum/CHECK/UNIQUE, диапазоны и provenance
проверяются будущим record/load engine. Unknown representations не принимаются
автоматически; casts, сложные arrays/JSON, generated-key propagation и выполнение
циклических стратегий не реализованы. SDK facade и loader остаются вне scope M11.
