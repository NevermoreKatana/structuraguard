# M14 — security review текущего diff

Дата: 2026-09-13. Scope: текущие tracked changes и новые untracked файлы M14
в рабочем дереве; tests/приёмка из предыдущего шага также учтены. Другие milestones
и неизменённые подсистемы рассматриваются только как владельцы existing guards.
Основание: [план M14](M14_security_layer.md), [приёмка](M14_acceptance.md),
[threat model](../threat-model.md), ADR 0030–0033 и security checklist.

**За последовательные reviews найдены и исправлены один High и две Medium
проблемы.** Последний High — SQLite constraint reader — закрыт отдельным fix
и regression suite. Повторный review нового diff существенных findings не выявил.
Полнота исходного M14 не подтверждена: незавершённые controls перечислены в приёмке.

Пути `src/` и `tests/` ниже относительны `packages/structuraguard/src/structuraguard/`
и `packages/structuraguard/tests/`. Номера строк относятся к исправленному дереву.

## M14-SR-03 — High: SQLite constraint reader обходил центральную DB policy

- **Location:** `src/database/constraint_reader.py:221`,
  `src/database/constraint_reader.py:306`, `src/database/sqlite.py:382`.
- **Trust boundaries:** TB-05, SR-14/29; недоверенный catalog/request →
  SQLite metadata reflection и параметризованные EXISTS.
- **Путь эксплуатации:** после сужения central policy локальный
  `ConstraintReadPolicy` продолжал разрешать ключ. Самосогласованный catalog/request
  с новым target policy fingerprint проходил reader: SQLite-ветка проверяла только
  legacy selectors. Fingerprint можно пересчитать; он не подтверждает authority.
- **Доказательство:** на реальной SQLite inspector возвращал
  `DATABASE_COLUMN_NOT_ALLOWED` для column deny и `TARGET_NOT_ALLOWED` для
  schema/table deny, тогда как reader во всех трёх случаях возвращал `exists=True`.
- **Влияние:** чтение запрещённых metadata и раскрытие существования конкретных
  значений. Это обход scope; SQL injection и DML воспроизведение не выполняло.
- **Исправление:** `authorize_database_scope` до SQLite connection; общая с
  inspector проверка свежих bounded column names в той же read transaction до
  чтения definitions и EXISTS. Локальная policy дополнительно ограничивает доступ;
  public signatures, read-only authorizer, deadlines и cleanup сохранены.
- **Security regression:** `tests/security/database/test_m14_constraint_policy.py`:
  schema/table/column denies, case folding, пустые allowlists, rebound catalog,
  lookup/identity/unqueried column, schema drift и N/N+1 column cap. До fix —
  13 failed / 2 passed; после — 15 passed. Запрещённый scope не открывает connection;
  column denial не читает definitions и не выполняет EXISTS. Положительный case
  сохраняет корректные exists/conflicts. Узкий набор — 283 passed.

## M14-SR-01 — Medium: raw diagnostics пересекали source/DLP boundaries

- **Location:** `src/security/source.py:21`, `src/security/classification.py:127`,
  `src/security/redaction.py:249`. До исправления source transport вызывался
  напрямую, DLP/restore обрабатывали только перечисленные классы исключений.
- **Trust boundaries:** TB-02/TB-07/TB-08, SR-20; недоверенные source/metadata
  и restricted значения → error API → host logger/report.
- **Путь эксплуатации:** attacker-controlled значение вызывает lookup/read failure
  в подключённом adapter. `SourceError(message=raw_value)` сохранялся в
  `SecuritySession.call`; `KeyError(raw_value)` от source/chunk/map store обходил
  узкий `except`. Ошибка property `SourceReader.source_fingerprint` также выходила
  до error barrier. Для воспроизведения не нужны реальные credentials: synthetic
  canaries видны в `str(error)` и стандартном formatted traceback.
- **Влияние:** раскрытие PII/secrets host logger или обработчику ошибки; часть
  transport failures не создавала terminal resource event. Это не подразумевает
  защиту от hostile Python-кода внутри host: речь о недоверенных diagnostics
  обычного подключённого adapter.
- **Минимальное исправление:** общий error barrier source reader охватывает
  fingerprint/read и сохраняет уже принятое safe resource decision. Остальные
  ошибки transport дают `SECURITY_OPERATION_FAILED` и закрывают session. DLP
  преобразует все обычные foreign `Exception` в закрытый `SECURITY_SCAN_FAILED`,
  restore — в `SECURITY_MAP_INVALID`; process-control exceptions сохраняются,
  cancellation остаётся `CancelledError` без raw message. Regex-redaction
  произвольного текста ошибки не используется как основная защита.
- **Security regression:**
  `tests/security/policy/test_source_error_boundary.py` — typed/lookup ошибки
  snapshot и guarded reader, ошибка fingerprint getter, safe terminal event;
  `tests/security/privacy/test_foreign_error_boundary.py` — chunk/store lookup.
  Первые четыре cases упали до исправления; дополнительный fingerprint case
  также отдельно воспроизведён красным тестом. После исправления 7 cases проходят.

