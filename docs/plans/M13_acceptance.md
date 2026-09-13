# M13 — аудит критериев приёмки

Дата: 2026-09-13. Основание — семь критериев и integration matrix
[плана M13](M13_staging_loader.md). **Milestone принят частично:** проверенные
PostgreSQL staging/loader primitives не закрывают требования к полному ingest
coordinator. Исходные критерии не изменены ради соответствия реализации.

В этом аудите добавлены 48 недостающих pytest cases, из них 42 на PostgreSQL 16/18,
и усилен существующий import probe. Дефектов runtime, непосредственно
воспроизведённых новыми тестами, не обнаружено; production-код не изменён.
Существующие M13 A–D изменения рабочего дерева сохраняются отдельно от этого аудита.
Реальные LLM API не использовались.

Последующий [security review текущего diff](M13_security_review.md) выявил и
исправил четыре Medium проблемы и добавил 18 PostgreSQL regressions. Его результаты
quality gates указаны отдельно; числа этого документа относятся к исходному аудиту.

## Критерий → наблюдаемый тест

Все пути ниже относительно `packages/structuraguard/tests/`. Обозначения:

- **S** — `integration/database/test_postgresql_staging.py`.
- **B** — `integration/database/test_postgresql_dry_run.py`.
- **C** — `integration/database/test_postgresql_loader.py`.
- **D** — `integration/database/test_postgresql_load_outcomes.py`.
- **E** — новый `integration/database/test_postgresql_load_acceptance.py`.
- **SC** — общий `contract/stores/_suite.py::StagingContract`, исполняемый на memory и PostgreSQL.

