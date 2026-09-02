# План M01 «Каркас Python-пакета»

Статус: локально выполнен и готов к ручному commit 2 сентября 2026
года. Commit не создавался; remote GitHub Actions до push/PR не запускался.

## Цель

Создать один устанавливаемый distribution `structuraguard` для Python 3.12+,
который собирается в wheel и sdist, безопасно импортируется, предоставляет
минимальные типизированные config/error/facade contracts и проходит единые
локальные и CI quality gates без реализации pipeline, parsers, БД или LLM.

## Основание и границы

План опирается на текущую задачу, `AGENTS.md`,
`docs/codex/PROJECT_CONTEXT.md`, строку «Каркас SDK» в
`docs/codex/SPEC_INDEX.md` и извлечённые скриптом
`scripts/extract_spec_sections.py` разделы ТЗ: `0`, `1`, `2`, `3`, `21`, `22`,
`23`, `27`, `M1` и `33`.

Для M1 обязательны доказательства только относящихся к нему критериев:

- `AC-01` — wheel устанавливается в чистое окружение;
- `AC-02` — импорт не создаёт запрещённых побочных эффектов;
- `AC-03` — core package не зависит от web frameworks.

В scope входят packaging, минимальный public scaffold, инструменты качества,
документационный builder и CI. В scope не входят:

- бизнес-логика операций `inspect_source`, `inspect_database`,
  `create_plan`, `validate_plan`, `execute`, `analyze`, `ingest` и
  `propose_schema`;
- domain DTO, ports, registries, pipeline и реальные extension contracts;
- parser, database, staging, loader, validation, security и LLM adapters;
- CLI, demo applications, migrations, network/DB integration tests и
  публикация package в registry.

Имена операций из `docs/public-api.md` доступны в scaffold, но не
получают фиктивную реализацию или ложный успешный результат.
До появления vertical slice они явно завершаются
`OperationNotImplementedError`; предметные типы вводятся вместе с
contracts в последующих milestone.

## Наблюдаемое состояние

### До M1

- ветка `feat/m01-sdk-scaffold` совпадает с `main`, рабочее дерево чистое;
- отсутствуют `pyproject.toml`, `uv.lock`, `Makefile`, package source и tests;
- отсутствуют `SDKConfig`, `AsyncStructuraGuard`, `StructuraGuard`, public
  exceptions и top-level exports;
- отсутствуют `.github/workflows/`, MkDocs configuration и `docs/index.md`;
- существующая `.venv` не является частью поставки и содержит только `pip`;
- `MANIFEST.sha256` относится к Codex-набору и не является build manifest;
- `docs/requirements.md`, `docs/architecture.md`, `docs/public-api.md`,
  `docs/threat-model.md` и ADR уже задают design M0; создавать параллельные
  документы или альтернативный API не требуется;
- `.gitignore` исключает только `.venv/`, а `.DS_Store` и Python bytecode уже
  попали под version control.

### После M1

- корень репозитория является `uv` workspace, а единственный package находится
  в `packages/structuraguard/src/structuraguard` согласно разделу 27 ТЗ;
- Hatchling воспроизводимо создаёт wheel и самодостаточный sdist;
- базовая установка тянет только реально используемый runtime dependency
  Pydantic v2, тяжёлые feature dependencies изолированы в extras;
- `import structuraguard` объявляет ленивые top-level exports для config,
  двух facade и public exception hierarchy, но не загружает Pydantic,
  не выполняет I/O и не создаёт runtime resources;
- все developer/docs dependencies отделены от metadata поставляемого package;
- Ruff, mypy strict, pytest с AnyIO pytest plugin, MkDocs, Makefile и CI
  используют одни и те же команды;
- M1 имеет автоматические evidence для `AC-01`–`AC-03`.

## Критерии приёмки

1. `uv build --package structuraguard` создаёт ровно один wheel и один sdist;
   wheel также успешно собирается из созданного sdist.
2. Wheel устанавливается не-editable способом в новое временное virtualenv.
   Из изолированного interpreter доступны package metadata и все утверждённые
   top-level exports.
3. Wheel содержит только ожидаемые modules и `py.typed`; sdist содержит всё,
   что нужно для повторной сборки, но не caches, `.DS_Store`, tests artifacts,
   secrets или repository-only configuration. Единственное исключение —
   обязательный для Hatchling sdist `/.gitignore`, который должен byte-for-byte
   совпадать с reviewed root file.
