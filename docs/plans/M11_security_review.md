# Security review M11 — MappingPlan Validator

Дата: 2026-09-12. Область: текущий незакоммиченный diff M11, включая новые
untracked-файлы, контракты, validator, общий helper порядка FK и manifests
дистрибутива. Остальные milestones не подвергались отдельному security audit.
Основание: [план M11](M11_mapping_plan_validator.md),
[приёмка](M11_acceptance.md), [ADR 0021](../adr/0021-mapping-plan-validation.md).

Найдены и исправлены **1 High, 4 Medium и 1 Low**. Critical не обнаружены.
Каждая проблема воспроизведена падающим regression-тестом до исправления.
Новые regression tests находятся в
`packages/structuraguard/tests/security/mapping/test_m11_security_review.py`.
Ниже указаны строки соответствующих проверок в исправленной версии.

## Trust boundaries

- Недоверенный вход: JSON/DTO MappingPlan, identifiers, operation, confidence,
  descriptors, строки metadata и повреждённые вложенные DTO. Ответ LLM имеет
  полномочия только предложить декларацию.
- Trusted composition root передаёт policy/options и scoped snapshots inspector,
  manifest и profile. Fingerprints проверяют согласованность и свежесть снимков,
  но не удостоверяют их автора и не подписывают право записи.
- Активы: ограничения допустимых DB targets и source PK, целостность checked
  wrapper, конфиденциальность raw input, память/CPU и устойчивость async caller.
- Выходы: typed result/error и безопасные issue codes/locations. Полная явная
  сериализация result/plan/evidence остаётся чувствительной; это не формат логов.

## Findings и исправления

### SG-M11-01 — High: обход разрешения source PK через другой unique key

- **Место:** `packages/structuraguard/src/structuraguard/mapping/_validation_scope.py:162`.
- **Путь эксплуатации:** таблица имеет обычный PK и второй unique key; write
  scope разрешает обе колонки, но `source_identity_allow` не разрешает PK.
  План всё равно передаёт значение PK и объявляет второй unique key как identity
  либо позволяет валидатору вывести его. До исправления insert/upsert получали
  ACCEPTED и wrapper.
- **Влияние:** недоверенная декларация обходила отдельное разрешение на запись
  source PK; будущий writer мог принять навязанный идентификатор. Сам валидатор
  SQL не выполняет, поэтому реальная запись в этом exploit не производилась.
- **Минимальное исправление:** проверять разрешение каждой отображаемой PK
  колонки независимо от выбранного identity/upsert key. Используется существующий
  blocker `MAPPING_SOURCE_IDENTITY_FORBIDDEN`.
- **Regression:** `test_alternative_unique_key_cannot_authorize_unapproved_source_pk`
  — четыре случая insert/upsert × explicit/inferred, плюс положительный control
  с разрешённым PK. Перед исправлением две исходные проверки получили ACCEPTED.

### SG-M11-02 — Medium: raw input утекал через Pydantic serialization warning

- **Место:** `packages/structuraguard/src/structuraguard/mapping/validation.py:103`;
  аналогичные границы constructor и revalidation snapshots.
- **Путь эксплуатации:** повреждённый `model_copy`/`model_construct` содержит
  объект с secret-canary в поле другого типа. Вызов `model_dump` до проверки формы
  выдавал UserWarning с `input_value`. Последующее отклонение DTO уже не убирало
  raw input из stderr или системы сбора warnings/logs.
- **Влияние:** утечка restricted input, identifiers или secrets до безопасного
  typed отказа. `hide_input_in_errors` не защищает serializer warnings.
- **Минимальное исправление:** `warnings=False` только при dump недоверенного
  входа внутри M11; все повторные shape/hash/policy checks сохраняются. Это
  исключает небезопасную диагностику, не отключая validation или quality gates.
- **Regression:** `test_invalid_typed_input_does_not_leak_payload_in_serialization_warning`
  — plan/catalog/manifest/profile/policy/options; все входы отклоняются, canary
  отсутствует в warnings и diagnostics. Первоначальный test фиксировал canary
  в warning при уже отклонённом плане.

### SG-M11-03 — Medium: descriptor budget не останавливал разбор полного плана

- **Место:** `packages/structuraguard/src/structuraguard/mapping/_validation_input.py:207`.
- **Путь эксплуатации:** JSON содержит больше identities/relations, чем
  `max_edges`. Intake добавлял общий PLAN_INVALID и пропускал semantic descriptors,
  но затем передавал исходный массив в `MappingPlan.model_validate`.
- **Влияние:** массив сверх явного budget всё равно разбирался и хешировался;
  нарушение budget маскировалось обычным отчётом. Общий input byte cap оставался,
  поэтому речь об обходе более строгого лимита работы, не о бесконечном входе.
- **Минимальное исправление:** немедленный typed
  `MAPPING_LIMIT_EXCEEDED` до построения descriptor DTO и полного плана.
