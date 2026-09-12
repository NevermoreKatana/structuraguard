# Приёмка M12-D: provenance и итоговый report

Дата: 2026-09-13. Scope текущей задачи — physical/normalization replay и агрегация.
[ADR 0025](../adr/0025-provenance-replay-and-validation-report.md) уточняет границу
относительно общего [плана M12](M12_validation_engine.md).

## Матрица

| Требование | Реализация и проверка |
|---|---|
| Source ID/fingerprint, существование ref | M5 replay; foreign source, rehashed unknown ref |
| Raw/location/selection binding | Сравнение exact DTO с replay; forged raw, pointer и selector |
| Missing non-null evidence | `PROVENANCE_REQUIRED`, unresolved record, без ремонта |
| Normalization trace | Trusted field policy/registry; replay всей цепочки, forged/missing trace |
| All-errors и aggregates | Layer/record/entity/value/check/code order; независимые findings, record dedupe |
| Safe summary | Без scalars/locations/IDs/hashes; произвольные codes заменяются закрытым bucket |
| Immutability и fingerprint | Sidecar references, canonical round-trip, timestamp исключён из hash |
| Unicode/перестановки | Hypothesis для JSON keys/locations и порядка findings |
| Bounds и boundary | Общий snapshot budget, hostile subclass serializer не вызывается |
| EOF/cancellation | Нет verified evidence после позднего source failure; iterator закрыт при отмене |
| Совместимость | Legacy ValidationReport не изменён; schema 1.1 — новый subclass и exports |
| Форматы | JSON/CSV/LOG/XML/HTML; PDF/DOCX/XLSX через настоящие parser adapters |

## Проверки

Новые tests находятся в `unit/validation/test_provenance_report.py`,
`security/validation/test_provenance_boundary.py`,
`property/validation/test_provenance_properties.py`,
`contract/validation/test_provenance_formats.py` и `docs/test_m12_provenance_example.py`.
Все команды завершились с exit 0 на финальном коде:

| Gate | Результат |
|---|---|
| `make lint` | 444 файла; format/check OK |
| `make typecheck` | strict mypy, 440 файлов |
| `make test` | 3701 passed; 112 PostgreSQL tests отдельно |
| `make test-integration` | 30 passed |
| `make test-security` | 892 passed |
| `make test-database` | 112 passed, PostgreSQL 16/18, SAWarning как error |
| `make test-build` | offline wheel/sdist build, reinstall и examples OK |
| `make lock-check` | 106 packages, lock актуален |
| `make docs` | strict build и локальные links/anchors OK |
| AST / whitespace | пять новых production modules без dynamic execution/broad except; `git diff --check` OK |

Для parser watchdog, loopback и локального Docker использован разрешённый запуск
вне sandbox. Пять предупреждений main/integration — существующие SWIG types PDF
backend. Внешние LLM и рабочие БД не вызывались. Первый полный прогон выявил eager
import; после исправления повторные gates прошли. Последняя правка связывания replay
policy проверена regression test и повторным main/integration/security/packaging.
DB-код после успешного PostgreSQL suite не менялся.

## Review

Проведены `structuraguard-review` и `structuraguard-security`. Закрыты найденные
regressions: nested findings влияли на hash порядком поступления; forged extra
fields могли отбрасываться Pydantic; eager M5 facade импортировал LLM зависимости;
missing evidence зависело от начала replay; policy hash не включал replay controls.
Для каждого поведения добавлен regression либо использован существующий smoke guard.
Глобальные findings считают records без repeated expansion всего dataset.
К концу review существенных открытых findings в поставленном scope нет.

## Ограничения

Physical snapshot/source context и implementations normalizers предоставляет
доверенный owner. Hash не является capability. Full report sensitive.
Автоматическая projection и повторная MappingPlan/catalog validation после изменения
нормализованных значений требуют отдельного coordinator; builder принимает
проверенные layer results от владельца и явно блокирует непроверенные required layers.
