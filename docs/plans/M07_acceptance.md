# M07 — проверка критериев приёмки

Дата: 2026-09-10. Основание: [план M07](M07_database_inspector.md), K1–K7;
выборочные разделы ТЗ 9.3, 9.5, 9.6 и M7 через `SPEC_INDEX`.
Итог: K1–K7 подтверждены перечисленными tests в проверенной matrix; выявленный
дефект устранён. Дополнительные дефекты финального review и regression evidence
описаны [ниже](#m07-final-review-fixes). Проверяется текущий Database Inspector A/B/C. Loader, mapping и SDK facade
не расширяют scope этой проверки.

Ниже пути tests указаны относительно `packages/structuraguard/tests/`.
Тесты, существовавшие до этой проверки, сохранены. Первичный аудит дополнил
общий `contract_suites/database.py` отрицательным контрактом; regressions
финального review перечислены отдельно. Lint/typecheck/security checks не ослаблялись.

## Матрица «критерий → наблюдаемый тест»

| Критерий | Наблюдаемое поведение | Тесты |
| --- | --- | --- |
| **K1: target/policy/scope до I/O** | Чужой target, policy, расширенная schema и unchecked `read_only=False` отклоняются до DB operation в обеих реализациях. | Новый `security/database/test_m07_full_inspection.py::test_full_inspect_rejects_untrusted_request_before_io`, общий `contract_suites/database.py::assert_request_rejection`; существующие `test_inspection_policy.py::test_policy_rejection_happens_before_open`, `test_postgresql_policy.py::test_rejects_scope_before_opening_connection`. |
| **K1: запрещённые объекты/FK targets** | SQL trace не содержит reflection запрещённых объектов; внешний FK не расширяет scope и не исчезает из успешного каталога. | `security/database/test_inspection_policy.py::test_denylist_applies_before_any_object_reflection`; `integration/database/test_postgresql_security.py::{test_scope_precedes_reflection_and_role_cannot_read_rows,test_foreign_key_does_not_expand_scope,test_request_schema_restriction_also_bounds_type_reflection}`; `unit/database/test_sqlite_inspection.py::test_foreign_key_outside_scope_is_not_silently_removed`. |
| **K2: отдельное read-only соединение, без DDL/DML** | SQLite-файл остаётся побайтово прежним; попытки изменения через inspection connection запрещены. PostgreSQL read-only действует даже для owner DSN, после операции нет inspection backend. | `security/database/test_sqlite_read_only.py::{test_inspection_preserves_file_bytes_and_user_data,test_database_file_is_opened_read_only_even_before_query_only,test_metadata_connection_cannot_read_rows_or_enable_mutation}`; `integration/database/test_postgresql_security.py::test_read_only_is_enforced_even_with_owner_credentials`. |
| **K2: metadata-only, expressions остаются данными** | SQL trace содержит catalog SELECT; inspector role не имеет SELECT на rows. Чтение views, которые падают при выполнении выражения, успешно отражает только metadata. | `integration/database/test_postgresql_security.py::test_scope_precedes_reflection_and_role_cannot_read_rows`; `test_postgresql_inspection.py::test_view_expression_is_not_executed`; новый `unit/database/test_m07_sqlite_acceptance.py::test_view_expression_that_would_fail_is_not_executed`. M7 не вызывает LLM; paid API в этой проверке не используются. |
| **K3: полнота разрешённых metadata** | Реальные SQLite/PostgreSQL fixtures проверяют native/canonical types, ordinal, nullable/default, PK/FK/unique/checks/indexes, generated/identity и writable flags, views, comments. | `unit/database/test_normalization.py`; `test_sqlite_inspection.py::test_sqlite_catalog_contains_real_metadata`; `test_sqlite_edge_cases.py`; `integration/database/test_postgresql_inspection.py::{test_real_constraints_types_comments_and_stable_order,test_generation_and_enforcement_follow_server_metadata,test_foreign_key_preserves_partial_delete_action}`; новый `test_m07_acceptance.py::test_schema_and_constraint_comments_are_published`. |
| **K3: missing/unsupported не маскируются пустым успехом** | Отсутствующий schema/table возвращает `DATABASE_OBJECT_NOT_FOUND`; EXCLUDE constraint — `DATABASE_METADATA_UNSUPPORTED`. SQLite не создаёт отсутствующий файл, nullable PK отклоняется явно. | Новые `integration/database/test_m07_acceptance.py::{test_missing_object_is_typed_not_an_empty_catalog,test_exclusion_constraint_is_explicitly_unsupported}`, `unit/database/test_m07_sqlite_acceptance.py::test_missing_table_is_not_an_empty_success`; существующие SQLite missing-file и nullable-PK tests. |
| **K4: порядок reflection и stable IDs** | Перестановка columns/constraints/indexes rows настоящего PostgreSQL сохраняет полный каталог и hash; property cases переставляют collections и parallel FK. Golden фиксирует bytes/hash; SQLite duplicate unnamed FK и autoindex order проверены. | Новый `integration/database/test_m07_acceptance.py::test_unordered_reflection_rows_preserve_catalog_ids_and_hash`; новый `unit/database/test_m07_fingerprint_acceptance.py::test_named_constraints_and_parallel_fk_reflection_permutations`; существующие `test_fingerprint.py::{test_reflection_permutations_preserve_sha256,test_catalog_v1_golden_bytes_and_hash}`, `test_inspection_catalog.py::test_implicit_index_order_and_duplicate_unnamed_fks`. |
| **K4: значимые изменения меняют hash** | Mutation matrix меняет structural fields и связанные состояния; SQL expressions не нормализуются по бизнес-смыслу. Реальный schema/comment drift даёт `DATABASE_SCHEMA_DRIFT`. | Новые `unit/database/test_m07_fingerprint_acceptance.py::{test_remaining_projection_fields_change_hash,test_coupled_schema_fields_change_hash}`; существующие `test_fingerprint.py::{test_schema_mutations_change_fingerprint,test_nested_type_semantics_affect_hash}`, `test_inspection_catalog.py::test_full_catalog_contract_and_data_vs_schema_drift`, `integration/database/test_postgresql_catalog.py::test_data_only_changes_preserve_hash_and_comment_change_is_drift`. |
| **K4: volatile metadata исключены** | Rows, target/policy, opaque IDs, producer/SDK version, display name и входное поле hash не меняют вычисленный fingerprint. Другой PostgreSQL principal/DSN даёт тот же schema hash и graph. | Новые `unit/database/test_m07_fingerprint_acceptance.py::test_full_catalog_producer_and_display_metadata_do_not_change_hash`, `integration/database/test_m07_acceptance.py::test_connection_identity_does_not_change_schema_fingerprint`; существующие data-only drift tests и `test_fingerprint.py::{test_volatile_fields_and_opaque_ids_are_excluded,test_column_ids_are_resolved_to_names,test_projection_does_not_publish_runtime_identity}`. Timestamps/row statistics отсутствуют в catalog DTO и явной projection. |
| **K5: DAG, isolated nodes, composite/multiple schemas** | Каждый parent precedes child, все узлы учтены; parallel FK не портят indegree, composite pairs сохраняют порядок. | `unit/database/test_dependency_graph.py::{test_arbitrary_dag_has_complete_parent_first_order,test_diamond_parallel_links_and_isolated_nodes,test_composite_fk_and_namespaces,test_views_remain_nodes_but_are_not_load_targets}`; реальный `integration/database/test_postgresql_catalog.py::test_full_port_catalog_and_composite_fk`. |
| **K5: cycles/self-reference и diagnostics** | SCC сравниваются с независимым transitive-closure oracle для произвольных графов; перестановки сохраняют diagnostics. Цикл из 1500 узлов не требует рекурсии, cyclic graph не публикует частичный load order. | Новый `unit/database/test_m07_graph_acceptance.py::test_arbitrary_graph_scc_matches_transitive_closure`; существующие ring/multiple-SCC/deep-cycle tests в `test_dependency_graph.py`, `security/database/test_dependency_evidence.py`. |
| **K5: достаточность join evidence** | Unique key даёт hint только с non-null endpoints; overlap/generated/unvalidated/unenforced проверяются независимо. Payload/surrogate/same-parent/name-only не дают hint. | Новые `unit/database/test_m07_graph_acceptance.py::{test_join_unique_key_requires_nonnullable_endpoints,test_join_rejects_each_insufficient_evidence_independently}`; существующие `test_dependency_graph.py::{test_join_hint_requires_full_structural_evidence,test_insufficient_join_evidence_is_not_guessed}`. |
| **K6: timeout/cancellation/budgets/cleanup** | Resource/statement/lock/total limits не публикуют partial catalog; cancellation ждёт остановки worker, cleanup failure блокирует reuse; общий deadline и bytes учитывают сборку graph. | `security/database/test_sqlite_read_only.py::test_cancel_or_deadline_joins_worker_before_returning`; `test_inspection_policy.py::{test_metadata_limits_fail_without_partial_snapshot,test_all_metadata_budgets_are_enforced}`; `integration/database/test_postgresql_security.py::{test_limits_fail_without_partial_catalog,test_server_statement_timeout_closes_connection,test_cancellation_stops_active_query_and_releases_connection,test_total_timeout_closes_connection,test_lock_timeout_during_reflection_closes_connection,test_cleanup_deadline_terminates_driver_and_blocks_reuse}`; `unit/database/test_inspection_catalog.py` cancellation/deadline/final-byte-budget tests. |
| **K6: coherent snapshot при DDL** | DDL другого admin connection фиксируется через event barrier после начала inspection snapshot; результат целиком before/after либо typed refusal, никогда mixed hash. Connections закрываются. | Новый `integration/database/test_m07_acceptance.py::test_concurrent_ddl_never_publishes_a_mixed_snapshot`. |
| **K6: permission и отсутствие secrets** | USAGE/authentication error типизирован; exception, context, logs и repr не содержат DSN/password; DEBUG SQLAlchemy не публикует comments. | `integration/database/test_postgresql_security.py::{test_permission_error_is_typed_and_credentials_are_redacted,test_debug_logging_does_not_publish_metadata}`; `security/database/test_inspection_policy.py::test_debug_logging_and_errors_never_expose_metadata`. Audit storage в M7 не подключён. |
| **K7: versioning/legacy/общий contract** | Legacy wire сохраняется, extended DTO имеет 1.1.0/catalog-v1; unsupported version не потребляется через unchecked copy. Общий positive/negative contract применяется к SQLite/PostgreSQL; `execute` отказывает до I/O/batches. | Новые `unit/database/test_m07_catalog_contract.py::{test_extended_catalog_rejects_wrong_wire_version,test_domain_consumer_rechecks_catalog_version}`; `unit/contracts/test_database_catalog.py`, M2 suites `unit/contracts/`, `unit/ports/`, `smoke/test_m02_layer_boundaries.py`; `contract_suites/database.py`, SQLite/PostgreSQL full-port tests, `security/database/test_m07_full_inspection.py`. |

## Исправления финального review {#m07-final-review-fixes}

Два Medium finding исправлены в текущем scope M7. PostgreSQL domain CHECK раньше
терял name/comment/validated, поэтому изменение этих metadata не меняло hash.
Дополнительное поле `DatabaseType.domain_constraints` использует существующий
`ConstraintInspectionMetadata`, проверяется на согласованность с `domain_checks`
и входит в canonical projection. Старое поле и legacy JSON сохранены.
Новые comments ограничиваются sentinel на сервере, общими text/byte budgets
и validators; ошибки не содержат raw metadata или исходный exception context.

Второй дефект: PostgreSQL attnum сохраняет пропуски после DROP COLUMN; публикация
этого номера давала разные hashes для одинаковой видимой схемы. Adapter теперь
нумерует видимые columns последовательно по attnum. Разрешение composite PK/FK
использует исходные физические номера, фактическая перестановка columns меняет hash.

| Критерий | Regression test | Наблюдаемый результат |
| --- | --- | --- |
| K3/K4: domain CHECK name/comment/validity | `integration/database/test_m07_review_regressions.py::test_domain_check_metadata_changes_fingerprint` | Rename/comment/VALIDATE меняют hash и дают `DATABASE_SCHEMA_DRIFT`; 6 cases на PG 16/18. |
| K4/K5: история DROP COLUMN | `integration/database/test_m07_review_regressions.py::test_dropped_column_history_preserves_fingerprint_and_composite_keys` | После пересоздания совпадают весь catalog/hash/graph; порядок составных keys сохранён; перестановка видимых колонок меняет hash; 2 cases. |
| K6: новая metadata boundary | `integration/database/test_m07_review_regressions.py::test_domain_check_metadata_remains_bounded_and_redacted` | Oversized/secret comment и strip-changing constraint name дают typed отказ, без raw errors/logs и открытого inspection connection; 6 cases. |
| K4/K7: nested metadata и совместимость | `unit/database/test_domain_constraints.py` | Round-trip, optional serialization, consistency, nested domain/array mutations и property permutations; 17 cases. Прежние fingerprint golden tests сохранены. |

До исправления новый PostgreSQL regression file дал **8 failed** на ожидаемых
сравнениях hashes. После исправления и добавления security cases — **14 passed**.
Узкие `unit/database`, `unit/contracts/test_database_catalog.py`, `security/database`:
**216 passed**. `make lint typecheck`: Ruff **267 файлов**, mypy **265**, ошибок нет.
Полные проверки исправленного состояния фиксируются в
[PROJECT_STATE](../codex/PROJECT_STATE.md#m07-review-fix-checks).

## Выявленный дефект и изменение

Regression `test_domain_consumer_rechecks_catalog_version` сначала дал **3 failed**:
`canonical_database_catalog`, `database_fingerprint` и `build_dependency_graph`
принимали `DatabaseCatalog` с `schema_version="9.0.0"`, подменённой через
`model_copy()`. DTO construction проверял версию, но domain consumer проверял
только `fingerprint_version` и metadata capability.

Минимальное исправление: `domain/_database_catalog.py::validated_metadata`
требует `schema_version="1.1.0"` перед потреблением полного каталога. Код отказа —
`DATABASE_METADATA_UNSUPPORTED`. После исправления целевой contract file:
**5 passed**. Остальные production modules и существующие assertions не менялись.

## Команды и результаты

Новые unit/property/contract/security files запущены сначала отдельно:
fingerprint **59**, graph **7**, version contract **5**, SQLite **2**, full-inspection
security **2** passed; общий прогон новых файлов — **75 passed**.
Новые PostgreSQL tests на 16.15/18.6: исходный прогон **12 passed**, дополнительная
проверка reflection order **2 passed**.

Точные наборы локальных новых и узких tests:

```bash
uv run --locked --no-sync pytest -q \
  packages/structuraguard/tests/unit/database/test_m07_fingerprint_acceptance.py \
  packages/structuraguard/tests/unit/database/test_m07_graph_acceptance.py \
  packages/structuraguard/tests/unit/database/test_m07_catalog_contract.py \
  packages/structuraguard/tests/unit/database/test_m07_sqlite_acceptance.py \
  packages/structuraguard/tests/security/database/test_m07_full_inspection.py

uv run --locked --no-sync pytest -q \
  packages/structuraguard/tests/unit/database \
  packages/structuraguard/tests/unit/contracts/test_database_catalog.py \
  packages/structuraguard/tests/security/database \
  packages/structuraguard/tests/docs/test_m07_examples.py \
  packages/structuraguard/tests/smoke/test_m02_layer_boundaries.py \
  packages/structuraguard/tests/smoke/test_import_side_effects.py

uv run --locked --no-sync pytest -q -W error::sqlalchemy.exc.SAWarning \
  -m database_integration \
  packages/structuraguard/tests/integration/database/test_m07_acceptance.py
```

Последняя команда запускалась до добавления reflection-order test (**12 passed**),
затем с `-k unordered` (**2 passed**, 12 deselected). После этого весь DB suite
проверен командой `make test-database`.

| Команда | Фактический результат |
| --- | --- |
| Новые локальные tests, первый набор выше | **75 passed** |
| Узкий набор M7, второй набор выше | **201 passed** |
| `make test-database` | **64 passed**, PostgreSQL 16.15/18.6, без skips и SQLAlchemy warnings |
| `make lint` | **262 files** formatted; Ruff **All checks passed** |
| `make typecheck` | **260 source files**, ошибок нет |
| `make test` | **2439 passed**, 64 DB cases deselected |
| `make test-integration` | **18 passed**, 2485 deselected |
| `make test-security` | **469 passed** |
| `make docs` | MkDocs strict build прошёл |
| `make test-build` | wheel/sdist build, offline rebuild и installed-package probes: **distribution verification OK** |
| `uv lock --check --offline` | **Resolved 105 packages**, exit 0 |
| `git diff --check` | exit 0 |

Общие gates выполнялись одной командой
`make lint typecheck test test-integration test-security`, exit 0.
Пять прежних PyMuPDF/SWIG deprecation warnings сохраняются в `make test` и
`make test-integration`; новых warnings нет. DB warnings считаются ошибками.
На этой машине pytest/gates использовали
`UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache`; `make test-build` — основной
uv cache. Docker использовал отдельный пустой credential config
`DOCKER_CONFIG=/private/tmp/structuraguard-docker-public` и локальный socket
`DOCKER_HOST=unix:///Users/katana/.docker/run/docker.sock`.

Review/security review: единственное production-изменение — проверка версии,
доказанная сначала падающим regression test. В общем contract suite только
добавлены negative cases; прежние assertions, lint/typecheck и границы слоёв
сохранены. Крупного рефакторинга и новых зависимостей нет.

## Непроверенные сценарии и границы

- PostgreSQL 15/17: текущая Testcontainers matrix закреплена на 16.15/18.6;
  наличие runtime feature gate для 15–18 не является проверкой всех версий.
- Другие OS/Python/SQLite builds: локальная проверка выполняется на macOS,
  Python 3.12.9 и SQLite 3.49.1; отдельная CI matrix в этой задаче не запускалась.
- Все возможные concurrent DDL schedules не перебираются: проверена управляемая
  интерливинг-ситуация и существующий lock timeout. Вечная защита TOCTOU перед
  загрузкой принадлежит будущему loader.
- Uninterruptible OS/filesystem I/O и hard limits памяти PostgreSQL не эмулируются.
  Проверены программные budgets/cancellation/cleanup; процессный sandbox и server
  memory policy настраивает deployment owner.
- SQL definitions views, semantic equivalence SQL, metadata вне allowlist,
  column projection и неподдерживаемые dialect features не становятся покрытыми
  fingerprint. Это ограничения текущего catalog DTO/inspection scope.
- Mapping, writer/staging/upsert, разрешение циклов при записи, SDK facade,
  audit sink и LLM routing не входят в M7. Реальные платные LLM API не использовались.