- **Regression:** `test_descriptor_budget_rejects_before_whole_plan_validation`
  — отдельные identities и relations. До исправления typed отказ отсутствовал.

### SG-M11-04 — Medium: короткий JSON вызывал необработанный Decimal exception

- **Место:** `packages/structuraguard/src/structuraguard/mapping/_validation_input.py:99`.
- **Путь эксплуатации:** например, `{"confidence":1e9999999999999999999}`
  достигал `Decimal` как JSON parse callback. Exponent вне диапазона runtime
  вызывал `decimal.InvalidOperation`, не включённый в обработанные ошибки.
- **Влияние:** отказ от controlled error contract и аварийное завершение проверки
  по короткому недоверенному входу; поведение зависело от Decimal traps/flags caller.
- **Минимальное исправление:** перехватывать `InvalidOperation` и выполнять
  decimal parsing в контексте, принадлежащем одному decode call. Возвращается
  безопасный неполный intake report с `MAPPING_PLAN_INVALID`.
- **Regression:** `test_json_decimal_outside_runtime_range_has_controlled_rejection`
  — положительный/отрицательный чрезмерный exponent, в том числе при отключённом
  trap приложения; внешний Decimal context не изменяется. Обе исходные проверки
  падали с необработанным исключением.

### SG-M11-05 — Medium: catalog limits проверялись после дорогой сериализации

- **Место:** `packages/structuraguard/src/structuraguard/mapping/_validation_input.py:39`;
  вызов перед revalidation в `mapping/validation.py`.
- **Путь эксплуатации:** snapshot укладывается в byte cap, но превышает заданный
  tables/columns/FK budget. До исправления выполнялись dump и вложенная проверка
  metadata, и только затем подсчитывались эти объекты.
- **Влияние:** лишние allocations/CPU сверх явно заданной допустимой структуры
  входа, до отказа. Поток не был безразмерным: общий preflight оставался активен.
- **Минимальное исправление:** ограниченный подсчёт объектов до snapshot dump,
  с явным отказом при повреждённой структуре вложенных DTO; без DB I/O.
- **Regression:** `test_catalog_limits_run_before_snapshot_serialization`
  — tables/columns/FK и spy, запрещающий `DatabaseCatalog.model_dump` до budget
  check. До исправления все три случая вызывали запрещённую сериализацию.

### SG-M11-06 — Low: ранний malformed-JSON report обходил result byte cap

- **Место:** `packages/structuraguard/src/structuraguard/mapping/validation.py:133`.
- **Путь эксплуатации:** malformed JSON и `max_result_bytes`, меньший стандартного
  error report. Ранняя ветка возвращала DTO без общей проверки размера.
- **Влияние:** нарушение заданного output budget. Ответ имел малый фиксированный
  размер без raw payload, поэтому риск ограничен и не даёт произвольного amplification.
- **Минимальное исправление:** применить существующий `_check_result_size` также
  к раннему intake report.
- **Regression:** `test_malformed_json_report_also_obeys_result_budget` — сначала
  отсутствовал typed limit error, после исправления возвращается LIMIT_EXCEEDED.

## Покрытие применимых угроз

| Угроза | Проверка текущего diff и результат |
|---|---|
| Attacker-controlled input | Закрытые DTO, exact types на входе, повторная shape/hash проверка, независимые veto. Исправлены SG-01/02/04. |
| Resource exhaustion | Bytes/depth/numeric/work/issues/result budgets, cancellation; SG-03/05/06. Spy-тесты проверяют раннюю остановку, а не только окончательный статус. |
| Parser exploit / unsafe deserialization | Только bounded JSON и фиксированные Pydantic DTO; duplicate keys/non-finite/malformed input отклоняются. Нет eval/exec/pickle/unsafe YAML/dynamic deserialization. SG-04 закрывает необработанный numeric decode path. |
| XXE/DTD/network | M11 не подключает XML parser, entity resolver или URL loader. Imports/AST boundaries и runtime spies запрещают file/socket/HTTP/DB calls. XML/parser adapters вне текущего diff не объявляются повторно проверенными. |
| Prompt injection / excessive agency | Нет LLM provider/tools или writer capability в validator. SQL/default/CHECK/comments — инертная metadata; plan не может расширить trusted policy, выбрать DDL или unsupported strategy. |
| PII/secrets | Codes и числовые locations не содержат raw values. Serializer warnings закрыты SG-02. Полная сериализация plan/result чувствительна и не должна попадать в diagnostics. |
| SQL / identifier injection | SQL не строится и не исполняется; AST и SQLCompiler/Connection spies. Existing security cases покрывают quotes/comments/semicolon/Unicode, unlisted и exact authorized quoted names. |
| DB allowlist/denylist | Schema/table/column scope и read/write deny precedence; system objects запрещены, lookup не обходит deny. SG-01 дополнительно закрывает обход source PK permission. |
| Schema drift | Пересчёт catalog hash, lineage и отдельные target/policy/writable bindings; fresh SQLite/PostgreSQL inspection после fixture ALTER. Caller не получает автоматический remap. |
| Logs/audit events | M11 не пишет логи или audit events. Проверены косвенные warnings как фактический канал вывода; SG-02. Для внешнего аудита допустимы codes/locations/hashes, не raw snapshots. |
| Path traversal / temp files | Validator не принимает filesystem target и не создаёт временные файлы; file API запрещён runtime spy. Diff distribution verifier меняет только статические manifests; integration fixtures используют pytest tmp_path. |
| Supply chain | Production dependencies и uv.lock не изменены; новые imports — стандартная библиотека и уже закреплённый Pydantic. Packaging/import probes проверяют текущий дистрибутив. Новые лицензии или сетевые установки не требовались. |

