# Security review M12 — normalization и validation

Дата: 2026-09-13. Scope — текущий незакоммиченный diff M12, включая untracked
production/tests, exports, зависимости и packaging changes. Основание:
[план](M12_validation_engine.md), [аудит приёмки](M12_acceptance_audit.md),
ADR [0022](../adr/0022-conservative-normalization-and-validation.md)–
[0025](../adr/0025-provenance-replay-and-validation-report.md).
Старые parsers/DB adapters проверялись только на затронутых границах и существующими
regression suites; отдельный повторный аудит остальных milestones не выполнялся.

В первом review найдены и исправлены **три Medium**. Critical/High не обнаружены. Каждая проблема
воспроизведена падающим regression test до изменения production-кода.
Строки ниже относятся к исправленной версии.

## Исправления финального review {#m12-final-review-fixes}

Финальный review обнаружил ещё три Medium; все исправлены локально без расширения
scope или изменения публичных сигнатур. Новых production dependencies нет.

| Finding | Исправление | Regression test |
|---|---|---|
| Потеря temporal precision и ложный pass UNIQUE | `domain/constraint_values.py`: PostgreSQL timestamp с неподдержанным modifier получает `DB_CONSTRAINT_UNVERIFIED`; общий predicate защищает также прямой reader до I/O | `unit/validation/test_m12_temporal_precision.py`; `integration/database/test_postgresql_record_validation.py::test_pg_timestamp_precision_cannot_hide_unique_conflicts` на PostgreSQL 16/18 |
| Hooks при обходе forged DTO keys/storage | `normalization/_boundary.py`, `validation/_bounded.py`, `validation/json_schema.py`: точные native dict/str проверяются до iteration/comparison/serialization | `security/validation/test_m12_final_review.py::test_foreign_dto_keys_never_execute_hooks`, `test_schema_storage_subclass_is_rejected_before_iteration` |
| Неверный error contract `generated_at` | `validation/provenance.py`: точный datetime с UTC требуется до replay; отказ — SDK `PROVENANCE_INPUT_INVALID` | `security/validation/test_m12_final_review.py::test_invalid_timestamp_is_rejected_before_replay` |

Пути production относительно `packages/structuraguard/src/structuraguard/`, tests —
`packages/structuraguard/tests/`. До исправлений: 14 unit/security failures и 4 PG
failures; 3 контрольных случая проходили. После исправлений все 21 cases проходят.
Узкий M12 suite — 409 passed; lint — 456 файлов, strict mypy — 452 файла, OK.
Повторный review этих пяти production modules не выявил новых существенных findings.
Итоговые gates и оставшийся scope отражены в [состоянии проекта](../codex/PROJECT_STATE.md).

## Trust boundaries

- Недоверенные данные: scalar inputs, поддельные Python DTO, схемы/локальные schema
  resources, DSL declarations, catalog metadata, projected records, source refs,
  locations, normalization sidecars и claimed fingerprints.
- Trusted owner выбирает policies/limits, pure custom implementations, physical
  snapshot/context, DB target и разрешённые columns. Неверная форма policy всё равно
  должна отклоняться до пользовательских hooks; trusted ownership не оправдывает
  молчаливый переход на более широкие defaults.
- Активы: raw/normalized/evidence integrity, PII и credentials, CPU/memory, read-only
  DB scope, стабильный error contract и cleanup. A/B/DSL/D не имеют полномочий
  открывать locations, выполнять SQL или делать network retrieval. C reader имеет
  отдельное ограниченное read-only разрешение.
- Findings о Python objects требуют передачи forged/subclass DTO через SDK/adapter.
  JSON сам по себе не создаёт Python classes. Защита intake предотвращает вызовы hooks
  в этой границе; изоляция уже загруженного произвольного Python-кода не заявляется.

## Findings

### SG-M12-01 — Medium: проверка identity Python input запускает hooks

- **Место:** `packages/structuraguard/src/structuraguard/normalization/_boundary.py:51`,
  storage — строка 79, enum — строка 102. Вариант с metaclass также затрагивал
  `packages/structuraguard/src/structuraguard/validation/_rule_input.py:68`,
  `packages/structuraguard/src/structuraguard/validation/_bounded.py:80` и
  `packages/structuraguard/src/structuraguard/validation/_schema_input.py:54`.
- **Путь эксплуатации:** adapter передаёт subclass `StringScalar` с поддельным
  `__module__="structuraguard.contracts.common"` и собственным `model_serializer`.
  Прежняя проверка по module prefix пропускала объект и вызывала его serializer.
  Другие варианты — чужой `StrEnum.__len__` в copied policy и dict subclass,
  подставленный в `__dict__` настоящего DTO. Дополнительный вариант: foreign object
  с metaclass `__hash__`/`__eq__`. Проверка класса через membership в set/tuple
  вызывала эти hooks в normalization/schema/rules/provenance intake.