4. Unconditional runtime dependencies ограничены Pydantic v2. FastAPI, Django,
   Flask, Celery, Redis и другие framework dependencies отсутствуют в metadata
   и не импортируются.
5. `SDKConfig()` создаётся только из явно переданных значений, immutable и
   отклоняет неизвестные поля. Обычный `import structuraguard` и
   construction не читают environment через код StructuraGuard; при первом
   явном доступе к `SDKConfig` отдельно инициализируется Pydantic. M1 не
   вводит `BaseSettings` или speculative operational fields.
6. `AsyncStructuraGuard` и отдельный composition-based `StructuraGuard`
   создаются с instance-local `SDKConfig`; import и constructors не создают
   event loop, thread или global mutable registry.
7. Public exception hierarchy соответствует NFR-012 и имеет typed поля `error_code`,
   `message`, safe immutable `details`, `run_id`, `retryable` и уже
   санитизированный `cause`. Дублирующий alias `code` не вводится.
8. Import smoke в отдельном subprocess обнаруживает попытки network/process
   access, запуска thread/event loop, изменения signal handlers/root logger и
   любое чтение environment package code. Проверка запускается и для source
   checkout, и для установленного wheel.
9. `make lint`, `make typecheck`, `make test`, `make docs` и
   `make test-build` завершаются успешно из корня репозитория.
10. CI использует lockfile и read-only permissions: quality, docs и package
    gates выполняются на Python 3.12, а pytest — в матрице Python 3.12–3.14.
    Workflow не получает write permissions или секреты.

### Итоговый checklist

- [x] 1. Scoped build создаёт один wheel и один sdist; wheel повторно
  собирается из sdist. Доказательство: `make test-build` и
  `scripts/verify_distribution.py`.
- [x] 2. Wheel устанавливается non-editable в новое temporary virtualenv;
  metadata и top-level exports проверены installed-wheel smoke.
- [x] 3. Contents wheel/sdist, `py.typed`, self-contained rebuild и запрет
  repository/generated files проверены distribution verifier tests.
- [x] 4. Runtime dependency allowlist ограничен Pydantic v2; metadata, source
  imports и отсутствие web frameworks проверены
  `test_metadata.py` и `test_dependency_boundary.py`.
- [x] 5. Explicit immutable `SDKConfig`, запрет extra fields и отсутствие
  environment reads покрыты config unit tests и import probes.
- [x] 6. Async-first facade, отдельная composition-based sync-оболочка,
  instance-local config и event-loop contract покрыты facade/operation tests.
- [x] 7. Typed exception hierarchy, stable `error_code`, immutable bounded details,
  redaction и traceback semantics покрыты `test_exceptions.py`.
- [x] 8. Source и installed-wheel subprocess probes проверяют environment,
  files, network/process/thread/event-loop, signals и logging side effects.
- [x] 9. Root targets `lint`, `typecheck`, `test`, `docs` и `test-build` прошли
  в составе `make check`.
- [x] 10. CI contract для pinned actions, lockfile, Python 3.12–3.14 и read-only
  permissions покрыт `test_ci_contract.py`; remote execution остаётся
  post-commit/post-push проверкой.

## Затронутые контракты

### Public Python API

Top-level module `structuraguard` экспортирует только утверждённый scaffold:

- `SDKConfig`;
- `AsyncStructuraGuard`;
- `StructuraGuard`;
- `StructuraGuardError`, `SourceError`, `ParserError`,
  `DatabaseInspectionError`, `MappingError`, `ValidationError`,
  `SecurityPolicyError`, `LoadError`, `OperationNotImplementedError`.

`SDKConfig` — Pydantic v2 model с `frozen=True` и `extra="forbid"`, но без
`BaseSettings`, implicit env sources и operational fields, смысл которых ещё не
зафиксирован. Обе facade принимают config keyword-only;
отсутствие argument создаёт
новый config на instance, а не разделяемый mutable default.

В M1 facade являются composition roots с именами целевых
pipeline methods и typed failure вместо бизнес-логики. Sync facade не
наследуется от async facade, не создаёт loop при import/construction,
проверяет активный loop и только при явном вызове вне него запускает
async operation через `asyncio.run`. В активном loop первичен
`SYNC_API_IN_ASYNC_CONTEXT`; вне loop операция даёт
`SDK_OPERATION_NOT_IMPLEMENTED`.

