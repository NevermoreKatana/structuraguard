# M09 — Security review текущего diff

Дата: 2026-09-11. Scope: новые contracts/port/модули deterministic mapping,
их tests, документация, offline evaluator и дополнения distribution manifest.
Review включает untracked файлы текущего diff. Парсеры, DB/LLM adapters, loader
и предыдущие milestones не изменялись и заново не ревьюились.

Результат отдельного security-этапа: обнаружена и исправлена одна проблема
**Medium**. Ниже сохранены её доказательство и результаты этого этапа.
Последующий final review выявил ещё два Medium в `mapper.py`: использование
confusable target как anchor и нормализация таблицы до scope filtering.
Оба исправлены и покрыты `tests/security/mapping/test_m09_review_regressions.py`
(10 regression/control cases). Повторный review существенных findings не выявил;
актуальные gates и residual risks — в [checklist M9](M09_deterministic_mapper.md#checklist-commit-pr).

## M09-SEC-001 — Обход operation budget через FK/identity fan-out

- **Severity:** Medium, availability/resource exhaustion.
- **Места:**
  `packages/structuraguard/src/structuraguard/mapping/_context.py:180`,
  `packages/structuraguard/src/structuraguard/mapping/_context.py:200`.
  Номера относятся к итоговому файлу; это обход child FK и identity evidence.
- **Контроль атакующего:** schema/FK metadata или Semantic Catalog, передаваемые
  приложением в mapper. Scope/options остаются параметрами доверенного caller.
- **Путь эксплуатации:** предоставить множество child FK либо identity keys
  известной таблицы. Scoring многократно обходил их для source/target pairs,
  а `max_operations` учитывал lexical comparisons и только часть graph context.
  Отсутствующий в scope parent не устранял стоимость обхода его FK ограничений.
  Даже неподдержанные identity keys бесплатно сравнивались с DB constraints.
- **Влияние:** входные данные, формально проходящие byte/pair ceilings, могли
  потреблять CPU сверх установленного caller operation budget. Для встраиваемого
  async SDK это задержка event loop и снижение доступности приложения.
  Ни выполнения SQL/кода, ни обхода grants этим не получалось.
- **Безопасное воспроизведение:** 40 колонок/identity keys с `max_operations=100`
  либо 40 FK с одним разрешённым child target и `max_operations=10`.
  Оба вызова до исправления возвращали результат; regression tests дали
  **2 failed: DID NOT RAISE MappingError**. Нагрузочный DoS не запускался.
- **Минимальное исправление:** учитывать зависимый от metadata объём работы
  **до** обхода: индексацию FK/relationships, ordered component/anchor scans,
  child FK checks, identity columns/keys и comparisons с DB constraints.
  Счётчик остаётся локальным для rank call. Превышение даёт
  `MappingError/MAPPING_LIMIT_EXCEEDED`, `details.reason=operations`, без partial
  result. Числовые weights/thresholds, порядок кандидатов и scoring evidence
  не изменялись.
- **Security regression tests:**
  `packages/structuraguard/tests/security/mapping/test_m09_resource_accounting.py:18`,
  `packages/structuraguard/tests/security/mapping/test_m09_resource_accounting.py:46`.
  После исправления regression + context/graph/evidence tests дали **45 passed**.
- **Статус:** исправлено; изменения production кода ограничены `_context.py`.

## Границы доверия и применимые угрозы

Входы mapper — готовые M8/M7 snapshots, локальные semantic hints и trusted
`MappingScope`/options. Данные, metadata и текст считаются недоверенными;
публичный `rank` повторно проверяет DTO, bindings, sizes и hashes. Hash обеспечивает
целостность и согласованность snapshots, но не удостоверяет автора данных.

| Угроза | Применимость, проверка и результат |
|---|---|
| Attacker-controlled input | Применимо к names, labels, comments, aliases, FK graph и aggregates. `_inputs.py` ограничивает обход до serialization, ревалидирует также обходы Pydantic через model_copy/model_construct, проверяет refs/policy/hash. Existing malformed/stale/foreign binding tests проходят. Arbitrary исполняемые Python objects внутри вызывающего процесса не являются изоляционной границей SDK. |
| Resource exhaustion | Применимо: bytes/depth/numeric size, fields/columns/edges/aliases, pairs, operations, state/result. Найден M09-SEC-001 и добавлены два regression tests. Existing budgets/cancellation tests проходят; полная матрица source×target не материализуется. |
| Parser exploit / unsafe deserialization | Production parser/loader в diff отсутствует. Semantic Catalog принимается как DTO/dict и проходит Pydantic; pickle/eval/exec/unsafe YAML не используются. Python object tags в metadata проверены новым inert-payload test; это текст, а не команды десериализатору. |
| XXE / DTD / network | XML в M9 не разбирается. Новые payload tests передают external DTD и file entity через comments/aliases/path labels при запрещённых file/socket/process calls; внешних действий нет. Это проверка границы M9, а не повторный аудит XML parser предыдущего milestone. |
| Prompt injection / excessive agency | LLM/embeddings/providers/tools не вызываются. Инструкции в metadata не меняют scope и не получают authority; score вычисляется SDK. Возвращаются только candidates, без MappingPlan/approval/SQL/load. Existing AST boundary и новые inert-payload tests проходят. |
| PII / secrets leakage | Новые DTO скрывают repr; safe_summary содержит только counts/classification. Explanations и errors не копируют examples, comments, aliases или labels. Проверены secret sentinels в metadata, output, repr, logs и ошибках. Полный result и legacy MappingCandidate остаются чувствительными, что явно указано в API guide. |
| SQL / identifier injection | Нет SQL builder, connection либо execute в новых production модулях. Targets берутся из согласованного catalog и точного scope, identifiers не интерполируются в запросы. SQL payload остаётся inert metadata; SQLite integration подтверждает отсутствие записи. Сам по себе candidate не является безопасным SQL identifier для произвольного внешнего consumer. |
| DB allowlist / denylist | Trusted scope обязателен; deny сужает allow. Unknown refs/bindings отклоняются, forbidden/generated/system/view/nonwritable targets исключены до scoring. Это подтверждают tests с trap на scoring. Предыдущая FK-scope regression сохранена: недоступный parent не снимает child blocker. |
| Schema drift | Проверяются target/policy bindings, schema/profile fingerprints и согласованность graph с FK metadata. Forged hash/graph не принимаются. Проверка drift живой БД после inspection относится к будущему validation/load; соединения для этого mapper не открывает. |
| Небезопасные logs / audit | Mapper не создаёт logger/audit events и не пишет в sinks. caplog + safe-summary/repr/error tests не показывают raw payload. Запись полного result вызывающим приложением остаётся вне гарантий safe_summary. |
| Path traversal / временные файлы | Source path labels используются только лексически. Новый `../../private/...` payload проверен при запрете open/Path.open/os.open/mkstemp/Popen/socket; файлов нет. SDK не создаёт временные файлы. Dev evaluator читает явно заданный локальный `--fixtures` path; это не путь из source metadata и не endpoint сервиса. |
| Supply chain | Diff `pyproject.toml`, package pyproject и `uv.lock` пуст. Новых production dependencies нет; imports mapping ограничены существующими contracts/domain, Pydantic и stdlib. Distribution manifest расширен фиксированными путями новых модулей; механика extraction/install не менялась. |

Дополнительный test file:
`packages/structuraguard/tests/security/mapping/test_m09_inert_payloads.py`
— шесть вариантов SQL, YAML object tag, external DTD/XXE, traversal path и prompt
instruction. Запрещены `open`, `Path.open`, `os.open`, `socket.socket`,
`subprocess.Popen`, `tempfile.mkstemp`; все шесть сценариев прошли.

## Проверки

Новые tests сначала запущены отдельно, затем — вместе с затронутым M9 suite.
Конфигурация lint/typecheck/pytest и существующие assertions не ослаблялись.
Реальные LLM API не использовались.

```bash
env UV_CACHE_DIR=/private/tmp/structuraguard-m09-uv-cache uv run --locked --no-sync pytest \
  packages/structuraguard/tests/security/mapping/test_m09_resource_accounting.py \
  packages/structuraguard/tests/security/mapping/test_m09_inert_payloads.py -q --tb=short

env UV_CACHE_DIR=/private/tmp/structuraguard-m09-uv-cache make lint typecheck
env PYTEST_ADDOPTS='-q --tb=short' make test test-integration test-security docs test-build
# После исправления ссылок security report:
env UV_CACHE_DIR=/private/tmp/structuraguard-m09-uv-cache make docs
make test-build
```

| Проверка | Результат |
|---|---|
| Resource regressions до исправления | 2 failed |
| Resource regressions + context/graph/evidence после исправления | 45 passed |
| Inert payloads | 6 passed |
| Два новых security test files вместе | 8 passed |
| Полный узкий M9 suite | 147 passed |
| `make lint` | Passed, 334 файла |
| `make typecheck` | Passed, 330 файлов |
| `make test` | 2886 passed, 84 database tests deselected |
| `make test-integration` | 23 passed |
| `make test-security` | 632 passed |
| `make docs` | Passed, strict build после исправления ссылок |
| `make test-build` | Passed, offline wheel/sdist и distribution verification OK |
| Offline evaluation | JSON полностью совпадает с результатом тестового аудита |

Локальный журнал полного прогона: `/private/tmp/structuraguard-m09-security-gates.log`.
Полный набор использует ранее разрешённый запуск вне sandbox для watchdog,
loopback fixtures и offline build; security controls не отключались.
Strict docs сначала отклонил ссылки на Python-файлы вне docs tree; ссылки
заменены точными repository paths, конфигурация strict mode сохранена.

## Оставшиеся риски и непроверенные сценарии

- Это review текущего M9 diff, а не всех parsers/DB adapters/LLM/loader SDK.
- Живая PostgreSQL и её 84 opt-in tests не запускались: DB adapters не менялись;
  mapper проверен по catalog DTO и SQLite integration.
- Operation/allocation budgets консервативны, не обещают точный CPU time/RSS
  и не заменяют OS isolation. Нагрузка у максимальных ceilings не измерялась.
- Доверенное приложение отвечает за происхождение snapshots/scope, ограничения
  до построения DTO, свежесть DB schema перед загрузкой и безопасную обработку
  полного результата. Mapper не удостоверяет истинность переданных aggregates.