| ID | Критерий плана | Наблюдаемые тесты и эффект | Итог |
|---|---|---|---|
| AC-01 | Нет target DML до EOF, seal, lineage/projection, актуального M11 и обязательных M12 layers | `unit/loading/test_projection.py::test_incomplete_stream_rejected_before_io`; SC `test_incomplete_seal_has_no_effect`; `security/loading/test_loader_boundary.py::test_rejects_unbound_input_before_writer_io`; B `test_mapping_policy_is_revalidated_on_every_run`, `test_unverified_check_cannot_be_quarantined_into_ready`; D `test_validation_and_fk_failure_roll_back_everything` | **Частично.** EOF/seal/exact-copy projection, M11 и M12 DB constraints проверены. Loader не принимает required/completed layers полного ValidationReport и не запускает physical/business/schema validators |
| AC-02 | Воспроизводимый dry-run, would/loaded counts, нет изменений target/sequences/staging/ledger, cleanup | `unit/loading/test_execution_plan.py::test_counts_bind_data_mapping_and_projection`; B `test_real_dry_run_leaves_target_staging_and_sequence_exactly_unchanged`, `test_cancellation_closes_transaction_without_changes`, `test_server_enforces_read_only_even_if_internal_code_attempts_mutation`; D `test_success_duplicate_request_and_redacted_audit`; C `test_loader_dry_run_never_claims_staging_or_uses_writer` | **Частично по исходному API.** Snapshot сравнивает rows, xmin/ctid, sequence state и staging; ledger также неизменен. Новый DTO содержит `planned_inserts/updates/skips/quarantine` по target units, а не `would_load_records`/`loaded_records` старого LoadReport. Измерения пиков памяти нет |
| AC-03 | Только разрешённые INSERT/UPDATE, подтверждённые ключи, parent/child порядок вне зависимости от batches | C `test_composite_upsert_matches_full_confirmed_key`, `test_unconfirmed_identity_cannot_select_upsert_target`, `test_parent_in_later_source_batch_is_loaded_first`, `test_bulk_boundaries_preserve_all_rows_and_missing_null_shapes`; новый `property/loading/test_batch_properties.py::test_arbitrary_batch_boundaries_preserve_rows_without_duplicates_or_omissions` | **Покрыт в поддержанном scope.** Реальные PG PK/natural/composite keys, graph order, lookup и SQL chunk limits. Property использует независимый SQLite oracle для portable INSERT, не подменяет PG semantics |
| AC-04 | Atomic default, rollback late/SQL/audit/commit failure, явная quarantine с provenance и global veto | D `test_exception_in_middle_of_batch_rolls_back_target_and_marker`, `test_audit_permission_failure_after_dml_rolls_back_marker_and_target`, `test_fk_validation_quarantine_is_explicit_and_persistent`, `test_late_fk_failure_rolls_back_parent_group_and_continues_independent_group`, `test_quarantine_does_not_bypass_global_validation_gate`; E `test_cancellation_inside_commit_is_unknown_until_ledger_reconciliation`; SC `test_closed_execution_retains_references_and_cannot_be_reopened` | **Покрыт с оговоркой об исходе COMMIT.** До COMMIT ошибки откатывают всю transaction. Потеря ACK не доказывает rollback: UNKNOWN/reconciliation по ADR 0028/0029. Quarantine хранит hashes/codes и retained references; доступность внешних artifacts обеспечивает caller |
| AC-05 | Повтор/concurrent idempotency binding без дублей, включая generated identity и lost COMMIT ACK | D `test_success_duplicate_request_and_redacted_audit`, `test_concurrent_same_key_waits_for_commit_or_rollback`, `test_same_key_with_different_binding_is_rejected_before_staging_claim`, `test_lost_commit_response_recovers_same_key_without_writes`, `test_ledger_survives_staging_expiry_and_cleanup`; E `test_cancelled_same_key_waiter_leaves_its_staging_sealed_and_owner_commits`, `test_generated_primary_key_is_explicitly_rejected_without_consuming_sequence` | **Частично.** Durable replay и гонки с явными source keys проверены. DB-generated PK остаётся неподдержанным: проверен безопасный отказ, а не успешная загрузка/replay |
| AC-06 | Нет DDL/arbitrary SQL/out-of-scope reads/смешения principals; разные cancellation/deadline/rollback failure/unknown outcomes | S `test_bootstrap_is_explicit_repeatable_and_writer_has_no_ddl`, `test_runtime_never_creates_missing_schema`; C `test_quoted_identifiers_and_string_parameters_are_data`, `test_writer_owning_function_is_rejected_without_target_change`; E `test_table_lock_interruption_rolls_back_and_releases_all_locks`, `test_repeated_cancellation_during_rollback_waits_for_cleanup`, `test_cleanup_failure_never_claims_false_rollback_or_loses_known_commit`, `test_corrupt_ledger_receipt_cannot_authorize_replay_or_leak_payload` | **Покрыт для проверенных paths.** Отдельно проверяются CancelledError, PROCESSING_TIMEOUT, UNKNOWN, committed + cleanup warning; release реальных PG locks. Остальные fault paths перечислены ниже |
| AC-07 | M02 wire/API inspection/M11/M12 совместимы, новые DTO versioned, optional DB dependencies | `unit/contracts/test_m02_contracts.py::test_closed_wire_vocabularies_are_stable_and_complete`; `unit/ports/test_m02_protocols.py::test_protocol_coroutine_and_streaming_call_shapes_are_stable`; `unit/loading/test_execution_plan.py::test_execution_plan_wire_round_trip_rejects_unknown_version`; усиленные `smoke/test_import_side_effects.py` и `smoke/test_dependency_boundary.py`; полный pytest/mypy и distribution gate | **Покрыт существующим набором и новыми probes.** Импорт public M13 modules проходит без подключения, environment/logging/process mutations и без загрузки SQLAlchemy/asyncpg; loader API добавлен отдельно |

## Детализация A–D и integration matrix