## M14-SR-02 — Medium: sandbox failure не закрывал общий security run

- **Location:** `src/parsers/runners.py:231`, `src/parsers/runners.py:396`;
  lifecycle port — `src/ports/resources.py:107`.
- **Trust boundaries:** TB-03/TB-06/TB-08, SR-07/10/17/20; недоверенный plugin
  output/cleanup outcome → runner → общий resource guard → downstream DB/LLM.
- **Путь эксплуатации:** parser выдаёт malformed probe, forged source provenance
  либо backend не подтверждает cleanup. Runner корректно бросает typed error
  и вызывает cleanup, но `_call`/cleanup не записывали terminal failure в session.
  Если host перехватывал ошибку и продолжал run, `session.before_query()` проходил.
- **Влияние:** fail-closed invariant общего run нарушен; возможно продолжение
  обработки промежуточных данных после parser failure, без terminal resource
  audit evidence. Это не обход отдельной DB allowlist или validators; они
  продолжают действовать и ограничивают последующие операции.
- **Минимальное исправление:** runner вызывает существующий
  `SecuritySession.record_failure` через `ParserResourceGuard` при operation и
  cleanup failures. Исходный runner error code сохраняется. Cleanup cancellation
  помечает session отменённой. Следующий DB/LLM resource guard отказывает
  `SECURITY_RUN_CLOSED`; event содержит только разрешённые code/outcome metadata.
- **Security regression:**
  `tests/security/sandbox/test_runner_terminal_policy.py::test_runner_failure_closes_shared_run_and_records_safe_event`
  — probe/provenance/cleanup cases. Все три падали на разрешённом `before_query()`
  до исправления и проходят после него, с проверкой cleanup и safe event.

`ParserResourceGuard` теперь требует `record_failure(code)`. Штатный
`SecuritySession` уже имел этот метод. Собственный host guard должен реализовать
его terminal semantics; новый no-op не удовлетворяет контракту. Wire DTO,
existing parser error codes и DB/LLM permissions не изменены.

## Проверенные угрозы

| Угроза / attacker-controlled input | Проверенная детерминированная граница и evidence | Вывод / residual risk |
|---|---|---|
| Source bytes/streams, fields, JSON, plugin output, DB metadata, LLM replies | Exact DTO/type/provenance checks, bounded inputs; исправленный error barrier; existing contract suites | Подтверждённые дефекты устранены; supplied reader/host code не аттестуется hash |
| Resource exhaustion | Compiler сужает existing contexts; N/N+1 byte/record/column/depth/text/chunk caps; общие calls/tokens/queries/deadlines; `security/policy/`, `privacy/`, `injection/`, `sandbox/` | Предварительные caps сохранены; CPU/RSS/native-call preemption зависит от OS runner |
| Parser exploit / unsafe deserialization | В diff нет eval/exec/pickle/unsafe YAML/shell invocation; JSON wire закрыт, builtin classes строго allowlisted; existing `security/parsers/` | Zero-day/native parsers требуют sandbox; fake backend проверяет лишь interface |
| XXE/DTD/network access | Existing XML/YAML/HTML и OOXML guards не отключаются compiler; markup/document security corpus; Tika остаётся opt-in, exact source-bound review до upload | OS no-network не доказывается declaration; remote retention вне SDK |
| Prompt injection / excessive agency | Base scanner обязателен, signals только ограничивают; exact request/report binding; local-only/RESTRICTED/fallback и no-tools проверены; M6/M10 review bridge не выпускает records/generation | Эвристика не доказывает безопасность; полная source/DLP/egress composition пока отсутствует |
| PII/secrets leakage | Classification floors, bounded recognizers, protected map, safe summaries; исправление M14-SR-01; privacy FP/FN/property fixtures | Неполнота detectors документирована; caller logging raw DTO/locals не защищён |
| SQL / identifier injection | SQL по-прежнему строят DB adapters с bound values; central ASCII identifiers/system schemas deny; M11 checked plans; actual PostgreSQL parameter/hostile identifier tests | DBA/unsafe driver/host compromise вне детерминированного policy gate |
| DB allowlist/denylist bypass | Deny wins, target scope только сужается, full-column admission до reflection; раздельные principals и writer transaction recheck; `test_m14_constraint_policy.py` | SQLite bypass M14-SR-03 исправлен. Partial catalog запрещён; legacy target без central policy сохраняет старый режим |
| Schema drift | Existing pre-DML/pre-COMMIT reflection/fingerprint, rollback/state assertions в PostgreSQL loader suites | Hash связывает snapshot, не доказывает доверие к DBA |
| Logs / audit events | Closed UUID/count/status events, canonical HMAC, CAS/sequence/key/anchor checks, transactional event+intent; safe failure; исправленный terminal runner event | Key holder может переподписать history; suffix truncation требует независимого anchor; outbox delivery — host |
| Path traversal / temporary files | M14 source snapshot — bounded memory; новый path opener/temp writer не добавлен; OOXML traversal/symlink/bomb guards остаются; descriptor не загружается в host | A2 roots/no-follow/TOCTOU ещё не реализован. OS temp isolation требует настоящего backend; здесь не заявляется |
| Production dependencies | Optional `security`/`all` extra, lock/hash/metadata и официальные advisories проверены; подробности ниже | Reachable exploit новой locked dependency в использованном AESGCM path не установлен; supply-chain risk остаётся |

