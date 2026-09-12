# M11 — строгая валидация декларативного MappingPlan

Статус: готов к ручному commit и последующему PR в `main` в scope M11.
Все семь критериев подтверждены локальными проверками; commit/PR не созданы.
[Результаты приёмки и исправления финального review](M11_acceptance.md).
Дата исходного плана и реализации: 2026-09-12. Ниже сохранён исходный design;
актуальный контракт и ограничения описаны в [API M11](../mapping-plan-validation.md).

## Цель

Получать для ограниченного декларативного MappingPlan полный, воспроизводимый
отчёт применимых проверок и выдавать `ValidatedMappingPlan` только при отсутствии
блокирующих проблем, без чтения данных, обращения к LLM и исполнения SQL.

Основание: извлечены **только** §14.1, §16, §18, §20.3–20.4 и M11 из
`StructuraGuard_SDK_Technical_Specification.md`. Команда извлечения:
`python scripts/extract_spec_sections.py "14.1. Проверка Mapping Plan" "16. Валидация" "18. Загрузка в БД" "20.3. Разделение DB users" "20.4. Запрет DDL" "M11. MappingPlan Validator"`.
Архитектурные ограничения: [ADR 0001](../adr/0001-public-api-and-run-policies.md),
[ADR 0017](../adr/0017-canonical-catalog-and-dependency-graph.md),
[ADR 0019](../adr/0019-deterministic-mapping-candidates.md),
[ADR 0020](../adr/0020-llm-semantic-mapping-proposals.md).

### Наблюдаемое поведение до и после

- До M11 `contracts/mapping.py` проверял форму, дубли, диапазон confidence,
  fingerprint плана и отдельные ссылки. `MappingPlanValidationRequest` прекращает
  проверку на первом несовпадении lineage или неизвестной ссылке. Самостоятельного
  валидатора и соответствующего port тогда не было. `StructuraGuard.validate_plan`
  остаётся заглушкой; готовый сервис M11 вызывается отдельно.
- `MappingPlan` 1.0.0 содержит плоские `FieldMapping` и общую операцию
  `insert_only`/`upsert`; identity, relation и transformation descriptors отсутствуют.
  M9 выдаёт candidates, M10 — proposals, оба не создают разрешение на загрузку.
- После M11 сервис проверяет все независимые правила, возвращает стабильные codes
  и расположение каждой проблемы. Ошибки policy/type/identity/FK не компенсируются
  confidence. Отказ или необходимость review всегда исключают checked wrapper.

## Критерии приёмки