- **Влияние:** side effects до допустимого normalizer callback и подмена исходного
  raw на этапе сериализации. Subclass и storage cases проходили intake; enum case
  вызывал пользовательский hook дважды перед последующим отказом. Metaclass cases
  выдавали произвольный RuntimeError из hook вместо безопасного SDK отказа.
- **Минимальное исправление:** instance-local allowlist точных identities SDK
  contracts/enums (membership по `id`, без hash/eq самого класса) и exact `dict`
  storage до обращения к serializers/методам. Native scalar types сравниваются
  через `is`. Module name не используется как удостоверение класса. Malformed
  inputs получают существующие безопасные коды соответствующего intake.
- **Security regressions:**
  `packages/structuraguard/tests/security/normalization/test_m12_normalization_security_review.py`:
  `test_spoofed_contract_module_cannot_execute_scalar_serializer`,
  `test_foreign_enum_is_rejected_before_its_length_hook`,
  `test_forged_contract_storage_is_rejected_before_mapping_hooks`.
  Также `test_normalizer_does_not_hash_or_compare_foreign_class_identity` и
  `test_intake_never_hashes_or_compares_foreign_class_identity` (schema/rules/provenance)
  в validation security review file. Семь cases проверяют typed отказ до hooks;
  scalar case дополнительно исключает restricted input из ошибки.

### SG-M12-02 — Medium: truthiness limits обходит проверку policy

- **Место:** `packages/structuraguard/src/structuraguard/validation/business_rules.py:66`
  (номер уточнён после добавления public docstring).
- **Путь эксплуатации:** передать `False`/`0` либо subclass limits с `__bool__`,
  возвращающим False. Выражение `limits or RecordValidationLimits()` выполняло hook
  и заменяло входной объект defaults до `checked()`. Например, заявленный
  `max_evaluations=1` превращался в default 100000.
- **Влияние:** malformed policy принимается без отказа, явный budget теряется;
  чужой truthiness hook получает исполнение до exact-type boundary. Встроенные
  абсолютные ограничения сохранялись, поэтому не заявляется их полный обход.
- **Минимальное исправление:** defaults выбираются только при `limits is None`;
  остальные входы проходят существующий exact-type intake с `RULE_INPUT_INVALID`.
- **Security regressions:**
  `packages/structuraguard/tests/security/validation/test_m12_validation_security_review.py`:
  `test_business_rule_limits_reject_falsey_non_dto` (False/0) и
  `test_business_rule_limits_never_call_foreign_truthiness_hook`.
  Все три cases ранее не выдавали требуемый отказ.

### SG-M12-03 — Medium: shared tuple fanout раздувает очередь до budget check

- **Место:** `packages/structuraguard/src/structuraguard/validation/_rule_input.py:109`.
- **Путь эксплуатации:** в forged `ValidationDataset` вложить tuples с множеством
  повторных ссылок на следующий уровень. Прежний код проверял длину одного tuple
  и число уже посещённых nodes, но добавлял весь следующий уровень в `pending`.
  Очередь росла на каждом уровне прежде, чем срабатывал общий node/depth limit.
- **Влияние:** память процесса расходуется существенно выше ожидаемой границы
  `max_nodes` ещё до Pydantic revalidation. Малый regression с 16 уровнями по 1024
  ссылок и `max_nodes=2048` показал 1 057 436 дополнительных bytes allocations;
  увеличение fanout масштабировало проблему. До ACCEPTED выполнение не доходило.
- **Минимальное исправление:** до `pending.extend` проверять суммарные queued и
  новые children против оставшегося node budget. Срабатывает существующий
  `SECURITY_LIMIT_EXCEEDED`, без выделения заведомо избыточной очереди.
- **Security regression:** тот же validation test file,
  `test_alias_fanout_cannot_overallocate_the_pending_intake_queue`. Input строится
  до `tracemalloc`; проверяются typed отказ и дополнительный peak ниже 400000 bytes.
  До исправления порог нарушался. Это regression очереди на CPython текущего стенда,
  а не универсальная гарантия process RSS.

## Применимые угрозы и controls

Пути тестов в таблице относительно `packages/structuraguard/tests/`.