Base exception принимает только безопасные JSON-compatible details и
санитизированное текстовое описание cause. Он не преобразует произвольное
исключение в строку автоматически, чтобы не копировать secrets. Категорийные
subclasses не меняют поля или семантику base contract.

### Packaging contract

- distribution/import name: `structuraguard`;
- initial version: `0.1.0`, static source of truth в package metadata;
- `requires-python = ">=3.12"` и SemVer;
- build backend: Hatchling;
- toolchain: `uv==0.9.27`, build использует синхронизированный из
  `uv.lock` Hatchling без повторного online resolution;
- typed distribution marker: `py.typed`;
- package discovery ограничен `src/structuraguard` внутри package project.

Feature extras повторяют публичные имена раздела 22.1 и не попадают в base
install:

| Extra | Допустимые dependencies M1 |
| --- | --- |
| `postgres` | SQLAlchemy 2.x, `asyncpg`, `psycopg` |
| `pdf` | PyMuPDF |
| `excel` | `openpyxl` |
| `office` | `python-docx` |
| `litellm` | LiteLLM |
| `tika` | Apache Tika Python client |
| `all` | точное дедуплицированное объединение остальных extras |

Перед фиксацией version bounds каждый package проверяется на поддержку Python
3.12+, активную поддержку и совместимую лицензию. Наличие extra в M1 означает
только dependency bundle, а не наличие adapter. `jsonschema`, `orjson`, HTTPX,
Polars, format detectors/parsers, Typer, retry и DB/LLM libraries вне указанных
extras не добавляются, пока их не использует production code.

Ruff, mypy, pytest, `anyio` со встроенным pytest plugin, MkDocs Material и
вспомогательные build tools находятся в root dependency groups `dev`/`docs`, а
не в package extras или wheel metadata. Отдельный placeholder distribution
`pytest-anyio` не добавляется; требование pytest-anyio реализуется upstream
plugin из `anyio`.

### Persistence и wire formats

M1 не создаёт schema, migrations, storage, SQL, network protocol или serialized
domain format. Data migration и backward compatibility для persisted data не
требуются.

## Трассировка NFR-001–NFR-012

| NFR | Решение M1 / граница последующего milestone |
| --- | --- |
| `NFR-001` | subprocess import probe и wheel smoke фиксируют отсутствие side effects |
| `NFR-002` | typed exports, Pydantic config, `py.typed`, mypy strict |
| `NFR-003` | async facade основная; sync facade композиционно делегирует в неё |
| `NFR-004` | не создаются фиктивные adapters/registries; ports появятся с contracts |
| `NFR-005` | config instance-local, mutable module singletons отсутствуют |
| `NFR-006` | streaming не входит в M1; scaffold не фиксирует batch API раньше M2/M4 |
| `NFR-007` | package version фиксируется; run metadata отложены до domain contracts |
| `NFR-008` | Python 3.12+, SemVer metadata и совместимые public additions |
| `NFR-009` | events не эмулируются; event contract реализуется вместе с pipeline |
| `NFR-010` | нет длительных operations/resources; timeout/cancellation не имитируются |
| `NFR-011` | performance pipeline отсутствует; тяжёлые imports не входят в core |
| `NFR-012` | public typed exception hierarchy и стабильное canonical поле `error_code` |

## Решения и trade-offs

### Один workspace package

Варианты: корневой `src/structuraguard` либо структура
`packages/structuraguard/src/structuraguard`. Выбран второй вариант: он прямо
следует разделу 27, оставляет root для общих quality/docs/CI settings и не
потребует переноса SDK при появлении demo packages. Root `pyproject.toml` не
является вторым distribution; buildable project ровно один.

Поскольку PEP 517 frontend без явного backend может применить legacy
Setuptools fallback и собрать `UNKNOWN`, virtual root использует dependency-free
no-build guard. Он отклоняет root sdist, wheel и editable build; разрешённой
целью остаётся только workspace member `structuraguard`.

Это уточняет оставленный M0 package-layout вопрос, но не меняет принятую
архитектуру, поэтому отдельный ADR не нужен. Точный layout добавляется в
`docs/architecture.md` в составе реализации M1.

### Facade без ложного поведения

Варианты: добавить методы, возвращающие placeholders; поднимать generic
`NotImplementedError`; публиковать только constructible facade; либо
зафиксировать имена операций с общими `object` inputs и возвратом `Never`.
Выбран последний вариант: он выполняет текущий критерий явного
typed failure, не выдаёт generic error в обход NFR-012 и не закрепляет
преждевременные DTO signatures. Предметные signatures добавляются
вертикально с DTO и tests.

