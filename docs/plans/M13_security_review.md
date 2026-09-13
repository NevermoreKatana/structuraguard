# M13 — security review текущего diff

Дата: 2026-09-13. Scope: текущие tracked changes и новые файлы M13 A–D, включая
последующий [аудит приёмки](M13_acceptance.md). Старые parser/LLM implementations
не переписывались и отдельно не аудировались. Основание: threat checklist,
[план M13](M13_staging_loader.md), ADR 0026–0029 и наблюдаемые PostgreSQL tests.

**Найдено и исправлено четыре Medium проблемы.** Critical/High findings в этом
scope не выявлены. Severity учитывает условия эксплуатации: первые три проблемы
требуют избыточных полномочий роли либо изменения DB definitions; четвёртая —
возможности изменять persisted metadata. Один только текст документа не даёт
эти полномочия. SDK при этом обязан отклонять такую конфигурацию/metadata.

Все пути ниже относительно корня репозитория. Строки указывают текущие места
исправлений; описание «до исправления» относится к состоянию diff перед review.

## M13-SEC-01 — неполная проверка полномочий staging-роли

- **Severity:** Medium, исправлено.
- **Место:** `packages/structuraguard/src/structuraguard/stores/postgresql.py:122`, `_principal`.
- **Путь эксплуатации:** роль staging владеет функцией, входит в NOINHERIT role,
  владеющую таблицей, либо получает `pg_read_server_files`. Ранее проверялись
  собственные role flags, CREATE grants и владельцы `pg_class` через `USAGE`.
  Функции/types и возможность SET ROLE через membership не учитывались.
- **Влияние:** `store.begin` принимал роль, сохраняющую DDL/file authority за
  пределами staging. Компрометация этих credentials имеет больший радиус доступа;
  декларация least privilege не соответствовала действительной проверке.
- **Минимальное исправление:** runtime writer/maintenance вызывают существующий
  `database._dry_run_permissions.principal`: pg_shdepend ownership, MEMBER,
  privileged memberships, schema ownership, flags и login/session identity.
  Bootstrap остаётся явным отдельным путём с проверкой identity. Ошибка переводится
  в прежний `STAGING_PRINCIPAL_FORBIDDEN`; GRANT/REVOKE runtime не выполняет.
- **Security regression:** `packages/structuraguard/tests/integration/database/test_postgresql_staging_security.py::test_staging_rejects_ddl_and_privileged_role_memberships`.
  Три варианта × PostgreSQL 16/18; до исправления все шесть принимались, после —
  отказ до INSERT в runs, таблица остаётся пустой.

## M13-SEC-02 — исполнение index support function из непроверенной схемы

- **Severity:** Medium, исправлено. Требуется установленный custom operator class
  и изменение staging index; создание operator class в PostgreSQL требует
  привилегированного администратора. Это не SQL injection из scalar input.
- **Место:** `packages/structuraguard/src/structuraguard/stores/_postgresql_schema.py:126`, `unsafe_index`.
- **Путь эксплуатации:** добавить валидный обычный btree index на runs с custom
  operator class. В его support function возможны дополнительные чтения/записи.
  Предыдущий preflight запрещал expression/partial/invalid indexes, но не проверял
  access method/operator class и принадлежность индекса ожидаемым constraints.
- **Влияние:** штатный INSERT SDK запускал SQL, не входящий в staging contract.
  Reproduction использует VOLATILE SECURITY DEFINER comparator: после второго
  `begin` реально появилась одна строка в `executed`, на которую staging writer
  не имел прямого INSERT grant. Это подтверждённый выход за closed SQL scope.
- **Минимальное исправление:** допускать только btree indexes штатных PK/UNIQUE
  constraints и operator classes из pg_catalog. Остальные definitions дают
  `STAGING_SCHEMA_UNAVAILABLE` до доступа к run data. Индексы не удаляются SDK.
- **Security regression:** `packages/structuraguard/tests/integration/database/test_postgresql_staging_security.py::test_staging_rejects_custom_index_code_before_executing_it`.
  Проверяются ноль side-effect rows, неизменное число runs и typed veto на PG 16/18.

## M13-SEC-03 — collation меняет namespace isolation

- **Severity:** Medium, исправлено. Namespace — application scope, не замена
  отдельным DB grants для взаимно недоверенных tenants.
- **Место:** `packages/structuraguard/src/structuraguard/stores/_postgresql_schema.py:136`, column collation gate.
- **Путь эксплуатации:** изменить collation namespace во всех трёх staging
  таблицах на case-insensitive nondeterministic ICU, сохранив типы, PK/FK и
  schema_info version. Ранее shape проверяла `text`, но не collation.