| Угроза | Проверенная граница / controls | Наблюдаемая проверка |
|---|---|---|
| Attacker-controlled input | Exact-type intake до serializers; closed DTO unions; повторное подтверждение lineage | Новые SG-M12-01/02; `security/validation/test_rule_boundary.py`, `test_provenance_boundary.py` |
| Resource exhaustion | Scalar/trace caps, schema depth/nodes/properties/regex/work/cache bounds, DSL queue/evaluation/key/issue budgets, replay budgets | Новый SG-M12-03; `security/validation/test_schema_boundary.py`, `test_rule_boundary.py`; `security/normalization/test_boundary.py` |
| Parser exploit / unsafe deserialization | M12 принимает bounded snapshots, использует M5 executor; не загружает pickle/YAML/Python objects из текста. JSON resources используют strict JSON decoding, duplicate/nonfinite/deep input отклоняется | `test_schema_boundary.py::test_local_resource_json_intake_is_strict_and_bounded`; `contract/validation/test_provenance_formats.py`; существующий `security/parsers/test_markup_security.py` |
| XXE / DTD / network | Нет remote refs, динамических dialects и retrieval callbacks; local URN resources переданы owner. Locations не открываются; M4 XML/HTML protections сохраняются | `test_schema_boundary.py::test_remote_refs_are_rejected_without_io`, `test_successful_local_schema_validation_has_no_io`; `test_provenance_boundary.py::test_locations_are_never_opened_and_safe_summary_hides_arbitrary_codes`; XML DTD/XXE regression |
| Prompt injection / excessive agency | Текст остаётся данными; 16 allowlisted DSL operations, нет execution/import/SQL expressions по входу; M12 не вызывает LLM и не выдаёт writer authority | `security/normalization/test_boundary.py::test_executable_text_stays_data`; `security/validation/test_rule_boundary.py::test_operation_allowlist`; AST review |
| PII / secrets | Raw и sidecars не копируются в issues; detailed report sensitive, `safe_summary` использует закрытые code buckets без IDs/paths/hashes; driver exceptions заменяются SDK codes | Existing forged serializer/warning tests; `test_provenance_boundary.py::test_locations_are_never_opened_and_safe_summary_hides_arbitrary_codes` |
| SQL / identifier injection | SQLAlchemy expressions и bound values; identifiers из проверенного reflected catalog; LOCK identifiers quoted; CHECK text сопоставляется с trusted typed representation, не исполняется | `security/validation/test_constraint_reader.py::test_identifiers_are_quoted_values_are_bound`; DSL injection cases |
| DB allowlist / denylist | Target schemas/tables и отдельный column allowlist до I/O, повторная проверка reflected snapshot, system schemas/views/partitions и неподдержанная key semantics отклоняются | `test_constraint_reader.py::test_reader_rejects_scope_before_connection`; `security/validation/test_m12_unknown_constraints.py` |
| Schema drift | Reinspection в той же read-only transaction, fingerprint сравнивается до lookup; PostgreSQL schema locks до snapshot, RLS не даёт ложное подтверждение | `test_constraint_reader.py::test_drift_is_detected_inside_read_transaction`; `integration/database/test_postgresql_record_validation.py` |
| Unsafe logs / audit events | Новые validators не пишут logs/audit. Reader использует приватный непередающий logger и hide_parameters; safe summary отделена от sensitive full report | no-I/O/safe-summary regressions, review DB exception paths; import probes |
| Path traversal / временные файлы | Pointers и refs — данные; A/B/DSL/D не создают temp files и не открывают пути. SQLite path задаётся trusted target, `mode=ro`. Packaging diff расширяет inventories/requirements, существующий TemporaryDirectory workflow не изменён | Provenance no-open test; SQLite scope tests; packaging archive/path regression suite и offline distribution verifier |
| Supply chain | Новые core requirements, locked artifacts, MIT licenses, bounded wrappers вокруг JSON Schema/referencing; optional format extras не включены | Metadata/lock inspection, package tests и offline wheel/sdist verifier; внешние источники ниже |

AST-проверка 35 изменённых/новых production files не обнаружила вызовов встроенных
`eval`, `exec`, `__import__`, `compile`, dynamic import, process shell execution или `shell=True`.
Она дополняет ручной review, а не доказывает отсутствие всех возможных side effects.

## Production dependencies

Diff добавляет прямые `jsonschema>=4.26,<5` и `referencing>=0.37,<0.38`.
В lock установлены 4.26.0 и 0.37.0. Проверены metadata лицензий: MIT у обоих,
а также у новых элементов core closure `attrs` 26.1.0,
`jsonschema-specifications` 2025.9.1 и `rpds-py` 2026.6.3.
Эти runtime packages уже присутствовали в workspace lock; M12 делает их частью
обязательных runtime dependencies. Новый `types-jsonschema` является dev dependency.
Artifacts закреплены hashes; verifier проверяет exact dependency bounds и closure.

