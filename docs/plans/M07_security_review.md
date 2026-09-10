# M07 — security review текущего diff

Дата: 2026-09-10. Область: незакоммиченные изменения M7 A/B/C и последующей
[проверки приёмки](M07_acceptance.md), включая новые untracked files.
Исходники прежних milestones вне затронутых contracts/import boundaries
не проверялись и не исправлялись.

Обнаружены и исправлены **две Medium-проблемы**. Подтверждённых Critical/High
нет. Это последующая проверка, уточняющая исторические результаты A/B/C в
[плане](M07_database_inspector.md).

## Границы доверия

Активы: неизменность БД, ограниченный catalog scope, конфиденциальность
credentials/metadata, корректность fingerprint/FK evidence, ресурсы процесса.
Недоверенные данные: request selectors, SQLite file/schema, PostgreSQL names,
types/defaults/expressions/comments и driver errors. Trusted owner задаёт
абсолютный SQLite path либо inspector DSN, allowlist/denylist и budgets.
Request не может заменить binding или расширить его. Graph — описание
зависимостей; он не разрешает запись и не подтверждает grants.

## R1 — Medium: исходная ошибка остаётся в exception context

**Места в исправленном дереве:**

- `packages/structuraguard/src/structuraguard/database/sqlite.py:175` — request validation;
- `packages/structuraguard/src/structuraguard/database/sqlite.py:288` — driver/metadata failure;
- `packages/structuraguard/src/structuraguard/database/postgresql.py:172` — request validation.

Номера строк актуализированы после дополнения публичных docstring; результаты
runtime-проверок ниже относятся к security review до docs-шага.

**Путь эксплуатации:** allowed SQLite view ссылается на отсутствующий объект,
чьё имя содержит sensitive metadata. SQLAlchemy включает имя в driver error.
Адаптер заменял его typed exception через `raise … from None`, но исходная
ошибка оставалась в `__context__`. Аналогично unchecked request с sensitive
selector оставлял вложенную validation chain. Система диагностики, собирающая
цепочку исключений независимо от `__suppress_context__`, получает raw values.

**Влияние:** утечка metadata/request secrets в error telemetry или downstream
audit. Обычный `str(error)` был безопасен; это не SQL injection и не обход scope.

**Минимальное исправление:** сохранять только безопасный code/признак отказа,
выйти из `except`, затем создать новый typed exception. SQLite connection
закрывается до публикации ошибки. Общие exception/contract modules не менялись.

**Regression tests:**
`packages/structuraguard/tests/security/database/test_m07_security_review.py`:
`test_sqlite_failure_drops_raw_exception_context` (оба inspection API) и
`test_rejected_request_has_no_validation_exception_chain` (оба API × обе БД).
Проверяются `__context__`, `__cause__`, canary в traceback/logs и сохранность файла.
Все **6** случаев упали до исправления и прошли после него.

## R2 — Medium: fingerprint скрывает значимую семантику схемы

**Места в исправленном дереве:**

- `packages/structuraguard/src/structuraguard/database/_sqlite_sql.py:142`;
- `packages/structuraguard/src/structuraguard/database/_postgresql_queries.py:40`;
- `packages/structuraguard/src/structuraguard/database/_postgresql_catalog.py:255`.

**Путь эксплуатации:** контролирующий schema actor меняет FK на
`DEFERRABLE INITIALLY DEFERRED`, добавляет `ON CONFLICT IGNORE` либо column
`COLLATE NOCASE` в SQLite. PRAGMA не переносит эти свойства в DTO.
Контрольный запуск получил одинаковый SHA-256 для исходной схемы и всех трёх
вариантов. PostgreSQL catalog query также не отражал `attcollation`, включая
collation от domain; неполный каталог успешно публиковался.

**Влияние:** проверка schema drift может подтвердить устаревшее представление
constraints/comparison/conflict semantics. Будущий consumer рискует использовать
неверные предпосылки валидации/загрузки. Сам M7 writer не содержит.

**Минимальное исправление:** явный `DATABASE_METADATA_UNSUPPORTED` до публикации
каталога для непредставимых SQLite clauses и PostgreSQL column/domain collation,
отличной от database default. Явные SQLite `NOT DEFERRABLE`, `ON CONFLICT ABORT`
и `COLLATE BINARY` также консервативно отклоняются. Index collation и collation
в сохранённых выражениях поддерживаются. Contracts и формат `catalog-v1`
не расширялись; необходимая граница поддержки отражена в документации API.

**Regression tests:**
`security/database/test_m07_security_review.py::test_unrepresented_sqlite_clauses_fail_closed`
— **4** случая (включая inline/table FK);
`integration/database/test_m07_postgresql_security_review.py::test_unrepresented_column_collation_fails_closed`
— **4** случая на реальных PostgreSQL 16.15/18.6 (column/domain).
Все упали до исправления и прошли после него.
`test_sqlite_clause_words_in_literals_comments_and_expressions_are_data`
проверяет, что lexical guard не путает literals/comments/quoted names с clauses
и сохраняет index/CHECK collation.

## Проверенные угрозы