Все пункты сопоставлены с наблюдаемыми tests в
[матрице приёмки](M11_acceptance.md#m11-acceptance-matrix); исправления review
имеют отдельные regression cases. Checklist отражает подтверждённый scope M11,
а не готовность будущего loader.

- [x] Корректные `insert_only` и `upsert` проходят проверки на catalog-v1 для SQLite
  и PostgreSQL; каждая запрещённая или недоказанная предпосылка имеет явный исход.
- [x] Один план с неизвестным source, запрещённой колонкой, несовместимым типом,
  отсутствующим ключом и низким confidence возвращает все независимые проблемы.
  Отсутствующая таблица не порождает выдуманные ошибки её типов или ключей.
- [x] Scope проверяется на schema/table/column уровнях; deny всегда сильнее allow.
  Generated, identity ALWAYS, views и non-writable targets запрещены для записи.
- [x] Drift, target binding, inspection policy и validation policy проверяются
  отдельно. Даже совпадающий schema hash не отменяет запрет записи.
- [x] Порядок диагностики, decision и validation fingerprint не зависят от hash seed,
  часов, порядка обхода dict/set и последовательности независимых проверок.
- [x] Нет DB/LLM/file/network I/O, преобразования значений, DDL, SQL compilation или
  мутации входов. Размер входов, обходов и отчёта ограничен до дорогих операций.
- [x] Legacy DTO и snapshots читаются совместимо; неподдержанный legacy catalog
  отклоняется с `DATABASE_METADATA_UNSUPPORTED`, без выдуманной metadata.

## Подготовка к ручному commit и PR — 2026-09-12 {#m11-commit-readiness}

Ветка `feat/m11-mapping-plan-validator`; HEAD, локальный `main` и проверенный
`origin/main` совпадают: `a85b205a29f3dce410d4ce6a4e04502533165e9c`.
В index нет staged changes. Commit, push и PR не выполнялись.

- [x] Семь критериев приёмки подтверждены; матрица и regression cases актуальны.
- [x] Четыре Medium финального review исправлены; повторный review не выявил существенных findings.
- [x] Полные gates повторены на окончательном коде после последнего уточнения FK aliases.
- [x] Русские public docstrings, пример, ограничения, ADR 0021 и PROJECT_STATE актуальны.
- [x] Проверен `git diff --check`, включая отдельную whitespace-проверку untracked файлов.
- [x] Проверены все 47 файлов-кандидатов: secrets, debug calls, conflict markers и случайные generated files не обнаружены.
- [x] `site/`, `dist/`, pytest/mypy/Ruff caches игнорируются Git и не входят в список commit.
- [x] Публичный API, legacy wire/hash и направление зависимостей проверены; production dependencies не добавлены.
- [ ] Пользователь создаёт ручной commit.
- [ ] Пользователь создаёт PR в `main`; удалённый CI проверяет опубликованный commit.

### Фактически выполненные команды

Python локального окружения — **3.12.9**, macOS. Полный make-прогон завершился
с exit code 0. Числа ниже относятся к окончательному коду, а предыдущие этапы
и воспроизведения дефектов сохранены в [отчёте приёмки](M11_acceptance.md).

| Команда | Фактический результат |
|---|---|
| `UV_OFFLINE=1 make lock-check` | Lockfile актуален, 105 packages resolved; файл не изменён. |
| `UV_CACHE_DIR=/private/tmp/structuraguard-m11-uv-cache make lint typecheck` | Ruff: 387 файлов, mypy: 383 файла, ошибок нет. |
| `make test` | **3290 passed**, **104 deselected**, 5 существующих SWIG warnings. |
| `make test-integration` | **26 passed**, **3368 deselected**, 5 существующих SWIG warnings. |
| `make test-security` | **782 passed**. |
| `make test-database` | **104 passed**, PostgreSQL 16/18, SQLAlchemy SAWarning как error, без skips. |
| `make test-build` | Wheel/sdist, offline rebuild/install/import и examples smoke: **distribution verification OK**. |
| `UV_CACHE_DIR=/private/tmp/structuraguard-m11-uv-cache uv run --locked --no-sync pytest -q --tb=short packages/structuraguard/tests/docs` | **130 passed**, включая 9 сценариев копируемого примера M11. |
| `UV_CACHE_DIR=/private/tmp/structuraguard-m11-uv-cache make docs` | Strict MkDocs build, внутренние ссылки и anchors — OK. |
| `git diff --check` | Whitespace — OK. |
| `git diff --no-index --check /dev/null <path>` для каждого файла-кандидата | Whitespace новых и изменённых файлов — OK; полный список ниже. |
| `git status --short`, `git diff --stat`, `git diff --cached --stat` | 47 файлов: 16 source, 22 tests, 7 docs, 2 config/build; staged changes отсутствуют. |
| `git check-ignore -v site dist .pytest_cache .mypy_cache .ruff_cache` | Все перечисленные generated directories исключены правилами Git. |
| `git branch --show-current`, `git rev-list --left-right --count main...HEAD`, `git rev-parse HEAD` | Ветка выше, расхождение с локальным main: `0 0`. |
| `git ls-remote --heads origin main` | SHA удалённого main совпал с HEAD; команда только читала refs. |
| `.venv/bin/python --version`, `uv python list --only-installed --offline` | Проект использует 3.12.9; локальные Python 3.13/3.14 также установлены, CI-окружения для них этим этапом не создавались. |

Полная команда тестов/сборки:
`PYTEST_ADDOPTS='-q --tb=short' make test test-integration test-security test-database test-build`.
Markers и assertions не отключались. Deselected PostgreSQL cases из основного
suite отдельно выполнены через `make test-database`.

Read-only Python-аудит получил кандидатов через
`git diff --name-only HEAD -z` и `git ls-files --others --exclude-standard -z`.
Для каждого файла проверены тип/суффикс, отсутствие бинарных данных и whitespace;
поиск private-key/token/credential-URI patterns и conflict markers дополнялся
AST-проверкой debug calls в production коде и review содержимого diff.
Совпадений secrets/debug artifacts не найдено. Это проверка текущих 47 файлов,
а не аудит всей Git history.

Первый запуск `uv lock --check` и `uv python list` внутри sandbox завершился
panic macOS SystemConfiguration; повтор вне sandbox прошёл offline.
Полным тестам также потребовались разрешённые system watchdog, loopback,
Docker и штатный UV cache. Эта ошибка среды устранена повторным запуском,
проверки из-за неё не пропущены.

### Непроверенные сценарии и причины

| Проверка / сценарий | Причина и граница утверждения |
|---|---|
| Удалённый CI Ubuntu 24.04, Python 3.12/3.13/3.14 | Commit/PR ещё не опубликованы по инструкции пользователя. Локальный suite выполнен на macOS/Python 3.12.9; отдельные CI environments не создавались. Установленные Python 3.13/3.14 не считаются пройденной матрицей. |
| Benchmark на верхних лимитах, RSS и wall-clock SLA | Проверены конечные budgets и отказ при их превышении; нагрузочная сертификация не входит в M11 и отдельного benchmark не проводилось. |
| Реальная загрузка, writer grants, TOCTOU перед SQL, staging, rollback/idempotency | Validator является чистой проверкой снимков; record/load engine ещё не реализован. Эти сценарии не объявлены готовыми. |
| Реальные платные LLM API | Запрещены задачей и не нужны M11; validator не вызывает LLM. Используются offline/fake/contract tests существующих модулей. |

Не осталось пропущенных обязательных локальных quality gates.
Пять SWIG deprecation warnings относятся к существующим документным тестам.
Совпадение fingerprints не является авторизацией; полные result/policy остаются
чувствительными и не предназначены для обычного logging.

### Файлы для ручного commit {#m11-commit-files}

Список включает tracked changes и все новые файлы; generated directories сюда
не входят. Проверка не изменяла staging area.

**Исходники — 16 файлов:**

- `packages/structuraguard/src/structuraguard/contracts/__init__.py`
- `packages/structuraguard/src/structuraguard/contracts/mapping.py`
- `packages/structuraguard/src/structuraguard/contracts/mapping_rules.py`
- `packages/structuraguard/src/structuraguard/contracts/mapping_validation.py`
- `packages/structuraguard/src/structuraguard/domain/database_graph.py`
- `packages/structuraguard/src/structuraguard/mapping/__init__.py`
- `packages/structuraguard/src/structuraguard/mapping/_inputs.py`
- `packages/structuraguard/src/structuraguard/mapping/_validation_identity.py`
- `packages/structuraguard/src/structuraguard/mapping/_validation_input.py`
- `packages/structuraguard/src/structuraguard/mapping/_validation_relations.py`
- `packages/structuraguard/src/structuraguard/mapping/_validation_report.py`
- `packages/structuraguard/src/structuraguard/mapping/_validation_scope.py`
- `packages/structuraguard/src/structuraguard/mapping/_validation_types.py`
- `packages/structuraguard/src/structuraguard/mapping/validation.py`
- `packages/structuraguard/src/structuraguard/ports/__init__.py`
- `packages/structuraguard/src/structuraguard/ports/mapping.py`

**Тесты — 22 файла:**

- `packages/structuraguard/tests/contract/mapping/test_m11_hashseed.py`
- `packages/structuraguard/tests/contract/mapping/test_m11_validation_port.py`
- `packages/structuraguard/tests/docs/test_m11_examples.py`
- `packages/structuraguard/tests/fakes/mapping_validation.py`
- `packages/structuraguard/tests/integration/database/test_postgresql_m11_review.py`
- `packages/structuraguard/tests/integration/database/test_postgresql_mapping_validation.py`
- `packages/structuraguard/tests/integration/test_mapping_plan_validation.py`
- `packages/structuraguard/tests/property/mapping/test_m11_validation_properties.py`
- `packages/structuraguard/tests/security/mapping/test_m11_acceptance_security.py`
- `packages/structuraguard/tests/security/mapping/test_m11_incomplete_dto.py`
- `packages/structuraguard/tests/security/mapping/test_m11_security_review.py`
- `packages/structuraguard/tests/security/mapping/test_m11_validation_security.py`
- `packages/structuraguard/tests/smoke/test_mapping_boundaries.py`
- `packages/structuraguard/tests/unit/contracts/test_m11_validation_contracts.py`
- `packages/structuraguard/tests/unit/mapping/test_m11_acceptance.py`
- `packages/structuraguard/tests/unit/mapping/test_m11_composite_relations.py`
- `packages/structuraguard/tests/unit/mapping/test_m11_relations_identity.py`
- `packages/structuraguard/tests/unit/mapping/test_m11_review_regressions.py`
- `packages/structuraguard/tests/unit/mapping/test_m11_rules.py`
- `packages/structuraguard/tests/unit/mapping/test_m11_types.py`
- `packages/structuraguard/tests/unit/mapping/test_m11_validation.py`
- `packages/structuraguard/tests/unit/ports/test_m02_protocols.py`

**Документация — 7 файлов:**

- `docs/adr/0021-mapping-plan-validation.md`
- `docs/codex/PROJECT_STATE.md`
- `docs/mapping-plan-validation.md`
- `docs/plans/M11_acceptance.md`
- `docs/plans/M11_mapping_plan_validator.md`
- `docs/plans/M11_security_review.md`
- `docs/public-api.md`

**Навигация и проверка дистрибутива — 2 файла:**

- `mkdocs.yml`
- `scripts/verify_distribution.py`

## Затронутые контракты

Ниже пути к исходникам относительно `packages/structuraguard/src/structuraguard/`.
Таблица сохраняет исходный design; фактические exports, файлы и ограничения
описаны в [API M11](../mapping-plan-validation.md).

| Файл / контракт | Текущее ограничение и изменение M11 |
|---|---|
| `contracts/mapping.py`: `MappingPlan`, `FieldMapping` | Добавить явно версионированные identity/relation descriptors для 1.1.0. В 1.0.0 новые поля отсутствуют в serialization и старом content hash; новые descriptors в 1.0.0 запрещены. Сохранить закрытые операции и immutable DTO. |
| `contracts/mapping.py`: `MappingPlanValidationRequest` | Сохранить прежний строгий constructor. Новый port принимает plan, manifest и catalog раздельно: семантически ошибочный набор достигает накопителя issues до создания legacy request. Успешный набор дополнительно проходит прежний constructor. |
| `contracts/mapping_validation.py` — новый | `MappingValidationPolicy`, `MappingValidationOptions`, `MappingIssueLocation`, `MappingPlanInputReport`: типизированная policy без credentials, budgets, безопасные locations и отчёт об ошибках загрузки wire payload. Не создавать второй MappingPlan или второй validator engine. |
| `contracts/mapping.py`: result / wrapper | Переиспользовать `MappingPlanValidationResult`, `ValidatedMappingPlan`, `ValidationDecision`, `ValidationIssue`. Добавить опциональные M11 evidence: validation-policy fingerprint, fingerprint эффективной writable projection и locations, согласованные с `issues`. Старые поля обязательности и запрет wrapper при отказе сохраняются. |
| `contracts/database.py`: `MappingPolicyRef`, `LoadPolicy` | `MappingPolicyRef` содержит лишь ID/hash, а `LoadPolicy` — ссылки и safety flags. Не трактовать их как раскрытый allowlist или доказательство grants. Их wire format и существующий смысл не менять. |
| `contracts/deterministic_mapping.py`: `MappingScope` | Переиспользовать exact column refs и canonical scope hash; пустой `allow` запрещает всё. Schema/table restrictions применять дополнительно из trusted validation policy. |
| `ports/mapping.py`, `mapping/validation.py`, `mapping/__init__.py` | Добавить async port `MappingPlanValidator` и его детерминированную реализацию с явно переданной policy/options. Реализация принимает готовые snapshots; зависимости на SQLAlchemy, adapter и LLM отсутствуют. |

`DatabaseCatalog` уже содержит PK/unique/FK, расширенные типы, defaults,
generated/identity metadata, kind объекта и dependency graph. `writable` означает
структурную допустимость, а не grants. `database_fingerprint` пересчитывает только
catalog-v1: имена, типы, constraints/indexes, comments и capabilities; opaque IDs,
target/policy, writable и derived graph в hash не входят.
`NormalizedDatasetManifest` даёт semantic schema и lineage, но не все значения
и не доказательство их уникальности. Для дополнительных type evidence принимать
необязательный `NormalizedDataProfile` M8 с проверкой manifest/content/profile
bindings; отсутствие профиля не заменять повторным чтением source.

### Совместимость и выбранные решения

1. **Накопление issues перед legacy request.** Ослабление существующего constructor
   сломало бы `test_m02_invariants.py`. Выбран раздельный вход нового сервиса;
   прежний request остаётся дополнительным invariant check после успешных правил.
   Wire intake использует те же правила и accumulator, затем создаёт строгий DTO.
   Независимые ошибки закрытой формы и дубли собираются до завершающего
   `model_validator`; обход через `model_construct` запрещён.
2. **Минимальное расширение MappingPlan 1.1.0.** Identity descriptor указывает
   target table, ordered key column refs и закрытый способ идентификации;
   relation descriptor — FK ID, child/parent tables, source entity bindings и
   ordered column pairs, способ получения FK. Не добавлять SQL, свободные
   expressions, произвольные joins, scripts или transformation DSL.
   Для legacy insert допустима доказанная стратегия source key либо DB-generated
   identity; legacy upsert без однозначного подтверждённого ключа не принимается.
3. **Структурная валидация отделена от record/load gates.** §16 требует проверки
   значений и provenance, §18 — staging, транзакции и идемпотентность. M11 описывает
   необходимые последующие gates, но не реализует loader или validation engine.
   `ACCEPTED` означает пригодность декларации, не успешный импорт и не полномочие.
4. **Отдельный validation-policy hash.** Не переопределять существующий
   `target_policy_fingerprint`, который привязан к inspection scope/limits.
   Новые thresholds, identity rules, schema/table scope, `MappingScope.fingerprint`
   и options входят в M11 evidence отдельно; изменение policy требует новой проверки.

Долгоживущее решение о wire 1.1.0, diagnostics и границе acceptance принято
в [ADR 0021](../adr/0021-mapping-plan-validation.md). Во время исходного
планирования новые контракты ещё не существовали; теперь они реализованы
и проверены в описанном M11 scope.

## Правила валидации

### 1. Existence, writability и scope

- Разрешать `SemanticFieldRef` через manifest; `CatalogColumnRef` — через точные
  table/column IDs каталога с проверкой принадлежности schema и table. Не разбирать
  opaque ID как qualified SQL name, не применять fuzzy matching или transliteration.
- Эффективный scope — пересечение trusted inspection scope, validation schema/table
  allowlist и `MappingScope.allow` за вычетом всех deny. Пустой allow на любом уровне
  закрывает соответствующий scope; неизвестный selector — ошибка policy.
  Deny родителя закрывает потомков; column allow не открывает запрещённую таблицу.
  Системные объекты запрещены независимо от пользовательского allow.
- Policy names разрешать по правилам dialect без самодельного casefold: SQLite
  использует ASCII identifier rules, PostgreSQL сохраняет регистр quoted names.
  Сравнения после resolution идут по exact refs; одноимённые таблицы разных schemas
  и Unicode confusables не считаются одной таблицей.
- Table kind должен быть `table`, table и каждая записываемая column — writable;
  generated и identity ALWAYS исключены. Identity BY DEFAULT / SQLite rowid alias
  разрешают source value лишь при отдельном разрешении policy.
- Проверить required targets на каждой затронутой таблице: non-null column без
  default/generation/identity должна иметь единственный источник либо поддержанный
  relation binding. `FieldMapping.required=False` не отменяет DB requirement.
  Default SQL хранится только как metadata; валидатор его не вычисляет.
- Отдельно проверить конфликт источников, повтор target и неоднозначное объединение
  нескольких source entity types в одну target row. Split одного entity между
  таблицами требует явных связанных relation descriptors; Cartesian fan-out запрещён.

Текущие SQLite/PostgreSQL inspectors отклоняют непустые column selectors **до I/O**.
M11 применяет column scope к готовому каталогу через `MappingScope`, не объявляя
поддержку ограниченного reflection колонок. Metadata-only refs PK/FK/generated
могут быть видимы, но не становятся writable. Существование внешних parent rows
требует отдельно разрешённого lookup на load boundary; скрытые таблицы не отражаются
повторно ради подробной диагностики.

### 2. Type compatibility

Проверять canonical `DatabaseType`, а не похожесть имён. Переиспользовать pure
evidence из `mapping/_compatibility.py`, где оно применимо, не считать M9 score
доказательством. Сохранить прежнее ранжирование M9; строгие решения вынести
в `mapping/_validation_types.py` с явной версией правил.

| Вход / target | Строгое решение |
|---|---|
| Совпадающие integer, Decimal, boolean, text, date, timezone-aware datetime | Совместимая семья; отдельно учитывать nullable, диапазон, length, precision/scale и timezone. Bool не является integer. |
| Integer → Decimal | Разрешить при допустимых параметрах; без доказательства диапазона оставить обязательную record-проверку. SQLite integer имеет 64-bit storage, PostgreSQL integer — границы native типа. |
| Money/Decimal → float, identifier с ведущими нулями → number | Запретить потерю точности/формата; не исправлять автоматически. |
| String → numeric/date/datetime/UUID, смена timezone, truncation | Нужна явная предварительная нормализация; M11 не выполняет cast. Выдать `MAPPING_TRANSFORMATION_REQUIRED`, wrapper отсутствует. |
| Email/UUID как semantic type | Не выводить physical kind только из semantic name. Проверить representation по M8; строки email совместимы с text, UUID-format сам по себе не доказывает native UUID representation. |
| Enum, domain, arrays, nested object/JSON | Рекурсивно проверить представимую структуру и базовый тип в пределах depth budget. Enum labels, элементы, domain constraints требуют value checks; отсутствие нужной metadata — `MAPPING_TYPE_UNVERIFIED`. SQL CHECK/domain expressions не исполнять. |
| Unknown/mixed/неполный evidence | Не выдавать auto acceptance для недоказанной совместимости; `NEEDS_REVIEW`. Известный конфликт или overflow — `REJECTED`. |

Профиль может доказать нарушение (`null_count`, длина, extrema), но выборка
и statistical uniqueness не доказывают отсутствие нарушений во всём потоке.
При `ACCEPTED` остаются обязательными record-level NOT NULL/range/length/enum,
UNIQUE/FK/CHECK, JSON Schema Draft 2020-12 с закрытыми remote `$ref`, business rules
и physical provenance validation. Исполнение этих проверок находится за scope M11.

### 3. Identity и upsert keys

- Применить порядок §18.2: explicit identity key → разрешённый source PK →
  безусловный unique constraint → настроенный natural key → ручное решение.
  На одном уровне несколько подходящих keys означают неоднозначность, а не выбор
  первого по порядку reflection. Некорректный explicit key нельзя заменить fallback.
- Для upsert нужен полный состав подтверждённого PK/unique key; natural key
  разрешён trusted policy и также обеспечен реальным unique constraint/index.
  LLM declaration, `IdentityHint`, имя `id` и наблюдаемая уникальность не подходят.
- Проверить наличие всех source components, совместимость типов, запрет null
  в identity, writable eligibility и разрешение source PK. Не использовать
  partial/expression/invalid unique index, неоднозначный nullable unique либо
  deferred conflict arbiter как portable upsert key. Учитывать dialect capabilities.
- Upsert имеет insert branch: обязательные insert columns должны быть покрыты;
  нельзя обновлять generated/ALWAYS identity или менять identity key как обычное
  update value. Один ключ должен одинаково идентифицировать обе ветки операции.
- Для insert разрешена явная DB-generated identity с пропуском соответствующей
  колонки; это не гарантирует idempotency повторного запуска. Таблица без
  доказанной стратегии не получает wrapper. Ручное решение требует новой
  fingerprint-bound декларации/policy и не обходит constraint/security запреты.

### 4. FK и relation resolution

- Для каждого relevant FK проверить реальные table/column refs, направление,
  ordered composite pairs, совместимость типов и полное покрытие компонентов.
  Из нескольких FK между теми же таблицами нужен явный FK ID; имя поля не решает связь.
- Поддержать source-supplied FK и прямую связь mapped parent→child с полным
  source-pair evidence. Existing-parent lookup допускается только как закрытый
  descriptor по подтверждённому ключу и разрешённым read refs; разрешение строк
  остаётся обязательным load gate. Автоматическое создание отсутствующего parent
  и подстановка значения по одной части composite FK запрещены.
- Пропущенный nullable/default FK допустим согласно metadata; обязательный FK
  без value source/resolution strategy отклоняется. Mixed-null composite values
  и фактическое существование родителей проверяются по records при исполнении.
- Построить граф именно запланированных записей на основании M7 FK graph.
  Стабильный parent-before-child order вычислять существующим domain алгоритмом
  с минимальным выделением общего helper при необходимости. Несвязанный цикл
  вне плана не блокирует план; global `load_order=None` не заменять выдуманным order.
- Self-reference и цикл в выбранном графе дают
  `CYCLIC_DEPENDENCY_REQUIRES_STRATEGY`. Для M11 выбрать контролируемый отказ;
  two-phase/deferred execution и generated-key propagation отложены. Декларация
  такой неподдержанной стратегии возвращает отдельный blocker, не обещание её исполнения.

### 5. Confidence, fingerprints и schema drift

- Задать конечный Decimal threshold в trusted `MappingValidationPolicy`, default
  `0.90` включительно, согласованный с auto threshold M9/M10. Проверить plan и
  каждое выбранное mapping; высокий общий score не скрывает слабое поле.
  Ниже threshold — `REJECTED / MAPPING_CONFIDENCE_BELOW_THRESHOLD` (§14.1);
  равенство проходит. Не вводить автоматическое округление или averaging.
- `NEEDS_REVIEW` использовать для недоказанной совместимости/неоднозначности,
  а не для обхода низкого threshold. Confirmation не снимает hard blockers.
  Composition root передаёт SDK confidence или явно заданный manual plan;
  self-reported LLM confidence не становится SDK evidence. Валидатор проверяет
  численные условия и bindings, но не удостоверяет автора по `producer`/hash.
- Повторно проверить plan content hash и полную source → extraction → ParsePlan →
  normalized lineage; пересчитать versioned manifest hash. Optional profile
  должен иметь согласованные собственный, manifest и content fingerprints.
- Пересчитать `database_fingerprint(catalog)` и сравнить как с заявленным catalog
  hash, так и с plan hash. Подмена catalog hash — invalid snapshot, реальное
  несовпадение с сохранённым планом — `DATABASE_SCHEMA_DRIFT`. Не обновлять план
  молча, не пересоздавать отсутствующие объекты, не remap IDs по именам.
- Дополнительно сверить target ID, inspection-policy binding, раскрытую validation
  policy/scope и writable projection. Одинаковые schema hash при разных targets,
  opaque IDs, grants или policy не делают планы взаимозаменяемыми.
- Перед load composition root обязан получить новый scoped read-only inspection
  отдельным inspector, повторить validation и передать checked plan writer с
  `LoadContext`. Проверка snapshot не закрывает TOCTOU: повторная проверка перед
  SQL, разрешённые identifiers, отдельный writer, staging/transaction/rollback
  остаются обязательствами будущего loader. `dry_run` проходит те же plan gates.

### 6. Forbidden operations и SQL fragments

- Только `insert_only`/`upsert`, с дополнительным сужением trusted policy.
  `append`, `update_only`, `merge`, DELETE, CREATE/ALTER/DROP/TRUNCATE/GRANT/REVOKE,
  отключение constraints и требование DDL отклоняются. Ни один флаг плана
  не подключает административный API или migration principal.
- Закрытая wire schema отклоняет неизвестные поля (`sql`, `query`, `expression`,
  `script`, `on_conflict_sql` и подобные); identifiers являются exact catalog refs.
  SQL fragments в управляющих полях дают безопасный code без возврата payload.
  Не строить безопасность на keyword regex и не исполнять текст для его проверки.
- Quoted catalog identifiers с пробелами или SQL keyword допустимы как точные
  metadata refs разрешённых объектов; comments/defaults/CHECK expressions —
  инертная metadata. Не сканировать весь catalog на слова `SELECT`/`DROP` и
  не путать quoted identifier с исполняемой командой. SQL формирует только adapter.

## All-issues report и детерминизм

### Intake и полнота

Основной `validate` принимает строгий MappingPlan и отдельные snapshots. Метод
wire intake принимает ограниченный JSON payload и использует общий accumulator:
неизвестные поля, операция, дубли и shape errors не превращаются в raw traceback.
Для payload, из которого нельзя создать MappingPlan, возвращается
`MappingPlanInputReport` с decision `REJECTED`, `complete`, codes/locations и
digest payload; обязательные fingerprints обычного result не выдумываются.
После успешного intake все semantic issues возвращает существующий
`MappingPlanValidationResult`. В обоих случаях wrapper создаётся только после
успеха всех обязательных правил и финальной строгой DTO-проверки.

Для структурно читаемых входов собрать все применимые независимые issues,
включая несколько ошибок одного класса в разных местах. Проверка с отсутствующей
предпосылкой пропускается с учётом статуса этой предпосылки; например, после
`MAPPING_TABLE_NOT_FOUND` не сообщать ложный `MAPPING_COLUMN_NOT_WRITABLE`.
Непарсируемый JSON, превышение budgets и некорректный trusted snapshot не позволяют
обещать полный semantic report: вернуть явно неполный intake report либо typed
`ValidationError` с безопасным code, без частичного checked result. Отмена
пробрасывается как `CancelledError`; silent truncation запрещён.

### Порядок фаз и issues

Фиксировать versioned registry правил в следующем порядке:

1. Resource preflight; закрытая форма/version; forbidden fields/operations.
2. Integrity snapshots, content hashes, target/policy/lineage и schema drift.
3. Source/target existence и scope; затем writability.
4. Конфликты mappings и покрытие required targets.
5. Type compatibility.
6. Identity/upsert.
7. FK/relation resolution и selected dependency graph.
8. Confidence; итоговые decision и evidence.

Ошибки предыдущей фазы не останавливают независимые правила последующих фаз.
Недоверенный/невалидный snapshot закрывает только зависимые от него проверки.
Сортировка: `(phase_rank, rule_rank, canonical_location, code)`; все location
segments типизированы и имеют фиксированный tag для сравнения. Location содержит
только закрытое имя секции и индекс элемента, без свободного текста или source
values. Не удалять одинаковый code у разных mappings; точный дубль
`(rule, location, code)` выводить один раз.

Порядок mappings в сохранённом плане уже участвует в его content fingerprint:
не обещать равенство hashes после перестановки mappings. Для неизменённого плана
перестановка unordered catalog/policy collections сохраняет decision/issues/hash.
Ordered composite key pairs и enum labels не сортировать как множества.

`validation_fingerprint` вычислять по versioned canonical projection: версия
validator/rules, plan/manifest/profile bindings, пересчитанный DB hash, target,
inspection/validation policy и scope hashes, writable projection, decision,
ordered issues/locations и selected load order. `validated_at` — UTC и исключён
из hash. Для приёмки result/wrapper evidence должен совпадать полностью.
Hash не является подписью или capability; повторная проверка обязательна.

### Каталог machine-readable codes

`ValidationIssue` уже содержит `code`, `severity`, `message_key`; physical
`source_refs` в mapping diagnostics запрещены. Locations хранить типизированно,
соответствие с `issues` проверять в result и wrapper. Все сообщения локализуются
по `message_key`; payload, DSN, имена скрытых объектов и SQL в diagnostics не включать.

| Группа | Основные codes |
|---|---|
| Intake / budgets | `MAPPING_PLAN_INVALID`, `MAPPING_PLAN_VERSION_UNSUPPORTED`, `MAPPING_LIMIT_EXCEEDED`, `MAPPING_SQL_FORBIDDEN`, `MAPPING_OPERATION_FORBIDDEN`, `MAPPING_DDL_FORBIDDEN` |
| Integrity / binding | `MAPPING_PLAN_FINGERPRINT_MISMATCH`, `MAPPING_SOURCE_LINEAGE_MISMATCH`, `MAPPING_TARGET_MISMATCH`, `MAPPING_POLICY_MISMATCH`, `MAPPING_CATALOG_FINGERPRINT_MISMATCH`, `DATABASE_METADATA_UNSUPPORTED`, `DATABASE_SCHEMA_DRIFT` |
| Existence / policy | `MAPPING_SOURCE_NOT_FOUND`, `MAPPING_TABLE_NOT_FOUND`, `MAPPING_COLUMN_NOT_FOUND`, `MAPPING_SCHEMA_DENIED`, `MAPPING_TABLE_DENIED`, `MAPPING_COLUMN_DENIED`, `MAPPING_SYSTEM_OBJECT_FORBIDDEN`, `MAPPING_SCOPE_INVALID` |
| Writability / coverage | `MAPPING_TABLE_NOT_WRITABLE`, `MAPPING_COLUMN_NOT_WRITABLE`, `MAPPING_GENERATED_COLUMN`, `MAPPING_REQUIRED_TARGET_MISSING`, `MAPPING_SOURCE_CONFLICT`, `MAPPING_TARGET_AMBIGUOUS` |
| Types | `MAPPING_TYPE_INCOMPATIBLE`, `MAPPING_TYPE_UNVERIFIED`, `MAPPING_TRANSFORMATION_REQUIRED`, `MAPPING_NULLABILITY_CONFLICT`, `MAPPING_LENGTH_OVERFLOW`, `MAPPING_NUMERIC_OVERFLOW` |
| Identity | `MAPPING_IDENTITY_REQUIRED`, `MAPPING_IDENTITY_AMBIGUOUS`, `MAPPING_UPSERT_KEY_INVALID`, `MAPPING_IDENTITY_NULLABLE`, `MAPPING_SOURCE_IDENTITY_FORBIDDEN` |
| Relations | `MAPPING_FK_NOT_FOUND`, `MAPPING_FK_PAIR_MISMATCH`, `MAPPING_RELATION_UNRESOLVED`, `MAPPING_RELATION_STRATEGY_UNSUPPORTED`, `CYCLIC_DEPENDENCY_REQUIRES_STRATEGY` |
| Confidence | `MAPPING_CONFIDENCE_BELOW_THRESHOLD` |

Каждый code получает правило, severity и test vector. Hard нарушения — ERROR;
недоказанная совместимость/неоднозначность — WARNING с обязательным
`NEEDS_REVIEW`. Агрегация: любой ERROR/CRITICAL → REJECTED; иначе review blocker →
NEEDS_REVIEW; иначе ACCEPTED. Обычные INFO/WARNING без blocker могут сопровождать
acceptance; флаг blocker определяется закрытым registry, а не caller или LLM.

## Шаги

Для краткости: `S` — `packages/structuraguard/src/structuraguard`,
`T` — `packages/structuraguard/tests`. В командах ниже это обозначения путей,
которые нужно раскрыть, а не существующие переменные окружения.

| Шаг | Файлы и вертикальный результат | Тест, затем команда проверки |
|---|---|---|
| 1. Контракты и успешный минимальный план | `S/contracts/mapping_validation.py`, `S/contracts/mapping.py`, exports, `S/ports/mapping.py`, `S/mapping/validation.py`, `docs/adr/0021-mapping-plan-validation.md`. Legacy 1.0.0 round-trip и простой insert 1.1.0, policy/evidence binding. | Новый `T/unit/contracts/test_m11_validation_contracts.py`; прежние `test_m02_invariants.py`, `test_m02_review_regressions_streams.py`, `test_m02_security_regressions.py`. `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/contracts`. |
| 2. Полный отчёт, policy и drift | `S/mapping/_validation_input.py`, `_validation_report.py`, `_validation_scope.py`, `validation.py`; общий accumulator, intake, limits, exact refs, writable, обязательные targets и пересчёт fingerprints. | Новые `T/unit/mapping/test_m11_validation.py`, `test_m11_scope.py`, `test_m11_drift.py`: одновременно несколько независимых нарушений, stale hash, смена policy/writable при том же DB hash. `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/mapping -k m11`. |
| 3. Types и keys | `S/mapping/_validation_types.py`, `_validation_identity.py`, versioned descriptors. Консервативная type matrix и доказанные composite upsert keys без изменения M9 scores. | Новые `T/unit/mapping/test_m11_types.py`, `test_m11_identity.py`, прежний `test_compatibility.py`: boundary numeric/nullable/default, partial unique, natural key без constraint, upsert insert branch. `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/mapping`. |
| 4. Relations, confidence и evidence | `S/mapping/_validation_relations.py`, `validation.py`, при необходимости pure helper в `S/domain/database_graph.py`. Selected DAG, полный FK, cycle refusal, threshold и согласованный accepted wrapper. | Новые `T/unit/mapping/test_m11_relations.py`, `test_m11_confidence.py`, `T/contract/mapping/test_m11_validation_port.py`: self-cycle, composite FK, unrelated cycle, threshold − epsilon / equality / + epsilon. `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/mapping packages/structuraguard/tests/contract/mapping`. |
| 5. Property/security и настоящая metadata | Новые `T/property/mapping/test_m11_validation_properties.py`, `T/security/mapping/test_m11_validation_security.py`, `T/integration/test_mapping_plan_validation.py`, `T/integration/database/test_postgresql_mapping_validation.py`; расширить `T/smoke/test_mapping_boundaries.py`. | Команды: `uv run --locked --no-sync pytest packages/structuraguard/tests/property/mapping packages/structuraguard/tests/security/mapping packages/structuraguard/tests/smoke/test_mapping_boundaries.py`; `make test-integration`; `make test-database`. SQLite read-only inspection и PG fixtures дают реальные type/key/identity/FK metadata; подготовка fixture DDL остаётся в test setup. |
| 6. Документация и приёмка | `docs/mapping-plan-validation.md`, `docs/public-api.md`, `mkdocs.yml`, итоговый M11 acceptance report. Описать API, codes, wire migration, all-issues boundary и обязательства loader. Facade остаётся явно не реализованным до отдельного orchestration milestone. | Review diff через `structuraguard-review` и trust boundary через `structuraguard-security`; полный набор ниже. Обновить docs только по подтверждённой реализации. |

### Property/security matrix

- **Полнота и стабильность:** генерировать независимые нарушения и сравнивать с
  независимым ожидаемым набором `(code, location)`; повторять на разных hash seeds
  и перестановках catalog/policy collections. Проверять dependency-aware suppression
  без потери ошибок других mappings, одинаковые codes в разных locations.
- **Монотонность:** сужение allow, расширение deny, рост threshold или снятие
  writability никогда не превращают отказ в acceptance. Добавление hard blocker
  всегда убирает wrapper; выбор `best_effort`/`dry_run` не снимает plan veto.
- **Integrity:** изменение plan/manifest, types, PK/unique/FK/default/identity
  инвалидирует прежнее evidence; изменение target/policy/writable ловится и при
  неизменном schema hash. Поддельные `model_copy`/`model_construct`, legacy catalog,
  stale wrapper и совпадающий schema hash с другими opaque IDs не обходят проверки.
- **Ключи и графы:** генерировать composite keys, неполные/permuted FK pairs,
  self-cycles, DAG и отдельные циклы; сверять parent-before-child порядок и отказ
  при недоказанной uniqueness. Статистика профиля и LLM hints не меняют hard outcome.
- **SQL/PII:** unknown SQL fields, DDL/DML, comments/semicolon/quotes/Unicode,
  malicious identifiers и metadata instructions; отдельно разрешённые quoted
  names и инертные CHECK/default expressions. Проверять отсутствие payload/secrets
  в repr, exceptions, reports и logs, отсутствие DB/LLM/network/file calls.
- **Budgets:** JSON bytes/depth, число mappings/tables/columns/edges/issues,
  metadata length, Decimal digits/exponent, work/result limits; лимит проверяется
  до dump/hash и больших allocations. При исчерпании нет fake complete report,
  acceptance или silent truncation. Async cancellation не подавляется.
- **Dialect integration:** SQLite rowid/INTEGER, PostgreSQL ALWAYS/BY DEFAULT,
  numeric scale/timezone, enum/domain/array, composite/partial unique, deferrable FK
  и case-sensitive identifiers. Drift тестировать fresh inspection после fixture
  migration; DML/DDL во время самого validate запрещён spy-проверками.

Полные quality gates перед завершением **реализации milestone**:
`make lint`, `make typecheck`, `make test`, `make test-integration`,
`make test-security`, `make test-database`, `make docs`.
Для `make test-database` нужен рабочий PostgreSQL/Testcontainers environment;
skip не считать проверкой dialect behavior. Исключение для этапа исходного
планирования (только review документа, whitespace и docs build) к завершённой
реализации M11 не применяется: полные результаты записаны в отчёте приёмки.

## Риски

- **Ошибочное принятие upsert:** потеря данных при nullable/неполном/неподтверждённом
  key. Устраняется отказом до wrapper; проверка конфликтов реальных строк остаётся
  транзакционному loader.
- **Schema drift/TOCTOU:** hash готового snapshot не гарантирует состояние живой
  БД или grants. Требуются свежий inspection и проверки непосредственно перед SQL.
- **Совместимость:** новые descriptors/evidence не должны менять legacy hashes
  или молча ослаблять DTO. Wire snapshots и regression tests обязательны.
- **Неполная metadata/evidence:** строгий MVP чаще возвращает NEEDS_REVIEW/REJECTED
  для unknown types, legacy catalogs и сложных relations. Это явное ограничение,
  которое нельзя обходить высоким confidence или manual flag.
- **Resource exhaustion и утечки:** полный отчёт ограничивается budgets; locations
  и errors не должны раскрывать недоверенные identifiers, source values и SQL.

За обязательным scope остаются transformation/business-rule engine, live grants
и parent-row lookup, generated-key propagation, cycle execution, staging/loader,
rollback/idempotency runtime, auto plan compiler из M10 и SDK orchestration.
Новые production dependencies и миграции пользовательской БД не требуются.