## Supply-chain проверка {#m14-supply-chain}

В diff добавлена одна прямая optional production dependency — `cryptography>=46,<51`.
Base dependencies прежние. Новые lock entries и установленное окружение совпадают:

| Package | Locked / installed | License из installed metadata |
|---|---|---|
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause |
| cffi | 2.1.1 | MIT-0 |
| pycparser | 3.0 | BSD-3-Clause |

Все три registry entries используют `https://pypi.org/simple`; sdist/wheels имеют
hashes в lock. Импорт AESGCM отложен до явного создания encrypted store; production
код не загружает packages или keys из сети/окружения. Нативная поверхность
cryptography/OpenSSL/CFFI добавляется только при установке extra. SDK передаёт
AESGCM bounded `bytes`, 32-byte key, 12-byte random nonce и authenticated AAD.

На дату review сверены [официальный changelog](https://cryptography.io/en/stable/changelog/)
и [список advisories PyCA](https://github.com/pyca/cryptography/security/advisories).
В частности, buffer overflow для non-contiguous buffers исправлен с 46.0.7,
а PKCS#7 oracle — с 50.0.0; locked 50.0.1 новее этих fixes. Описанные пути
не используются здесь: SDK не передаёт non-contiguous buffers и не вызывает
PKCS#7 decrypt. Источники: [GHSA-p423-j2cm-9vmq](https://github.com/pyca/cryptography/security/advisories/GHSA-p423-j2cm-9vmq),
[GHSA-g6cj-pr64-35w5](https://github.com/pyca/cryptography/security/advisories/GHSA-g6cj-pr64-35w5).

**Ограничение:** package range допускает более старые releases у downstream
consumer, который не использует workspace lock. Lock/hashes не удостоверяют
честность upstream release, а просмотр advisories не заменяет полный SBOM/CVE
scan конечного host. Версии dependencies в рамках этих fixes не менялись;
неподтверждённые CVE не выдаются за exploit SDK.

## Команды и фактические результаты

Таблица ниже — история первого security review, до документационного шага и
исправления M14-SR-03. Актуальные результаты после всех fixes и список пропусков
зафиксированы в [плане передачи](M14_security_layer.md#m14-commit-checks).

Все pytest commands используют `uv run --locked --no-sync`. Для обычного sandbox
запуска использован `UV_CACHE_DIR=/private/tmp/structuraguard-m14-uv-cache`.
Document workers, локальные HTTP fixtures и PostgreSQL 16/18 Testcontainers
запускались с предоставленным разрешением на соответствующий local runtime.
Платные/external LLM API не вызывались.

| Проверка | Результат |
|---|---|
| Canary regression до fixes: source transport + chunk/map error barriers | 4 failed: раскрытие raw exception |
| Отдельный fingerprint getter regression до fix | 1 failed: raw KeyError |
| Sandbox terminal policy до fix | 3 failed: следующий query разрешён |
| Три новых regression files после fixes | 10 passed |
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/security/policy packages/structuraguard/tests/security/privacy packages/structuraguard/tests/security/sandbox packages/structuraguard/tests/security/parsers` | 493 passed; 19.47 s |
| `make lint typecheck` | Ruff: 568 files, passed; mypy: 564 source files, no issues |
| `make test` | 4288 passed, 430 deselected, 5 existing SWIG deprecation warnings; 130.06 s |
| `make test-integration` | 30 passed, 4688 deselected, 5 existing SWIG deprecation warnings; 7.44 s |
| `make test-security` | 1376 passed; 32.94 s |
| `make test-build` | Wheel/sdist собраны; isolated installation и distribution verification OK |
| `make test-database` | 430 passed на PostgreSQL 16/18; 206.41 s |
| `make docs`, `git diff --check` | Strict docs build passed; diff whitespace check passed |

## Изменения и границы результата

В первом security review исправлены `security/source.py`, `security/classification.py`,
`security/redaction.py`, `parsers/runners.py` и `ports/resources.py`.
Добавлены три regression files, этот отчёт и ссылки/уточнения в документации.
Existing tests, lint/typecheck settings и guards не ослаблены.

В отдельном fix M14-SR-03 изменены только `database/constraint_reader.py`,
`database/sqlite.py` и добавлен `test_m14_constraint_policy.py`. При подготовке
передачи обновлена документация; исполняемый код повторно не менялся.

Непроверенные host guarantees: реальная OS/network/temp isolation, provider
retention, внешний outbox dispatcher и key/store compromise recovery. Source
safe-open/export sink policy и performance baseline остаются незакрытыми пунктами
[приёмки M14](M14_acceptance.md); несвязанные features не реализовывались.