### Явная конфигурация

`SDKConfig` основан на `BaseModel`, а не `BaseSettings`. Это оставляет
composition owner ответственным за environment/secrets и делает import
детерминированным. Empty immutable model в M1 лучше speculative timeout, batch,
DB или LLM fields; добавление optional fields позднее совместимо.

### Extras без раздувания core

Имена extras фиксируются сейчас как packaging contract из ТЗ, но dependencies
остаются opt-in и не импортируются scaffold. Dev/docs tools используют отдельные
workspace groups. `all` проверяется как union автоматически, чтобы группы не
расходились при дальнейших изменениях.

## Шаги реализации

### 1. Зафиксировать workspace и build metadata

Файлы:

- `pyproject.toml` — `uv` workspace, dependency groups, общие tool settings и
  явный no-build guard для virtual root;
- `scripts/workspace_build_guard.py` — dependency-free отказ PEP 517/PEP 660
  hooks при попытке собрать корень;
- `packages/structuraguard/pyproject.toml` — PEP 621 metadata, Hatchling,
  runtime dependency и extras;
- `packages/structuraguard/src/structuraguard/__init__.py` и `py.typed` —
  минимальный importable package;
- `.python-version`, `uv.lock`, `.gitignore`;
- `packages/structuraguard/tests/packaging/test_metadata.py` — metadata
  allowlists, extras и Python/version assertions.
- `packages/structuraguard/tests/packaging/test_workspace.py` — executable
  regression, подтверждающий отказ generic root build без distribution artifacts.

Сначала добавить failing metadata test, затем build files и минимальный package.
Обновить `.gitignore` для Python/build/docs caches и удалить из version control
уже отслеживаемые `.DS_Store`/`__pycache__` artifacts. `MANIFEST.sha256` не
переименовывать и не использовать в package build.

Проверка:

```bash
uv lock
uv lock --check
uv sync --all-packages --locked --group dev --group docs
uv run pytest packages/structuraguard/tests/packaging/test_metadata.py
```

### 2. Ввести минимальные config и facade contracts

Файлы:

- `packages/structuraguard/tests/unit/test_config.py`;
- `packages/structuraguard/tests/unit/test_facades.py`;
- `packages/structuraguard/tests/unit/test_public_exports.py`;
- `packages/structuraguard/src/structuraguard/config.py`;
- `packages/structuraguard/src/structuraguard/sdk.py`;
- `packages/structuraguard/src/structuraguard/sync_sdk.py`;
- `packages/structuraguard/src/structuraguard/__init__.py`.

Тесты фиксируют explicit/frozen config, запрет extra fields, отдельные классы,
instance-local defaults, стабильный `__all__` и отсутствие loop/thread creation
при construction. Один test с `@pytest.mark.anyio` создаёт async facade внутри
существующего asyncio context и подтверждает, что running loop не заменяется.
Добавить constructors/read-only config properties и scaffold operations,
которые всегда дают typed failure; registries и business logic не добавлять.

Проверка:

```bash
uv run pytest packages/structuraguard/tests/unit/test_config.py packages/structuraguard/tests/unit/test_facades.py packages/structuraguard/tests/unit/test_public_exports.py
uv run mypy packages/structuraguard/src packages/structuraguard/tests/unit
```

### 3. Ввести public typed exception hierarchy

Файлы:

- `packages/structuraguard/tests/unit/test_exceptions.py`;
- `packages/structuraguard/src/structuraguard/exceptions.py`;
- `packages/structuraguard/src/structuraguard/__init__.py`.

Сначала проверить inheritance, canonical `error_code`, `str(error)`, safe details,
optional `run_id`, `retryable`, sanitized cause и защиту от последующей мутации
переданного mapping. Затем реализовать один base class и категорийные subclasses
без adapter-specific codes. Не добавлять broad `Any`, автоматическое чтение
exception traceback/locals или логирование.

Проверка:

```bash
uv run pytest packages/structuraguard/tests/unit/test_exceptions.py
uv run mypy packages/structuraguard/src/structuraguard/exceptions.py packages/structuraguard/tests/unit/test_exceptions.py
```

### 4. Добавить smoke evidence для import и distribution

Файлы:

- `packages/structuraguard/tests/smoke/test_import_side_effects.py`;
- `packages/structuraguard/tests/smoke/test_dependency_boundary.py`;
- `scripts/verify_distribution.py`.