- **Влияние:** scoped WHERE для `APP-1` находил существующий run `app-1`; context
  внутри payload не содержит namespace и не устранял смешение. Caller мог
  получить refs другого application namespace или столкнуться с неверным replay.
- **Минимальное исправление:** фиксированная staging schema принимает только
  default column collation (либо отсутствие collation у integer). Несовпадение
  отклоняется под table locks до `_load` и любого изменения metadata.
- **Security regression:** `packages/structuraguard/tests/integration/database/test_postgresql_staging_security.py::test_staging_rejects_nondeterministic_namespace_collation`.
  Реальный ICU collation, восстановленные FK и второй store с `APP-1`; до
  исправления get_run возвращал запись, после — schema veto на PG 16/18.

## M13-SEC-04 — неограниченные поля persisted metadata читаются до бюджета

- **Severity:** Medium, исправлено.
- **Места:** `packages/structuraguard/src/structuraguard/stores/postgresql.py:294`,
  `packages/structuraguard/src/structuraguard/stores/_postgresql_schema.py:245`,
  `packages/structuraguard/src/structuraguard/database/_load_ledger_schema.py:159`.
- **Путь эксплуатации:** записать oversized fingerprint в доступную staging
  runs row либо повредить schema_info; для raw staging payload использовать
  многобайтный UTF-8. Fingerprint читался целиком; `substr(payload, ..., max_bytes)`
  ограничивал число символов, а не байтов. Проверка Python происходила после
  materialization результата драйвером.
- **Влияние:** объём сетевого ответа и памяти превышал предусмотренную границу;
  PostgreSQL TEXT допускает значительно более крупные значения. В reproduction
  Python получил 220000/280000 bytes fingerprint и 262144 bytes UTF-8 payload
  при контрольной границе 65536 bytes. Ошибка после чтения не предотвращала DoS.
- **Минимальное исправление:** SQL CASE по `octet_length(payload)` выдаёт NULL
  вместо oversized payload; fingerprint читается с sentinel длиной 65 для raw
  SHA-256 и 72 для `sha256:` version binding. Обрезанное/oversized поле не может
  успешно пройти сравнение. Валидация JSON остаётся строгой и выполняется после
  server bound, без unsafe deserialization.
- **Security regressions:**
  `packages/structuraguard/tests/integration/database/test_postgresql_staging_security.py::test_persisted_metadata_text_is_bounded_before_transfer_to_python`
  и `packages/structuraguard/tests/integration/database/test_postgresql_load_security_review.py::test_ledger_schema_fingerprint_is_bounded_before_transfer`.
  Asyncpg text codec измеряет реальные строки до DTO parsing; тесты не проверяют
  spelling SQL. После исправления размер ограничен, returned error typed,
  target/staging/ledger не изменены вызовом чтения/loader.

## Матрица применимых угроз

| Угроза | Проверенный control / результат |
|---|---|
| Attacker-controlled input | MappingPlan, normalized DTO, persisted metadata и DB definitions недоверенные. `checked` ограничивает traversal до dump/hash, повторно валидирует DTO; EOF/hashes/seal/M11/M12 DB constraints проверяются перед load. Hash связывает вход, но не удостоверяет внешнего автора |
| Resource exhaustion | Input/node/record/batch/key/query/SQL/deadline/cleanup budgets, chunking и bounded EXISTS. Исправлен M13-SEC-04; regression измеряет PG wire data до parsing. Throughput/peak memory всей ОС не гарантируются |
| Parser exploit / unsafe deserialization | Новые M13 модули не запускают parser, pickle, YAML object construction, eval/exec или plugins. Persisted DTO читаются через Pydantic JSON после server-side size checks. Старые parser implementations вне scope |
| XXE / DTD / network access | M13 не разбирает XML/DTD и не dereference artifact_id. Единственная штатная сеть — явно сконфигурированные PostgreSQL endpoints; writer endpoint связан с inspector host/port/database. Парсерный XXE pipeline не переаудировался |
| Prompt injection / excessive agency | Нет LLM/tool calls. Source/comment/model-derived strings не становятся SQL. M11 и catalog дают identifiers; SQL строится adapter. Исправлен дополнительный DB metadata execution path M13-SEC-02 |
| PII / secrets leakage | SecretStr DSN исключён из serialization; sensitive DTO repr скрыт; driver errors заменяются closed codes; private engine/pool loggers не печатают rows/parameters. Quarantine/audit хранят hashes/codes/counts. Полные JSON/result/ref artifacts чувствительны и требуют контроля owner |
| SQL / identifier injection | Values — bind parameters, identifiers — trusted target + checked/reflected catalog и compiler quoting. Quoted-name/value canary tests из loader/staging/dry-run сохранены; arbitrary SQL/DDL pipeline отсутствует |
| DB allowlist / denylist | `_scope` ограничивает reflection, M11 scope/source identity/lookup policies имеют veto, writer principal и column grants проверяются отдельно. Исправлены staging role и namespace gaps M13-SEC-01/03 |
| Schema drift | Target locks, fresh catalog перед DML и перед COMMIT, повтор grants; фиксированные staging/ledger schemas проверяются при каждой операции. Поддержка неизвестных definitions не включается по одному совпадению fingerprint |
| Unsafe logs / audit events | Audit/marker/quarantine атомарны с target; SQL/credentials/source values не включаются. Canary и corrupt receipt tests проверены; failure не создаёт ложный committed audit. Raw hashes не считаются authentication/redaction всех возможных данных |
| Path traversal / временные файлы | Runtime M13 не открывает пути, не создаёт temp files и не dereference refs как file/URL. Artifact storage/retention owner — внешняя граница; supply artifact_id не запускает file access |
| Supply chain | `pyproject.toml`, package pyproject и `uv.lock` не менялись. Новых production dependencies нет. SQLAlchemy/asyncpg остаются optional postgres extra; import guards и offline distribution verification сохраняются. Testcontainers images закреплены digest |