Официальные release metadata: [jsonschema 4.26.0](https://pypi.org/project/jsonschema/4.26.0/),
[referencing 0.37.0](https://pypi.org/project/referencing/0.37.0/).
На дату просмотра страницы maintainers
[jsonschema advisories](https://github.com/python-jsonschema/jsonschema/security/advisories) и
[referencing advisories](https://github.com/python-jsonschema/referencing/security/advisories)
не содержали опубликованных advisories. Это не утверждение об отсутствии всех CVE
или неизвестных уязвимостей. Полный внешний CVE scan транзитивной цепочки и аудит
исходников dependencies не выполнялись; native `rpds-py` расширяет trusted runtime.
Приложение-потребитель отвечает за собственный lock при разрешении диапазонов SDK.

## Выполненные проверки

Новые regressions запускались до fixes (7 failures SG-M12-01, 3 failures SG-M12-02,
1 failure SG-M12-03), затем узкие наборы и доступные gates. Имена новых файлов
сделаны уникальными после выявленной pytest import-name collision; pytest settings
и existing tests не изменялись.

| Команда / набор | Фактический результат |
|---|---|
| Новые regressions в двух файлах | 11 passed |
| Промежуточный normalization unit/contract/property/security | 114 passed, до добавления metaclass case |
| Промежуточный DSL/DB unit, DSL contract и security regressions | 117 passed, до добавления metaclass cases |
| Финальный узкий M12 A–D, docs examples, SQLite | 388 passed |
| `make lint` | 452 файла; format/check OK |
| `make typecheck` | strict mypy, 448 файлов; OK |
| `make test` | 3790 passed, 112 PostgreSQL cases deselected; 137.52 s |
| `make test-integration` | 30 passed, 3872 deselected; 8.36 s |
| `make test-security` | 922 passed; 32.60 s |
| `make test-database` | 112 passed, PostgreSQL 16/18; SAWarning как error; 18.66 s |
| `make test-build` | Offline wheel/sdist build, reinstall и `distribution verification OK` |
| `make lock-check` | 106 packages; lock актуален |
| `make docs` | Strict MkDocs и локальные links/anchors; OK |
| `git diff --check` | OK |

Все финальные команды завершились с exit 0. Main/integration сохраняют пять
существующих SWIG DeprecationWarning PDF backend; filters/checks не ослаблялись.
Integration/security входят в main, поэтому числа не суммируются как уникальные
tests. Для локальных watchdog, loopback и Docker использовано разрешённое исполнение
вне sandbox. Внешние платные API и рабочие БД не вызывались.

Логи: `/private/tmp/structuraguard-m12-security-regressions.log`,
`/private/tmp/structuraguard-m12-security-narrow.log`,
`/private/tmp/structuraguard-m12-security-static.log`,
`/private/tmp/structuraguard-m12-security-final-gates.log`,
`/private/tmp/structuraguard-m12-security-docs.log`.
Исправленные ранее состояния не подменяют результаты финального прогона.

Production changes этого review ограничены пятью intake/evaluator modules,
перечисленными в findings. Добавлены два test files (11 cases), обновлены boundary
docs, audit/state и MkDocs nav. Новых dependencies, DB/DDL/writer/parser изменений нет.

```bash
UV_CACHE_DIR=/private/tmp/structuraguard-m12-uv-cache uv run --locked --no-sync pytest -q --tb=short \
  packages/structuraguard/tests/security/normalization/test_m12_normalization_security_review.py \
  packages/structuraguard/tests/security/validation/test_m12_validation_security_review.py
UV_CACHE_DIR=/private/tmp/structuraguard-m12-uv-cache make lint typecheck docs
UV_OFFLINE=1 PYTEST_ADDOPTS='-q --tb=short' make test test-integration test-security test-database test-build lock-check
git diff --check
```

## Оставшиеся границы

- Нет открытых Critical/High/Medium findings в проверенном diff после исправлений.
  Автоматические projection/coordinator, derived schema 1.3 и MappingPlan/catalog
  revalidation из общего плана ещё не поставлены: standalone acceptance не является
  разрешением loader. См. частичные критерии в аудите приёмки.
- Policies, physical anchors, implementations normalizers и `ValidationLayerResult`
  задаёт trusted composition owner. Self-consistent hashes не удостоверяют автора.
  При согласованной подмене bytes и trusted anchor нет независимого доказательства.
- DB prechecks advisory и не закрывают race до будущей записи. Constraints/rollback
  финальной транзакции остаются обязанностью loader. Произвольные CHECK/collation/
  expression-index semantics получают unverified, а не имитацию SQL evaluator.
- Bounded in-process validation не является OS sandbox и не обещает абсолютный
  wall-clock/RSS cap. Custom Python implementations не изолированы от I/O владельца.
- Проверки выполнены на macOS/Python 3.12.9; другие ОС/Python и hard-cap benchmark
  не запускались. Внешние LLM API и рабочие БД не использовались.
