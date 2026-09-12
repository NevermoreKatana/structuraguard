# M12-B — приёмка локального schema validator

Дата: 2026-09-12. Поставляется standalone JSON Schema service из
[плана M12](M12_validation_engine.md), без batch projection и record engine.
Контракт и controls: [API](../json-schema-validation.md),
[ADR 0023](../adr/0023-local-json-schema-validation.md).

## Матрица

| Требование | Тесты относительно `packages/structuraguard/tests/` |
|---|---|
| Meta-validation и multiple errors | `unit/validation/test_json_schema.py`: malformed keywords, required, bounds, arrays, immutable input, stable paths/codes |
| Draft semantics | `contract/validation/test_draft202012.py`: 30 сценариев через публичный port; compositions, conditions, contains, unevaluated, local anchors, false schema, Decimal/float numbers |
| Refs/network, regex, recursion и budgets | `security/validation/test_schema_boundary.py`: 36 сценариев, включая unused defs, SSRF/file refs, nonprogress cycles, draft-switch bypass, unsafe patterns, forged policy, resource JSON, report/cache caps и длину property names |
| Unicode и детерминизм | `property/validation/test_schema_properties.py`: Hypothesis для path escaping/round-trip и порядка object keys |
| Копируемый пример | `docs/test_m12_json_schema_examples.py`: код руководства в subprocess с network audit guard |
| Packaging/import | Smoke и точный distribution inventory: новые файлы, exports, runtime closure и version constraints |

Новые unit/security/contract tests сначала запускались без реализации; failing
cases подтверждены. Contract matrix обнаружила ошибочную блокировку companion
keywords `then/else/minContains/maxContains`; allowlist исправлен. Отдельный
regression проверяет, что вложенный `$schema` не обходит runtime budgets.

Изменены `contracts/json_schema.py`, `ports/json_schema.py`,
`validation/{json_schema,_schema_input,_schema_regex,_schema_backend}.py`,
package exports, dependencies/lock, import/dependency/packaging checks и документация.
Все предыдущие изменения scalar normalization сохранены.

## Локальные проверки

macOS / Python 3.12.9. Набор нового модуля и копируемый пример: **78 passed**.
Совместимый набор с contracts, ports и import smoke до последних дополнительных
security cases: **622 passed**. Ruff format/check — **412 файлов**, mypy —
**408 файлов**, strict MkDocs build и `git diff --check` — OK.

Первый полный прогон выявил единственное несовпадение: старый packaging-test
ожидал ровно две runtime dependencies. Ожидаемый список обновлён до четырёх,
с сохранением exact names/version bounds. Узкий packaging suite в sandbox
дополнительно встретил известный сбой UV/macOS SystemConfiguration; для полного
набора используется разрешённый запуск вне sandbox. Checks не ослаблялись.

Проверка wheel также воспроизвела различие порядка version specifiers в metadata.
Verifier теперь сравнивает точные bounds независимо от порядка; четыре regression
cases проверяют допустимую перестановку и запрет расширения/удаления границ.
Свежий packaging suite: **30 passed**, offline distribution verification — OK.

После окончательного исправления work budget выполнена команда
`PYTEST_ADDOPTS='-q --tb=short' make test test-integration test-security test-database test-build`
с exit code 0:

| Gate | Результат |
|---|---|
| `make test` | **3500 passed**, 104 deselected, 5 существующих SWIG warnings |
| `make test-integration` | **26 passed**, 3578 deselected |
| `make test-security` | **846 passed** |
| `make test-database` | **104 passed** на PostgreSQL 16/18, SAWarning как error |
| `make test-build` | Offline wheel/sdist rebuild, isolated install и examples smoke: **distribution verification OK** |
| `make lock-check` | **106 packages**, lock актуален |
| `make lint` / `make typecheck` | **412 / 408 файлов**, без ошибок |
| `make docs` / whitespace | Strict build и проверка изменённых/новых файлов — OK |

Для общих tests использованы разрешённые system watchdog, loopback, Docker и
штатный UV cache. Платные/external LLM и живые внешние сервисы не вызывались.

## Security review

Review завершён; существенных открытых findings нет. Дополнительно проверен AST
семи новых production files: executable input и file calls отсутствуют.

Последний security regression выявил недоучёт длины property names в regex work
budget. Ранее длинный ключ проходил при policy `max_evaluations=100`; теперь
стоимость учитывает keys и связанные pattern schemas, а превышение даёт typed
limit error до matching. Регрессия, все 78 checks модуля и полный набор gates
после финального изменения прошли.

Активы: CPU/memory, raw instance, schema keys, локальные resources и cache.
Входы недоверенные; разрешения ограничены CPU и owned memory. Конструктор читает
bundled dependency data, validate не имеет полномочий на file/network/DB retrieval.
Нет `eval`, `exec`, SQL, dynamic imports по пользовательскому имени или LLM calls.

Controls: bounded exact-type intake, strict resource JSON, closed schema/regex
features, in-memory refs, graph cycle check, keyword depth/work counters, exact
numeric arithmetic, capped issues/paths и instance-local cache. All-errors
не включает raw library messages; paths чувствительны и скрыты из repr.

Residual limits: optional dynamic/vocabulary/regex features явно отклоняются;
format — annotation; thread sharing и OS/RSS/wall-clock isolation не гарантируются.
Report/hash не удостоверяют provenance и не разрешают загрузку. Удалённая
CI-матрица Python 3.13/3.14 и benchmark на hard caps локально не выполнялись.