Import test использует два независимых subprocess-режима:

1. black-box probe ставит audit/monkeypatch guards и снимает snapshots до
   первого импорта `structuraguard`; он проверяет весь фактический
   import graph и не допускает eager import Pydantic; probe обязательно
   запускается и для установленного wheel;
2. attribution probe предварительно загружает разрешённые dependencies, затем
   запрещает environment access, удаляет только `structuraguard.*` из
   `sys.modules`, повторяет import и разрешает все lazy exports, чтобы
   локализовать side effect в package code, а не в dependency.

В обоих режимах guards покрывают environment,
network/process/thread/event-loop/signal/logging boundaries. `os.getenv`,
`os.getenvb`, `os.environ` и `os.environb` завершают probe с ошибкой, даже
если переменная
optional. Разрешается только file access самого import machinery; любое
открытие config/data path считается failure. Snapshot root logger, signal
handlers, active threads и loop state до/после должен совпасть.

Dependency test проверяет installed metadata и `sys.modules`: единственный
unconditional runtime dependency разрешён явно, extras имеют markers, web
frameworks отсутствуют. AST-проверка package source запрещает import
`pydantic_settings`/`BaseSettings` и обращения к `os.getenv`/`os.environ` в
importable scaffold. Проверка не полагается только на отсутствие доступной сети
или БД.

Distribution verifier на stdlib:

1. проверяет количество, имена и contents wheel/sdist;
2. собирает wheel повторно из sdist;
3. создаёт временное virtualenv вне repository;
4. устанавливает wheel не-editable способом;
5. запускает `python -I` import/dependency probe;
6. всегда очищает temporary directory.

Проверка:

```bash
uv run pytest packages/structuraguard/tests/smoke
uv build --offline --no-python-downloads --package structuraguard --out-dir dist --clear --no-create-gitignore --no-build-isolation
uv run python scripts/verify_distribution.py dist
```

### 5. Подключить единые local quality gates

Файлы:

- `pyproject.toml` — Ruff, mypy strict, pytest и AnyIO plugin configuration;
- `Makefile` — `sync`, `format`, `lint`, `typecheck`, `test`, `docs`, `build`,
  `test-build`, `check`.

Ruff проверяет format, imports и correctness для package, tests и M1 scripts.
Mypy не исключает public modules и tests из strict profile. Pytest использует
`--strict-config`, `--strict-markers`, явный test path и fixture
`anyio_backend = "asyncio"`, чтобы встроенный AnyIO plugin не требовал Trio.
Make targets являются тонкими обёртками над `uv run` и не
устанавливают/обновляют dependencies неявно; только явно вызванный `make sync`
выполняет `uv lock --check` и
`uv sync --all-packages --locked`.

Пустые `test-integration` и `test-security`, выдающие ложный зелёный результат,
не создаются; targets появятся вместе с реальными suites.

Проверка:

```bash
make lint
make typecheck
make test
make test-build
```

### 6. Подключить существующую документацию к MkDocs

Файлы:

- `mkdocs.yml` — Material theme, строгий nav и repository links без plugins,
  которые пока не используются;
- `docs/index.md` — краткая landing page со статусом milestone и ссылками;
- `docs/requirements.md` — заменить выходящую за `docs_dir` relative-ссылку на
  полное ТЗ на абсолютную repository-ссылку, не копируя ТЗ в docs tree;
- `docs/architecture.md` — один canonical package-layout раздел;
- `docs/public-api.md` — availability note: M1 реализует только scaffold,
  operation examples остаются design последующих milestone.

В nav повторно использовать `requirements.md`, `architecture.md`,
`public-api.md`, `threat-model.md` и существующие ADR. Не создавать второй набор
API/security/architecture документов и не генерировать API reference до
появления содержательных operations.

Проверка:

```bash
uv run mkdocs build --strict --clean
make docs
```

### 7. Добавить CI с отдельным packaging gate

Файл: `.github/workflows/ci.yml`.

Workflow запускается для pull request и push. Actions фиксируются immutable
commit SHA, permissions ограничиваются `contents: read`, install выполняется из
`uv.lock`. Перед install `uv lock --check` отклоняет stale lockfile, затем
`uv sync --all-packages --locked` запрещает его неявное обновление.

Jobs:

1. `quality` на Python 3.12 — `make lint`, `make typecheck`, `make docs`;
2. `tests` на матрице Python 3.12, 3.13 и 3.14 — `make test`;
3. `package` на Python 3.12 — `make test-build`, включая чистую установку wheel
   и rebuild из sdist.

