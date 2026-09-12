# M12-A — приёмка scalar normalizer registry

Дата: 2026-09-12. Scope: самостоятельный scalar registry из
[плана M12](M12_validation_engine.md). Batch integration и Validation Engine B–D
этим отчётом не объявляются готовыми.

## Реализованное поведение

Instance-owned `NormalizerRegistry` предоставляет immutable snapshot/freeze,
точное разрешение ID/version и configurable synchronous `Normalizer` protocol.
Доступны 11 built-ins: `trim`, `empty_to_null`, `boolean`, `integer`, `decimal`,
`money`, `date`, `datetime`, `phone`, `email`, `uuid`.

`NormalizationResult` сохраняет raw/input/output и связную историю каждого
изменения; no-op также отражён как вызванный step. Failed chain атомарно возвращает
исходный input, сохраняя историю попытки. `normalize_value()` сохраняет полный
исходный `NormalizedValue` с selection/provenance. Legacy wire/hash не изменены.
Неоднозначные даты/числа не выбираются по догадке; money использует только Decimal.
Встроенные операции не выполняют I/O. Новых production dependencies нет.

API и копируемый пример описаны в [руководстве](../normalization.md), решение о
scalar sidecar и shared M8 grammar — в [ADR 0022](../adr/0022-conservative-normalization-and-validation.md).

## Матрица проверок

Пути тестов относительно `packages/structuraguard/tests/`:

| Требование | Подтверждение |
|---|---|
| Все built-ins, exact Decimal, currency, explicit locale, UTC | `unit/normalization/test_normalizers.py`: значения, trace, native signed zero/scale, Decimal context, minute precision, offset и calendar overflow |
| Raw preservation, atomicity, immutable snapshot, custom config | `unit/normalization/test_normalizer_registry.py`: source parent/selection/provenance, freeze/version/order, failed chain, falsey invalid policy и отказ async adapter |
| Unicode, separators, round-trip, ambiguity | `property/normalization/test_normalization_properties.py`: Hypothesis для Unicode trim, RU grouping, date/UUID round-trip, unknown date/number locale, digit lookalikes, phone digits и email local part |
| Один port для built-ins и custom | `contract/normalization/test_protocol.py`: детерминизм, неизменность inputs, связная история для всех 11 built-ins и configurable fake |
| No I/O и недоверенный input/output | `security/normalization/test_boundary.py`: запрет file/network/DNS/process/locale calls, inert executable text, forged DTO, mutations, output/trace budgets и отсутствие raw в serialization warnings |
| Совместимость M8 и упаковки | Основной suite; import-side-effect smoke; точный список exports/package files в distribution verifier |

Набор normalization: **100 passed**. Пример money из руководства исполнен:
проверены исходная строка, точный Decimal и пять transformations.

## Проверки окружения

Локальная среда: macOS, Python 3.12.9; PostgreSQL 16/18 через Docker.

| Команда | Результат |
|---|---|
| `make lint` | Ruff format/check: **400 файлов**, OK |
| `make typecheck` | mypy: **396 файлов**, OK |
| `make lock-check` | **105 packages**, lock актуален |
| `make test` | **3408 passed**, 104 deselected, 5 существующих SWIG warnings |
| `make test-integration` | **26 passed**, 3486 deselected |
| `make test-security` | **810 passed** |
| `make test-database` | **104 passed**, SQLAlchemy SAWarning как error |
| `make test-build` | Offline wheel/sdist rebuild, isolated install и examples smoke: **distribution verification OK** |
| `make docs` | Strict MkDocs build и проверка внутренних ссылок — OK |
| `git diff --check` и проверка новых файлов | Whitespace/conflict markers — OK |

Для тестов использованы `PYTEST_ADDOPTS='-q --tb=short'` и временный
`UV_CACHE_DIR=/private/tmp/structuraguard-m12-uv-cache`. Existing document memory
watchdog, loopback и Docker потребовали разрешённого запуска вне sandbox.
Offline packaging сначала остановился из-за отсутствия `hatchling` в отдельном
временном cache; штатный UV cache содержит locked dependencies, повторный
`make test-build` прошёл. Зависимости и checks не ослаблялись.
`uv lock --check` также прошёл вне sandbox после сбоя macOS SystemConfiguration
в sandbox. Основной suite повторён с сохранением полного локального отчёта.

## Review и оставшиеся границы

Correctness/security review завершён. Закрыты дефекты: scalar budgets ошибочно
затрагивали policy metadata; serialization warnings могли раскрыть invalid value;
async adapter принимался до вызова; falsey invalid policy заменялась default policy.
Все случаи покрыты regression tests. Конфликт имён pytest modules и exact-export
expectation исправлены; существующие архитектурные и security checks сохранены.

Custom Python implementation остаётся доверенным extension point, без OS sandbox:
registry не может гарантировать отсутствие I/O или hidden state произвольного
plugin. Snapshot fingerprint описывает ID/version, а не код и происхождение.
Полный result чувствителен; hash не анонимизирует raw values. Scalar result не
доказывает physical provenance и не заменяет derived batch lineage либо M8–M11
revalidation. Phone/email проверяют ограниченную canonical форму, не существование
номера/ящика. Другие locale и formats требуют отдельного явного расширения.

Удалённая CI-матрица Python 3.13/3.14 и нагрузочный benchmark на hard caps не
выполнялись. В полном suite остаются пять существующих SWIG deprecation warnings.