Пути tests ниже относительно `packages/structuraguard/tests/`.

| Угроза | Применимость, control и наблюдаемое evidence |
| --- | --- |
| Attacker-controlled input | Request повторно валидируется; immutable target ограничивает selectors до I/O. `security/database/test_m07_full_inspection.py`, `test_inspection_policy.py`, `test_postgresql_policy.py`. R1 закрывает error path этой границы. |
| Resource exhaustion | Row/column/constraint/text/byte budgets, общий и statement/lock/cleanup deadlines; отказ без partial catalog, запрет reuse после cleanup failure. SQLite cancellation tests, PostgreSQL real timeout/lock/repeated cancellation/cleanup tests, `unit/database/test_inspection_catalog.py`. SCC алгоритм итеративный, property tests включают cycles и глубокий graph. |
| Parser exploit / unsafe deserialization | SQLite schema разбирает native runtime и bounded lexical parser; нет `eval`, `exec`, `pickle`/YAML deserialization. Новый `test_malformed_sqlite_file_is_rejected_without_leaking_bytes` проверяет безопасный malformed payload. Это не native fuzz campaign и не process sandbox. |
| XXE / DTD / network access | В M7 нет XML/HTML/document parser, DTD resolver или document fetch. Metadata expressions не исполняются. Сеть ограничена явным trusted PostgreSQL binding; SQLite authorizer запрещает ATTACH и `load_extension`. Проверка существующих parser modules вне diff не проводилась. |
| Prompt injection / excessive agency | Comments остаются untrusted DTO data; нет LLM/tool/writer вызовов. PG fixture содержит `ignore all instructions`; SELECT view с падающим выражением отражается без исполнения. `execute` отказывает до I/O и чтения batches в общем contract suite. |
| PII / secrets leakage | DSN исключён из serialization/repr, engine/pool logging изолирован; metadata-only без user rows. PG permission/authentication tests проверяют trace/context/logs. R1 добавляет SQLite и invalid-request chains. Произвольные comments могут содержать PII: catalog не является обезличенным документом. |
| SQL / identifier injection | PG queries фиксированы и параметризованы, `pg_catalog` квалифицирован, search_path фиксирован. SQLite names quoting и URI escaping. `unit/database/test_sqlite_edge_cases.py::test_identifier_injection_is_quoted_and_keeps_exact_name`, PostgreSQL quoted-name tests и SQL trace. |
| Allowlist / denylist bypass | Deny применяется до reflection; SQLite ASCII case rules, PostgreSQL qualified pairs. FK и nested types не расширяют scope; column selectors дают explicit unsupported. Оба отрицательных contract suite и real PG scope tests. |
| Schema drift | Canonical projection, version gate, deterministic IDs, mutation/permutation/property tests и coherent snapshot при concurrent PG DDL. R2 закрывает найденные потери schema semantics. Fingerprint ограничен полями DTO; view SQL definition в него не входит. |
| Unsafe logs / audit | Проверены DEBUG engine/pool и error paths, canaries не публикуются. Audit sink в M7 не подключён; downstream exporter вне scope. Сбор frame locals внешней telemetry требует отдельной privacy policy. |
| Path traversal / temporary files | Path задаёт trusted owner, request не содержит path/DSN. `Path.as_uri()` экранирует URI operators; новый `test_sqlite_path_cannot_supply_uri_options` проверяет `?mode=rw&immutable=1#fragment` в имени файла. Missing file не создаётся, readonly fixture остаётся единственным файлом. Symlink/path confinement принадлежит trusted owner; адаптер не является файловым sandbox. |
| Supply chain | Проверены новый SQLite extra, asyncio extra SQLAlchemy, lock artifact hashes, лицензии и изменения CI. Подробности ниже; новые production dependencies в ходе review не добавлялись. |

## Dependencies

Core dependencies не изменены M7. Новый `sqlite` extra использует SQLAlchemy;
существующий `postgres` extra включает `SQLAlchemy[asyncio]` и asyncpg.
Greenlet обслуживает SQLAlchemy async bridge. Testcontainers — только dev;
PostgreSQL images закреплены digest, имеют memory/CPU limits, CI job — timeout,
actions закреплены commit SHA и `persist-credentials: false`.

Проверенные installed/locked metadata:

| Dependency | Версия | Лицензия | Артефакты lock с SHA-256 |
| --- | --- | --- | --- |
| SQLAlchemy | 2.0.52 | MIT | 24 |
| asyncpg | 0.31.0 | Apache-2.0 | 33 |
| greenlet | 3.5.5 | MIT AND PSF-2.0 | 60 |
| testcontainers, dev | 4.15.0 | Apache-2.0 | 2 |