| Область | Дополнительные наблюдаемые тесты | Граница вывода |
|---|---|---|
| A: run/batch/record metadata, replay ordinal, CAS | SC `test_stage_seal_read_and_idempotent_batch`, `test_context_mismatch_and_stale_revision`, `test_rejected_intake_does_not_change_run`; S `test_concurrent_batch_replay_and_cas`, `test_namespace_isolation_and_durable_reopen` | Один общий protocol suite на memory и PG; scoped namespace, durable reopen |
| A: retention пяти artifact kinds, TTL, cleanup | `security/stores/test_staging_boundary.py`; SC `test_cleanup_retains_tombstone_and_does_not_delete_early`, `test_unknown_never_expires_into_cleanup`, новый `test_closed_execution_retains_references_and_cannot_be_reopened` | Payload не копируется; terminal refs сохраняются до cleanup; UNKNOWN не истекает автоматически |
| A: явный bootstrap, SQL bind values, схема/роли | S `test_runtime_schema_and_principal_abuse_is_rejected`, `test_staging_values_are_bound_and_driver_logs_do_not_leak`, `test_schema_drift_fails_before_run_write`, `test_late_sql_failure_rolls_back_batch_and_records` | Runtime не создаёт схемы; batch rollback не оставляет частичных metadata |
| B: insert/update/skip/quarantine counts, актуальный read snapshot | B `test_key_only_upsert_skip`, `test_fk_missing_is_quarantine_without_target_change`, `test_upsert_classification_uses_the_same_snapshot_as_constraints` | Counts — прогноз, не резервирование ключей и не гарантия последующего commit |
| B: unknown defaults, unverified checks | B `test_unknown_default_blocks_ready_without_evaluating_sequence`, `test_unverified_check_cannot_be_quarantined_into_ready` | Непроверенная семантика блокирует ready, даже в quarantine |
| C: PK/unique/natural/composite, duplicate key, mutable update | C `test_upsert_updates_existing_and_inserts_new_without_duplicate`, `test_duplicate_key_rejects_whole_run`, `test_composite_upsert_matches_full_confirmed_key`; `unit/mapping/test_m11_relations_identity.py::test_unsafe_unique_evidence_cannot_authorize_upsert` | Unsafe key veto дополнительно проверяется на M11; не все варианты arbiter созданы в PG loader fixture |
| C: FK order, composite pairing, mapped_parent, bounded lookup | C `test_insert_parent_child_uses_graph_order_and_postgresql_staging`, `test_composite_fk_lookup_preserves_catalog_key_order`, `test_mapped_parent_checks_actual_values_and_source_link`, `test_lookup_only_parent_needs_select_without_update`, `test_fk_query_budget_vetoes_write` | Нет translation natural key → generated ID и generated-key propagation |
| C: bulk/no omissions, missing/null, Decimal/UTC, generated columns | C `test_bulk_boundaries_preserve_all_rows_and_missing_null_shapes`, `test_generated_column_is_omitted_and_requires_explicit_server_permission`; E `test_decimal_and_utc_values_survive_real_insert_and_update`; новый property test | Decimal сохраняет 18 цифр, UTC — микросекунды; generated non-key вычисляется сервером только по metadata-bound permission |
| D: schema recheck до DML и перед COMMIT, grants | C `test_execution_rechecks_schema_and_grants_after_successful_dry_run`; D `test_schema_drift_after_dml_before_commit_rolls_back_ledger` | Реальный COMMENT ON SCHEMA после DML вызывает rollback target/marker/audit |
| D: validation/FK/mid-batch/audit failure | D `test_validation_and_fk_failure_roll_back_everything`, `test_exception_in_middle_of_batch_rolls_back_target_and_marker`, `test_audit_permission_failure_after_dml_rolls_back_marker_and_target`; C `test_late_sql_duplicate_rolls_back_previous_bulk` | Сравнение состояния до/после, отсутствие marker и committed audit |
| D: cancellation/query/lock/rollback/commit/cleanup | C `test_cancellation_after_dml_rolls_back_and_closes_run`; B `test_cancellation_closes_transaction_without_changes`; новые E cancellation/cleanup tests | Реальная transaction и backend locks; ошибки close/commit инъецируются на async driver boundary |
| D: quarantine dependency groups, all-invalid, global veto | D `test_late_fk_failure_rolls_back_parent_group_and_continues_independent_group`, `test_all_invalid_quarantine_commits_zero_target_rows`, `test_quarantine_does_not_bypass_global_validation_gate`; `unit/loading/test_quarantine.py` | Source/FK/parent/related closure; отдельный business group coordinator отсутствует |
| D: replay/conflict/concurrency/UNKNOWN/retention | D idempotency tests из AC-05, `test_replay_rejects_new_snapshot_with_old_mapping`; `unit/loading/test_idempotency.py`; E waiter и corrupt receipt tests | Одинаковый key сериализуется DB advisory lock; receipt не разрешает replay при malformed/oversized/hash/binding mismatch |
| D: audit без secrets, append-only ledger | D `test_success_duplicate_request_and_redacted_audit`, `test_column_grants_cannot_weaken_append_only_ledger`; E corrupt receipt canary | Raw key, DSN, SQL parameters отсутствуют в diagnostics/audit; column grants не обходят append-only |

