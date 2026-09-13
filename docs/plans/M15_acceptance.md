# M15 — проверка критериев приёмки

Дата: 2026-09-13. Основание: девять критериев из
[плана M15](M15_sdk_orchestrator.md).
Проверяется текущая композиция M3–M14, включая ограничения
[ADR 0034](../adr/0034-sdk-orchestrator.md). Реальные платные LLM не используются.

Последующий финальный review выявил два пробела критериев 5/7/8:
смену dry-run отклонённым параллельным execute и потерю подтверждённого commit
при исключении loader. Исправления, дополнительные 17 regression cases и свежие
quality gates — в [состоянии M15](../codex/PROJECT_STATE.md#m15-review-fix-checks).
Ни критерии, ни прежние assertions не ослаблялись.

Пути тестов ниже относительно `packages/structuraguard/tests/`.
«Новый» означает добавленный при этой проверке; остальные tests существовали
после реализации M15. Проверки M11–M14 указаны как component evidence и не
выдаются за самостоятельные E2E сценарии SDK.

## Матрица «критерий → тест»

| № | Критерий плана | Наблюдаемый тест | Проверяемый эффект |
|---|---|---|---|
| 1 | Typed async/sync API, эквивалентность, constructor compatibility | **Новый:** `unit/pipeline/test_acceptance_lifecycle.py::test_sync_async_and_stepped_results_are_equivalent`; существующие `unit/test_facades.py::test_facade_uses_explicit_config`, `test_facade_uses_explicit_parser_registry`, `unit/test_operations.py::test_all_sync_guards_precede_coroutine_creation`; mypy | Все пошаговые методы вызываются через оба facades; при одинаковых IDs/clocks совпадают fingerprints, планы, validation и load reports. Config/registry сохраняют identity; sync внутри loop не создаёт coroutine. |
| 2 | Полный flow, законные transitions, отсутствие записи при planning | `unit/pipeline/test_end_to_end.py::test_complete_pipeline_and_dry_run`; **новые** `test_acceptance_lifecycle.py::test_loading_cannot_skip_required_gates`, `test_terminal_state_has_no_outgoing_transition`, `test_analysis_and_standalone_inspection_never_load` | Точная последовательность 16 stage events; запрет входа в LOADING до STAGING и выхода из каждого terminal status; analyze/standalone inspection не вызывают planner, staging references или loader. |
| 3 | ParsePlan/MappingPlan validation до execution и при reuse; полный EOF | `unit/pipeline/test_failures.py::test_saved_parse_plan_is_revalidated_without_llm`, `test_changed_source_never_uses_saved_plan`, `test_mapping_reuse_requires_exact_normalized_snapshot`; **новые** `security/pipeline/test_acceptance_gates.py::test_validly_hashed_foreign_mapping_rejected_before_key_reads`, `test_parser_without_terminal_manifest_cannot_reach_semantics_or_db`, `test_required_json_schema_failure_never_stages`; component `unit/loading/test_projection.py::test_incomplete_stream_rejected_before_io` | ParsePlan reuse проходит повторную validation. Новый normalized fingerprint требует нового MappingPlan; подмена source/DB/policy binding не вызывает key reads или writes. Незавершённый physical parser закрывается до semantics/DB; M13 отвергает неполный normalized stream до I/O. Обязательный JSON Schema failure блокирует staging. |
| 4 | Parsing/LLM policies, no hidden fallback, без LLM на каждую строку | `unit/pipeline/test_llm.py::test_parsing_modes_with_fake_provider`, `test_no_llm_does_not_silently_change_llm_first`, `test_repeated_rows_do_not_consume_one_llm_call_per_row`, `test_provider_failure_keeps_attempt_without_hidden_fallback`; `test_mapping_llm.py::test_mapping_llm_result_reaches_load_report`; **новые** `security/pipeline/test_acceptance_llm.py::test_no_llm_applies_to_unresolved_database_mapping`, `test_scanner_deny_precedes_external_parsing_provider` | Три parsing mode, отдельный NO_LLM, 100 повторных rows, M10 success/error. Проверяются реальные counts fake provider; scanner deny останавливает egress; NO_LLM действует и при неоднозначном DB mapping. |
| 5 | Dry-run: полная validation, ноль изменений, отдельный прогноз | `integration/database/test_sdk_orchestrator.py::test_source_to_report[dry_run=True]`; `unit/pipeline/test_end_to_end.py::test_complete_pipeline_and_dry_run[dry_run=True]`; component `integration/database/test_postgresql_dry_run.py::test_real_dry_run_leaves_target_staging_and_sequence_exactly_unchanged` | PostgreSQL target rows, persistent staging и sequence snapshots неизменны; validation complete, `loaded_records=0`, прогноз находится в DryRunExecutionPlan. Fake boundary проверяет ноль loader calls и references. |
| 6 | PostgreSQL staging, separate principals, scope, schema/grants, transaction/ledger | `integration/database/test_sdk_orchestrator.py::test_source_to_report`, `test_drift_and_permission_errors_prevent_load`, `test_failure_after_actual_dml_rolls_back`; **новый** `test_sdk_unknown_commit_can_be_reconciled_by_same_ledger_binding`; components `security/mapping/test_m11_validation_security.py::test_unlisted_malicious_identifiers_remain_inert`, `integration/database/test_postgresql_load_outcomes.py::test_success_duplicate_request_and_redacted_audit`, `test_same_key_with_different_binding_is_rejected_before_staging_claim` | Production adapters используют inspector/dry_writer. Поздний schema/grants drift запрещает commit; ошибка после реального DML откатывает target. SDK передаёт idempotency key; при потере COMMIT reply делает ровно одну попытку. Явный M13 replay с тем же sealed request и новым guard подтверждает commit без повторного DML/ledger marker. Scope/SQL payload veto подтверждены M11. |
| 7 | Отдельные failure/review/security/cancel/timeout/rollback/unknown/post-commit outcomes; cleanup | **Новые** `unit/pipeline/test_acceptance_cancellation.py::test_cancellation_reaches_active_port_and_closes_run`, `test_deadline_cancels_read_only_port`, `test_acceptance_outcomes.py::test_missing_loader_outcome_remains_unknown_without_retry`, `test_acceptance_lifecycle.py::test_completed_source_rejects_second_execution`, `test_busy_lease_rejects_concurrent_stage_without_poisoning_run`, `test_one_facade_has_independent_concurrent_runs`; существующие `test_failures.py::test_cancellation_before_load_marks_sealed_staging_cancelled`, `test_post_commit_hook_cannot_claim_rollback` | Отмена достигает probe/parser/inspector/reader/planner/loader await; timeout — пяти read-only ports. Parser закрыт, отмена не публикует payload. SEALED переводится в CANCELLED; EXECUTING/UNKNOWN не объявляются NOT_STARTED/ROLLED_BACK. Повторный execute отклонён до изменения результата; конкурирующая операция не портит активный run. |
| 8 | Только фактическое evidence; безопасные hooks/audit/errors | **Новые** `unit/contracts/test_m15_orchestration.py::test_envelope_rejects_foreign_fingerprint`, `test_envelope_rejects_foreign_execution_evidence`, `test_safe_summary_contains_no_source_canary`, `security/pipeline/test_acceptance_gates.py::test_missing_audit_key_fails_before_source_probe`; существующие `unit/pipeline/test_audit_and_sync.py::test_signed_audit_references_verify_against_independent_anchor`, `test_failures.py::test_hook_failure_retains_safe_cause_and_stops_before_writes`, `test_step_exception_does_not_retain_raw_context` | Envelope rejects mismatched fingerprints, включая вложенные live/dry-run reports. Partial result не выдумывает load reports; canaries отсутствуют в hooks/safe_summary/exception context. HMAC chain проверяется по независимому anchor; отсутствующий signing key запрещает даже probe. |
| 9 | Отрицательные тесты ограничений; scope не подменяет весь §33 | **Новые** `property/pipeline/test_acceptance_limits.py::test_oversized_source_stops_before_detection`, `security/pipeline/test_acceptance_gates.py::test_value_changing_normalization_is_explicitly_blocked`, `test_strict_risky_format_requires_sandbox_before_parser_execution`, `unit/pipeline/test_acceptance_outcomes.py::test_missing_load_dependencies_never_stage`; существующие `unit/pipeline/test_llm.py::test_unknown_structure_requires_review`, `test_ambiguous_table_is_not_accepted_by_confidence_alone` | Hypothesis проверяет byte cap при разных размерах/chunks до probe. Изменяющая значения normalization требует review. Strict risky format не исполняется без sandbox; отсутствие loader не создаёт staging. Unknown/ambiguous structure останавливается именно на semantic analysis. |

## Дефекты, подтверждённые новыми тестами

1. `test_completed_source_rejects_second_execution`: повторный `execute` на
   завершённом lease возвращал прежний COMPLETED. Admission теперь отклоняет
   terminal source до смены dry-run или запуска stage. Исходный result неизменен.
2. `test_missing_loader_outcome_remains_unknown_without_retry[executing]`:
   после ошибки loader staging EXECUTING оставлял NOT_STARTED. Теперь outcome
   UNKNOWN; отсутствие commit response не считается доказательством отсутствия DML.
3. `test_envelope_rejects_foreign_execution_evidence`: wire envelope принимал
   чужие database/normalized/mapping fingerprints вложенного live/dry-run result.
   Добавлены только недостающие cross-report проверки в `IngestResult`.

Все три regression были сначала запущены с исходной реализацией и падали на
assertions поведения. Дополнительные первые падения новых tests относились к
ошибкам fixtures: audit-unsafe UUID, отсутствующим полям routing policy,
имени semantic field и попытке повторно использовать закрытый resource guard.
Fixtures исправлены по существующим contracts; production guards не ослаблялись.

## Команды и результаты

Команды выполнялись с `UV_CACHE_DIR=/private/tmp/structuraguard-m15-uv-cache`.
Для offline distribution check использован существующий
`UV_CACHE_DIR=/Users/katana/.cache/uv`, содержащий build wheels.
Workers, локальные HTTP sockets и Docker запускались вне ограниченного sandbox;
внешние платные API не вызывались.

Новые tests сначала запускались отдельными файлами, затем вместе с SDK module:

```bash
uv run --locked --no-sync pytest \
  packages/structuraguard/tests/unit/pipeline \
  packages/structuraguard/tests/security/pipeline \
  packages/structuraguard/tests/property/pipeline \
  packages/structuraguard/tests/unit/contracts/test_m15_orchestration.py \
  packages/structuraguard/tests/unit/test_facades.py \
  packages/structuraguard/tests/unit/test_operations.py \
  packages/structuraguard/tests/docs/test_m15_example.py -q

uv run --locked --no-sync pytest -W error::sqlalchemy.exc.SAWarning \
  -m database_integration \
  packages/structuraguard/tests/integration/database/test_sdk_orchestrator.py \
  -k unknown_commit -q
```

| Команда | Фактический результат |
|---|---|
| Узкий SDK набор выше | 282 passed |
| Новый PostgreSQL recovery test | 2 passed, PostgreSQL 16/18 |
| `make lint` | Ruff format/check успешно, 597 файлов |
| `make typecheck` | mypy успешно, 593 source files |
| `make test` | 4644 passed, 442 database cases deselected, 5 upstream deprecation warnings |
| `make test-database` | 442 passed, PostgreSQL 16/18 |
| `make test-integration` | 30 passed, 5056 deselected, 5 upstream deprecation warnings |
| `make test-security` | 1401 passed |
| `make test-build` | wheel/sdist, offline rebuild/install и installed examples успешно |
| `make docs` | MkDocs strict успешно; первоначальная ошибка ссылки на anchor исправлена |
| `git diff --check` | Успешно |

Добавлены 234 pytest cases с учётом параметризации: 232 в default suite и два
PostgreSQL cases. Integration/security subsets пересекаются с default suite;
counts таблицы не складываются как число уникальных tests. Все проверки запускались
без ослабления lint/typecheck, отключения tests или production dependencies.

## Непроверенные сценарии и причины

- Реальные cloud LLM, провайдерская тарификация и удалённый transport: исключены
  заданием. Проверены FakeLLMProvider, scanner decisions и существующие локальные
  provider/HTTP contract suites; реальные модели не оценивались.
- Production URL/path transport, OS sandbox, durable host artifact retention и
  внешняя доставка audit: готовых integrations в M15 нет. Проверены gates и
  локальные signatures, но не эксплуатационные гарантии чужого host.
- O(batch) memory на больших файлах, spill/restart/resume, generated PK propagation
  и изменение normalized values при load: вне утверждённого bounded copy-only
  scope. Отказы по лимиту и неподдержанной normalization проверены; performance
  benchmark и успешный load для этих возможностей не заявляются.
- Автоматический cross-run replay MappingPlan/ingest: M5 record IDs содержат
  run identity. Проверен явный отказ при fingerprint mismatch. Recovery
  подтверждён на существующем M13 sealed request, сохранённом host, а не через
  повторный `ingest` с новым normalized snapshot.
- Принудительное прерывание некооперативного CPU callback, kill процесса во время
  COMMIT и все сочетания repeated cancellation/cleanup failure: не моделировались.
  Проверены cooperative I/O cancellation, deadlines и потеря ответа после реального
  PostgreSQL COMMIT; это не доказательство crash recovery для любого host.
- Зависимые FK/quarantine/business-rule варианты в полном SDK source→report:
  отдельные новые E2E не добавлялись. Их contracts проверяются существующими
  M11–M13 suites; новый SDK test проверяет обязательный JSON Schema gate.

Новые tests не заменяют и не отключают прежние проверки. Существенные findings
review, относящиеся к проверенным сценариям, устранены указанными тремя правками;
перечисленные ограничения сохраняются.