Источник артефактов — `https://pypi.org/simple`. На дату проверки upstream
security pages [SQLAlchemy](https://github.com/sqlalchemy/sqlalchemy/security),
[asyncpg](https://github.com/MagicStack/asyncpg/security) и
[greenlet](https://github.com/python-greenlet/greenlet/security) не показывают
опубликованных advisories. Это не полный OSV/PyPI advisory scan и не доказательство
отсутствия уязвимостей; consumers SDK разрешают version ranges своим lock-файлом.

SQLite runtime здесь 3.49.1, он поставляется с Python и не закреплён `uv.lock`.
Проверены [upstream CVE prerequisites](https://www.sqlite.org/cves.html) и
[меры защиты SQLite](https://www.sqlite.org/security.html). Опубликованные
SQL/FTS/extension payloads нельзя автоматически считать достижимыми через
фиксированные metadata queries с authorizer. Их отсутствие в данном пути не
доказывает безопасность native runtime. Обновление системного Python/SQLite,
native fuzzing и OS memory isolation в этот diff не входят.

## Фактические проверки

Новые tests запускались до production-исправлений: **10 failed, 1 passed**
для SQLite/request regressions; **4 failed** для real PostgreSQL collation.
После исправлений и добавления двух safe abuse probes: **13 passed** локально,
**4 passed** на PostgreSQL. Узкий suite unit/database + catalog contracts +
security/database + M7 examples: **201 passed**.

Команды запуска новых и узких tests (префикс `uv run --locked --no-sync`):

```bash
uv run --locked --no-sync pytest -q packages/structuraguard/tests/security/database/test_m07_security_review.py
uv run --locked --no-sync pytest -q -W error::sqlalchemy.exc.SAWarning -m database_integration packages/structuraguard/tests/integration/database/test_m07_postgresql_security_review.py
uv run --locked --no-sync pytest -q packages/structuraguard/tests/unit/database packages/structuraguard/tests/unit/contracts/test_database_catalog.py packages/structuraguard/tests/security/database packages/structuraguard/tests/docs/test_m07_examples.py
```

| Quality gate | Фактический результат |
| --- | --- |
| `make lint` | 264 файла уже отформатированы; Ruff passed |
| `make typecheck` | mypy: 262 source files, ошибок нет |
| `make test` | **2452 passed**, 68 database tests выбраны отдельной командой |
| `make test-integration` | **18 passed**, 2502 deselected |
| `make test-security` | **482 passed** |
| `make test-database` | **68 passed** на PostgreSQL 16.15/18.6; без skips/SAWarnings |
| `make docs` | strict build passed |
| `make test-build` | wheel/sdist, offline rebuild/import probes: `distribution verification OK` |
| `uv lock --check --offline` | 105 packages, exit 0 |
| `git diff --check` | exit 0 |

В первом полном прогоне pytest обнаружил одинаковые basenames двух новых
test files. Integration-файл переименован в
`test_m07_postgresql_security_review.py`; повторный общий collection и все
gates прошли. Конфигурация pytest, lint/typecheck и прежние tests не ослаблялись.
В общих suite сохраняются 5 прежних PyMuPDF/SWIG deprecation warnings.

Локальные команды использовали
`UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache`. Для Testcontainers:
`DOCKER_CONFIG=/private/tmp/structuraguard-docker-public` и
`DOCKER_HOST=unix:///Users/katana/.docker/run/docker.sock`.
Build использовал основной uv cache для offline hatchling. Полные gates
выполнялись вне filesystem sandbox из-за особенностей локального uv/Docker.
Paid LLM API не вызывались.

## Файлы, изменённые этим review

Остальной незакоммиченный M7 diff сохранён.

| Файлы | Изменение |
| --- | --- |
| `database/sqlite.py`, `database/postgresql.py` внутри `packages/structuraguard/src/structuraguard/` | Удаление raw exception chain на найденных public error paths |
| `database/_sqlite_sql.py`, `database/_postgresql_queries.py`, `database/_postgresql_catalog.py` | Отказ для непредставимых schema clauses/collations |
| `packages/structuraguard/tests/security/database/test_m07_security_review.py` | 13 локальных regression/abuse cases |
| `packages/structuraguard/tests/integration/database/test_m07_postgresql_security_review.py` | 4 regression cases на реальном PostgreSQL |
| `docs/database-inspection.md` | Явные ограничения SQLite/PG catalog support |
| `docs/plans/M07_database_inspector.md` | Ссылка на последующий review |
| `docs/plans/M07_security_review.md` | Findings, threat matrix, команды, результаты и границы проверки |

## Остаточные риски и границы проверки

- Нативные ошибки SQLite/asyncpg/greenlet, предельная память DB server/процесса
  и непрерываемые OS calls не покрываются unit tests или thread cancellation.
- Нет MITM/TLS/certificate rotation теста: Testcontainers использует локальную
  сеть; transport policy и доверие endpoint задаёт владелец PostgreSQL DSN.
- Нет тестов на PostgreSQL 15/17, альтернативные OS/SQLite builds и live CI runner;
  реально проверены PostgreSQL 16.15/18.6 и локальная среда Python 3.12.9.
- Hash не удостоверяет автора каталога, credentials, grants или будущую
  неизменность БД. Loader/TOCTOU и downstream logs/audit/LLM export вне M7.
- View definitions и другие отсутствующие в DTO объекты не покрыты fingerprint;
  обнаруженные непредставимые clauses/collations теперь приводят к отказу.