Имена файлов `test_postgresql_insert.py`, `test_postgresql_transactions.py` и прочие
в исходном плане были проектными. Реальное покрытие выше находится в C/D/E;
отсутствие файла с проектным именем само по себе не означает отсутствие теста.

## Новые проверки этого аудита

- E: 34 PostgreSQL cases на версиях 16 и 18, включая scalar insert/update,
  generated-PK veto, lock cancellation/statement/lock/deadline timeout,
  повторную cancel во время rollback, cancel внутри COMMIT, cleanup failure,
  corrupted/oversized receipt и cancelled same-key waiter.
- SC: 4 lifecycle cases на memory и по 4 на PostgreSQL 16/18, с сохранением refs
  и отказом повторно открыть закрытый execution.
- Property: 30 детерминированных Hypothesis examples, произвольные IDs и source/SQL
  chunk sizes; результат сравнивается с исходными строками через настоящий SQLite.
  Недостаточный bind budget проверяет отказ и пустую таблицу.
- Unit: wire round-trip execution plan, сохранение fingerprint и отказ версии 2.0.0.
- Existing import probe усилен public M13 imports и запретом optional DB drivers.
  Stdlib sqlite3 и Pydantic TypeAdapter прогреваются как зависимости до attribution
  guards; весь SDK импортируется заново под guards. Дополнительно запрещён sqlite connect.

## Команды и фактические результаты

Для краткости `$T` ниже означает `packages/structuraguard/tests`.
Pytest запускается через `uv run --locked --no-sync` с `UV_CACHE_DIR=/private/tmp/structuraguard-uv-cache`.
PG-команды дополнительно используют `-W error::sqlalchemy.exc.SAWarning -m database_integration`.
Сначала выполнены новые/узкие тесты, затем gates. PostgreSQL и общие suites
выполнены с разрешённым Docker/loopback/локальным process watchdog; платных сервисов нет.

| Команда / аргументы pytest | Фактический результат |
|---|---|
| `pytest -q $T/integration/database/test_postgresql_load_acceptance.py` на первой серии новых cancellation/timeout/cleanup tests | **18 passed**, 24.46 s, PG 16/18 |
| Тот же PG файл, `-k 'decimal or generated'` | **6 passed, 18 deselected**, 8.61 s |
| PG файлы `test_postgresql_load_acceptance.py test_postgresql_staging.py`, `-k 'corrupt or waiter or closed_execution'` | **18 passed, 74 deselected**, 16.39 s |
| `pytest -q $T/property/loading/test_batch_properties.py $T/contract/stores/test_memory_staging.py $T/smoke/test_import_side_effects.py` | **19 passed**, 2.29 s |
| `pytest -q $T/unit/loading/test_execution_plan.py` после добавления wire test | **4 passed**, 0.42 s |
| `pytest -q $T/unit/loading $T/unit/database/test_loader_sql.py $T/contract/loading $T/contract/stores $T/security/loading $T/security/stores $T/property/loading $T/smoke/test_import_side_effects.py` | **60 passed**, 2.79 s; wire test проверен отдельно после этого прогона |
| PG файлы `test_postgresql_staging.py test_postgresql_dry_run.py test_postgresql_loader.py test_postgresql_load_outcomes.py test_postgresql_load_acceptance.py` | **240 passed**, 152.93 s |
| `make lint` | **PASS**, 502 файла отформатированы, Ruff без замечаний |
| `make typecheck` | **PASS**, 498 файлов, без ошибок |
| `make test` | **3871 passed, 356 deselected**, 130.96 s; 5 сторонних SWIG/PyMuPDF DeprecationWarning |
| `make test-integration` | **30 passed, 4197 deselected**, 8.30 s |
| `make test-security` | **959 passed**, 32.89 s |
| `make test-database` | **356 passed**, 166.90 s; PG 16/18, SQLAlchemy SAWarning повышен до ошибки |
| `make docs` | **PASS**, strict build; исходные настройки проверки ссылок сохранены |
| `make test-build` без переопределения UV cache | **PASS**, offline wheel/sdist и `distribution verification OK` |
| `git diff --check` | **PASS** |