Jobs не получают publish token, deployment environment или write permission.
Cache включает lockfile в key и не заменяет проверку актуальности lockfile.

Локальная проверка до push:

```bash
make check
git diff --check
```

### 8. Выполнить итоговый review M1

Проверить diff на:

- отсутствие parser/DB/LLM/domain placeholders и framework imports;
- отсутствие import-time I/O, environment reads и mutable singletons;
- совпадение `__all__`, metadata extras, MkDocs nav и documented availability;
- отсутствие secrets, generated `dist/`, `site/`, caches и editable-install
  зависимости тестов;
- self-contained sdist и импорт именно установленного wheel;
- неизменность принятых M0 API/security решений.

Фактически выполнено 2 сентября 2026 года:

```bash
make check
uv run --isolated --python 3.13 --locked pytest -q
uv run --isolated --python 3.14 --locked pytest -q
python3 -B scripts/validate_codex_pack.py
make docs
git diff --check
git status --short --ignored
```

`make check` завершён с `90 passed` на Python 3.12; Ruff, mypy strict,
MkDocs strict, wheel/sdist rebuild, isolated non-editable install и оба
installed-wheel import probe прошли. Изолированные прогоны на Python
3.13 и 3.14 завершились с `90 passed` каждый. Codex-pack validator вернул
`errors=0` и одно warning об отсутствии `.codex/config.toml`; repository-local
Codex config не входит в scope SDK scaffold, а validator считает warning
неблокирующим. После финальной правки plan повторная MkDocs
strict-сборка прошла. `git diff --check` и отдельный trailing-whitespace
scan untracked-файлов не нашли ошибок. После очистки generated-artifact
scan видит только игнорируемую `.venv/`; real secrets и debug artifacts не
обнаружены.

Осознанно не выполнены:

- remote GitHub Actions — до ручного commit и push/PR нет remote
  revision; workflow contract и Python matrix проверены локально;
- `make test-integration` и `make test-security` — targets намеренно не
  созданы: M1 не вводит parser/DB/LLM/runtime integration boundaries, а
  связанные с import/error security semantics regression tests входят в
  обычный pytest suite;
- registry publish — вне M1 и запрещён до утверждения лицензии и
  заполнения release metadata.

## Изменяемые и создаваемые файлы

- root tooling: `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`,
  `Makefile`;
- package: `packages/structuraguard/pyproject.toml`,
  `packages/structuraguard/src/structuraguard/{__init__,config,exceptions,sdk,sync_sdk}.py`,
  `packages/structuraguard/src/structuraguard/py.typed`;
- tests: `packages/structuraguard/tests/unit/`, `packaging/`, `smoke/`, `docs/`;
- build verification: `scripts/verify_distribution.py`,
  `scripts/workspace_build_guard.py`;
- docs: `mkdocs.yml`, `docs/index.md`, точечные изменения
  `docs/requirements.md`, `docs/architecture.md`, `docs/public-api.md`,
  `docs/threat-model.md`, `docs/codex/PROJECT_STATE.md` и этот plan-файл;
- CI: `.github/workflows/ci.yml`;
- Codex tooling: точечные Ruff-compatible изменения
  `scripts/extract_spec_sections.py` и `scripts/validate_codex_pack.py`;
- repository hygiene: удаление tracked `.DS_Store` и `__pycache__` artifacts.

## Риски

- Имена extras зафиксированы ТЗ, но adapter code появится позднее. Документация
  должна явно не обещать работоспособность feature только по факту установки
  extra; version bounds и лицензии требуют проверки перед lock.
- Предметные operation signatures зависят от DTO M2. В M1 зафиксированы
  только имена и `Never` outcome; замена общих inputs на DTO в
  последующем vertical slice должна быть оформлена как осознанное уточнение
  public contract.
- Subprocess guards дают сильный smoke signal, но не являются формальным
  доказательством отсутствия всех OS-level side effects; их дополняют review
  import graph и dependency allowlist.
- Репозиторий пока не содержит утверждённой лицензии. Это не блокирует локальную
  build/install проверку, но публикация distribution запрещена до выбора
  лицензии и заполнения package metadata.
- `requires-python = ">=3.12"` обещает совместимость с будущими Python. CI
  покрывает доступные 3.12–3.14; новый stable Python добавляется в matrix до
  следующего release.