## Изменённые этим review файлы

- `packages/structuraguard/src/structuraguard/mapping/`: `_validation_scope.py`,
  `_validation_input.py`, `validation.py` — перечисленные исправления.
- Новый `packages/structuraguard/tests/security/mapping/test_m11_security_review.py`.
- `packages/structuraguard/tests/unit/mapping/test_m11_composite_relations.py`:
  fixture ambiguity содержит два unique key и не содержит PK. Ранее она одновременно
  использовала неразрешённый PK; это отдельный High blocker. Существующие assertions
  NEEDS_REVIEW и отсутствия wrapper сохранены.
- Документация M11: этот отчёт, приёмка и API; ссылка добавлена в MkDocs navigation.

## Проверки

| Команда / набор | Фактический результат |
|---|---|
| Новый `test_m11_security_review.py` | 18 passed, 0.31 s |
| Узкий mapping suite, команда ниже | 439 passed, 8.63 s |
| `make lint` | Ruff format/check пройдены, 383 Python files |
| `make typecheck` | mypy: no issues, 379 source files |
| `make test` | 3258 passed, 96 deselected, 105.18 s |
| `make test-integration` | Пройден |
| `make test-security` | Пройден |
| `make test-database` | Пройден на PostgreSQL 16/18; SAWarning как error |
| `make test-build` | Wheel/sdist и distribution verification OK |
| `make docs` | Strict MkDocs build пройден |
| `git diff --check` | Пройден |

Команды из корня репозитория:

```bash
UV_CACHE_DIR=/private/tmp/structuraguard-m11-uv-cache uv run --locked --no-sync pytest -q --tb=short packages/structuraguard/tests/security/mapping/test_m11_security_review.py
UV_CACHE_DIR=/private/tmp/structuraguard-m11-uv-cache uv run --locked --no-sync pytest -q --tb=short packages/structuraguard/tests/unit/mapping packages/structuraguard/tests/contract/mapping packages/structuraguard/tests/property/mapping packages/structuraguard/tests/security/mapping packages/structuraguard/tests/unit/contracts/test_m11_validation_contracts.py packages/structuraguard/tests/integration/test_mapping_plan_validation.py packages/structuraguard/tests/smoke/test_mapping_boundaries.py
UV_CACHE_DIR=/private/tmp/structuraguard-m11-uv-cache make lint typecheck
PYTEST_ADDOPTS='-q --tb=short' make test test-integration test-security test-database test-build
UV_CACHE_DIR=/private/tmp/structuraguard-m11-uv-cache make docs
git diff --check
```

Полный последовательный make-прогон завершился с exit code 0. Docker, loopback,
системный memory watchdog и штатный UV cache для offline packaging использовались
с разрешённым доступом за пределами sandbox. Остались пять существующих
PyMuPDF/SWIG deprecation warnings в основном/document integration suites.

История воспроизведения: первые пять cases SG-01/02/03 — **5 failed** до fixes;
затем SG-04 — **2 failed, 12 passed**; SG-05 — **3 failed, 14 deselected**;
SG-06 — **1 failed, 17 deselected**. После исправлений весь новый файл зелёный.
Выполнен итоговый correctness/security review изменений; незакрытых
Critical/High/Medium findings в рассмотренном diff не осталось. Existing tests,
lint/typecheck и security gates не ослаблены.

## Остаточные границы

Review не удостоверяет происхождение внешнего snapshot и не заменяет record/load
engine. Live grants, TOCTOU непосредственно перед SQL, значение FK/enum/CHECK,
транзакции, rollback/idempotency и исполнение циклических стратегий остаются за
scope M11. Валидация структурно пригодного snapshot не разрешает использовать
его как capability без свежего inspection и trusted composition root.

Budgets ограничивают работу внутри входной границы, но не allocation объекта
caller до вызова и не гарантируют OS/RSS или wall-clock isolation. Платные и
реальные внешние LLM API при review не использовались.
