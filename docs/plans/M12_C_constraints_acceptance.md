# M12-C — приёмка DB constraints и business-rule DSL

Дата: 2026-09-12. Scope: standalone record validators и read-only key reader из
[плана M12](M12_validation_engine.md). Контракт: [API](../db-business-validation.md),
[ADR 0024](../adr/0024-deterministic-record-constraints.md).

## Поставка

- Immutable `ValidationDataset`, typed operands/16 allowlisted operations,
  `BusinessRuleSet`, CHECK attestations, scope/limits, closed reader requests/results.
- Pure async business evaluator и deterministic local DB validator; общий all-errors
  result без raw/driver values. Missing/null/wrong type различаются, repairs нет.
- NOT NULL/default/generated rules, type/length/enum, integer bounds и Decimal
  precision/scale, exact ordered UNIQUE/FK, completed parent scope, upsert identity.
- SQLite/PostgreSQL reader: scope до I/O и после reflection, одна read-only transaction,
  bounded EXISTS, no SQL expressions from caller. PostgreSQL AccessShare до snapshot,
  RLS fail-closed; SQLite mode=ro/query_only/authorizer/deadline.
- Нет новых production dependencies; SQLAlchemy/asyncpg остаются optional extras.
  Экспорты и точный inventory wheel/sdist обновлены. Изменения A/B сохранены.

## Матрица проверок

| Область | Файлы относительно `packages/structuraguard/tests/` |
|---|---|
| Все 16 operations, typed refs, null policies, dates, Decimal tolerance, references | `contract/validation/test_rule_operations.py` |
| All errors, source immutability, exact sum независимо от Decimal context, parent scope | `unit/validation/test_business_rules.py` |
| NOT NULL, enum/CHECK attestation, defaults/generated, bounds/negative scale, composite keys, late parent, shared budget и FK types | `unit/validation/test_db_constraints.py` |
| Rule injection, unknown fields, forged DTO/subclass, huge Decimal exponent, budgets | `security/validation/test_rule_boundary.py` |
| Identifier/value injection, scope before I/O, drift, forged read binding, query budget, timeout | `security/validation/test_constraint_reader.py` |
| Unicode/composite key без concatenation и normalization; Decimal round-trip, reordered batches | `property/validation/test_constraint_boundaries.py` |
| Реальные существующие составные UNIQUE/FK; unchanged rows | `integration/test_sqlite_record_validation.py` |
| PostgreSQL 16/18: upsert same/other identity, FK, NULLS NOT DISTINCT, RLS, locks before reflection, GENERATED ALWAYS parent | `integration/database/test_postgresql_record_validation.py` |
| Offline копируемый пример | `docs/test_m12_rule_examples.py` |
| ABI, import safety, optional dependencies, packaging closure | Contracts/ports/smoke suites и `scripts/verify_distribution.py` |

Tests добавлялись до реализации: первоначальные import failures, затем failing
regressions для FK operands, shared CHECK budget, unbound child, PostgreSQL locks
и GENERATED ALWAYS parent были воспроизведены и исправлены.

## Review и security

Применены structuraguard-review, structuraguard-security и DB checklist.
Существенные открытые findings не обнаружены в проверенном scope. Исправлены:

1. **High:** schema race между PostgreSQL reflection и key SELECT. AccessShare
   берётся до первого SELECT/snapshot; PostgreSQL integration подтверждает lock
   перед reflection на 16/18. DDL не может заменить читаемую relation в этом окне.
2. **Medium:** FK values проверялись только по child type. Теперь parent type
   проверяется до reader и выдаёт полный typed issue вместо implicit coercion.
3. **Medium:** несколько trusted CHECK bindings могли перезапускать budget.
   Теперь имеется aggregate rules cap и общий evaluation budget.
4. **Medium:** unbound child выпадал из суммы. Явный parent-scope issue блокирует
   acceptance; остальные независимые правила продолжают выполняться.
5. **Medium:** read FK к GENERATED ALWAYS identity ошибочно применял write veto.
   Value predicates отделены от local write checks; реальная существующая identity
   читается, а попытка её записать по-прежнему отклоняется.

Exact class intake не доверяет пользовательскому `__module__`, malformed DTO не
передаёт управление serializer hooks. Scalar budgets применяются до Fraction и
serialization. Keys/SQL values не логируются; isolated engine logger и safe errors
сохраняют правила M7. AST/source review не обнаружил eval/exec/dynamic imports по
данным; SQLAlchemy и фиксированные adapter statements не принимают user SQL.

## Границы

Dataset завершён и bounded; сквозная batch/provenance integration — следующий D.
Unknown CHECK/index/collation/deferred/SQLite affinity блокирует acceptance как
unverified. RLS/permission/drift/timeouts не превращаются в empty DB. Attestation
эквивалентности CHECK — обязанность trusted владельца. Report является advisory:
после rollback final enforcement и TOCTOU остаются за будущим loader.

## Проверки

Локальный набор validation (A не входит, B и C входят), SQLite и docs example:
**161 passed**. Новые PostgreSQL проверки после окончательных исправлений:
**8 passed** на 16/18. Ruff: **433 файла**, mypy: **429 файлов**, без ошибок.
Финальный прогон после всех исправлений:
`PYTEST_ADDOPTS='-q --tb=short' make test test-integration test-security test-database test-build`
завершился с exit code 0.

| Gate | Свежий результат |
|---|---|
| `make test` | **3640 passed**, 112 deselected, 5 существующих SWIG warnings |
| `make test-integration` | **27 passed**, 3725 deselected |
| `make test-security` | **877 passed** |
| `make test-database` | **112 passed** на PostgreSQL 16/18; SAWarning как error |
| `make test-build` | Offline wheel/sdist rebuild, isolated install, examples: **distribution verification OK** |
| `make lint` / `make typecheck` | **433 / 429 файлов**, без ошибок |
| `make docs` | Strict MkDocs build и копируемый offline DSL example — OK |
| `uv lock --check` | **106 packages**, lock актуален |
| AST / whitespace | 12 новых production files и `git diff --check` — OK |

Для системных watchdog, loopback, локального Docker и штатного UV cache
использован разрешённый запуск вне sandbox. Платных LLM и внешних рабочих БД нет.
Первый полный прогон также прошёл; после закрытия schema race и identity regression
все gates повторены на окончательном коде.