Существующие security tests в `tests/security/loading`, `tests/security/stores` и
PostgreSQL S/B/C/D/E из [матрицы приёмки](M13_acceptance.md) остаются в силе;
assertions, lint, typecheck и SQLAlchemy warning policy не ослаблены.

## Проверки

До исправления: 16 failing PG cases (roles/index/collation/fingerprints) и отдельно
2 failing UTF-8 payload cases. После исправления: **18 passed** на PostgreSQL 16/18,
7.81 s. Узкие pure/contract/security/import tests: **51 passed**, 2.05 s.
Команды запускаются через `uv run --locked --no-sync`; для PostgreSQL добавлены
`-W error::sqlalchemy.exc.SAWarning -m database_integration`.

| Команда | Фактический результат |
|---|---|
| `pytest -q .../test_postgresql_staging_security.py .../test_postgresql_load_security_review.py` | 18 passed |
| Узкие staging/loader PostgreSQL tests | **180 passed**, 100.62 s |
| `make lint` | **PASS**, 504 файла, Ruff без замечаний |
| `make typecheck` | **PASS**, 500 файлов, без ошибок |
| `make test` | **3871 passed, 374 deselected**, 139.16 s; 5 сторонних SWIG/PyMuPDF DeprecationWarning |
| `make test-database` | **374 passed**, 174.38 s; PostgreSQL 16/18 |
| `make test-integration` | **30 passed, 4215 deselected**, 8.52 s |
| `make test-security` | **959 passed**, 33.21 s |
| `make docs` | **PASS**, strict build |
| `make test-build` | **PASS**, offline wheel/sdist, `distribution verification OK` |
| `git diff --check` | **PASS** |

Полная команда regression:

```sh
uv run --locked --no-sync pytest -q -W error::sqlalchemy.exc.SAWarning \
  -m database_integration \
  packages/structuraguard/tests/integration/database/test_postgresql_staging_security.py \
  packages/structuraguard/tests/integration/database/test_postgresql_load_security_review.py
```

Узкий PG suite дополнительно включает `test_postgresql_staging.py`,
`test_postgresql_load_outcomes.py` и `test_postgresql_loader.py` из той же директории.
Полные gates выполнялись с разрешёнными Docker, loopback и `/bin/ps`, необходимыми
существующим test fixtures. Runtime grants не расширялись.

Во время проверки Mypy обнаружил импорт `writer_engine` через модуль, который
его не экспортирует: новый тест исправлен на импорт из defining module, без ignore.
Первый объединённый PG suite дал **164 passed, 16 setup errors** из-за двух
FixtureDef для импортированной session fixture ролей (`role already exists`).
`stage_accounts` перенесена из test module в общий conftest; DDL и assertions
не ослаблялись. Повторный объединённый и полный PG suites прошли.

Логи: `/private/tmp/structuraguard-m13-security-*.log`.

## Остаточные границы

- Не заявлена защита от администратора, способного произвольно менять PostgreSQL
  system catalog/roles/functions. Owner server-value permissions и координация
  migrations остаются trusted deployment authority.
- Нет полного M12 ingest coordinator, generated-PK load/propagation и старого
  facade dry-run LoadReport; эти пробелы приёмки не замаскированы security fixes.
- Внешние artifact authenticity/retention, взаимная изоляция tenants с общими SQL
  credentials, OS memory ceiling и реальные network/process crash scenarios
  не доказаны этим review. Применимые ограничения подробно указаны в аудите приёмки.
- Непроверенных платных LLM вызовов нет: такие API не использовались.