Начальные неуспешные прогоны также учтены:

- Первый property probe ожидал успешную запись даже при бюджете двух binds для
  двух колонок + RETURNING. Исправлено ожидание нового теста: typed budget veto и
  пустая таблица. Производственный лимит сохранён.
- Attribution import probe потребовал прогрева stdlib sqlite3 и Pydantic TypeAdapter;
  существующие guards сохранены, добавлен запрет `sqlite3.connect`.
- Mypy выявил неаннотированный `ModuleType.connect` в новом probe; заменено на
  существующий в probe приём `setattr`, без `type: ignore`.
- Strict docs отклонил шесть ссылок за пределами MkDocs docs tree. Они заменены
  точными repository-relative путями; strict mode не отключён.
- Первый `make test` внутри sandbox: **74 failed, 3797 passed, 356 deselected**;
  первый integration gate: **21 failed, 9 passed**. Причина — запрет `/bin/ps`
  memory watchdog и локального fake HTTP server. Код парсеров/тесты не менялись;
  общие gates повторены с нужными разрешениями. Security gate исходной цепочки
  после failed integration не запускался и выполнен в повторном прогоне.

Логи: `/private/tmp/structuraguard-m13-acceptance-*.log`.

## Непроверенные или неподдержанные сценарии

| Сценарий из плана | Причина и последствие |
|---|---|
| Полный ingest с обязательными physical/schema/business/provenance M12 layers | Нет coordinator/API required layers на границе loader. Тесты отдельных M12 validators не доказывают gate перед target DML; AC-01 остаётся частичным |
| `would_load_records` и `loaded_records=0` старого LoadReport в dry-run | Реализован отдельный DryRunExecutionPlan по ADR 0027. Нужная интеграция facade/report не поставлена; нулевое изменение БД доказано напрямую |
| Успешный insert/replay DB-generated PK; передача generated keys детям | Loader допускает только доказуемые source keys. Новый отрицательный тест подтверждает veto и неизменную sequence, не успешную поддержку |
| Пустой dataset через public loader | MappingPlan требует непустые mappings; отдельной приёмки пустого sealed snapshot со schema-only manifest нет |
| Diamond/join и shared-parent quarantine в реальном loader; nullable/MATCH FK; concurrent delete lookup parent | Есть graph/M11/M12 unit tests и parent/child/composite PG tests, но нет всех сквозных вариантов. Их полнота не заявляется |
| Deadlock и разрыв backend во время активного DML; timeout именно idempotency lock | Проверены table lock/statement/overall timeout, cancel same-key waiter и отказ close с terminate. Эти случаи не заменяют отдельные серверные deadlock/termination/claim-timeout tests |
| Процессный crash между COMMIT и staging finalize; реальный lost network ACK; многократная cancel после подтверждённого COMMIT | Есть driver boundary fault injection и durable recovery в той же test session. SIGKILL/перезапуск процесса и сетевой proxy не использованы |
| ALTER/DROP/recreate и DDL wait во время load; каждый runtime RLS/trigger/partition вариант | Есть реальные preflight schema/grant mutations и schema comment перед COMMIT. Полная параллельная DDL matrix не прогнана |
| Доступность/подлинность внешних raw/validation/provenance artifacts после retention | Staging хранит ссылки, не открывает artifacts. Их storage/authenticity остаются ответственностью владельца |
| Пиковая память, production-scale throughput, PostgreSQL 15/17 | Тестовые budgets и поддерживаемый CI matrix 16/18; memory profiler/load benchmark и дополнительные версии не запускались |

При review тестов существенных runtime/security findings не выявлено.
Границы AC-01/02/05 и перечисленные непроверенные варианты не позволяют объявить
весь исходный milestone полностью закрытым.
