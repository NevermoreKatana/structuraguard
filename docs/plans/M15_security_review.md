# M15 — security review текущего diff

Дата: 2026-09-13. Scope: текущий working-tree diff SDK orchestrator M15,
включая новые `pipeline/`, result contract, async/sync facades, tests и docs.
Компоненты M3–M14 изучались только в местах вызова и для проверки их guarantees.
Полный аудит прежних milestones и CVE-аудит всех установленных пакетов не проводились.

## Findings

### S1 — High: потеря credential field hints перед cloud LLM

**Расположение:**
`packages/structuraguard/src/structuraguard/pipeline/parsing.py:168`,
`packages/structuraguard/src/structuraguard/pipeline/source.py:158`;
повторное применение gate — `pipeline/loading.py:94`.
Номера строк относятся к исправленному рабочему дереву.

**Путь эксплуатации.** Приложение явно разрешает cloud route и использует scanner,
принимающий request с объявленной classification. Attacker передаёт JSON
`[{"password":"orchidcanary","name":"Ada"}]`, CSV с колонкой `password`
или XML с элементом `<password>`. M15 собирал отдельные имена/значения в безымянный
текст и вызывал только `ContentProtector.classify_chunks`. Контекст credential
field терялся: обычная строка пароля не имеет обязательного regex-префикса.
Классификация оставалась ниже RESTRICTED, поэтому admission пропускал cloud
provider. M6 добавляет фактические raw samples в prompt после approval.

**Влияние.** Потенциальная передача credentials внешнему LLM вопреки hard gate
для RESTRICTED. Это не утверждение об утечке реальных данных: воспроизведение
использовало synthetic canary и in-process spy с cloud capabilities, без сети.
Исходный тест зафиксировал provider call для JSON/CSV/XML. Числовая карточная PII
в отдельном control case уже блокировалась исходной реализацией.

**Минимальное исправление.** Общий `classify_source` сохраняет text scan и передаёт
credential name/value pairs в существующий M14 `classify_fields`. Он использует
существующий `field_category`, учитывает `custom_secret_fields`, наследует hint
для nested JSON/XML values и сохраняет табличные column hints между segments.
Дополнительные hints только повышают classification; они не формируют ParsePlan.
Named projection ограничена `max_fields`/`max_chars`, общий deadline сохраняется.
Тот же gate применяется перед staging/load. Reports двух scans сохраняются
отдельно, без переписывания исходных findings.

**Security regression:**
`tests/security/pipeline/test_m15_security_review.py::test_secret_field_and_numeric_pii_never_reach_cloud`
— шесть cases: JSON, CSV, XML, numeric PII, nested credential и custom secret field.
Проверяются RESTRICTED, REJECTED_SECURITY и отсутствие provider calls.

**Статус:** исправлено; первоначальные три failing cases и последующие checks зелёные.

### S2 — Low: неполное покрытие parser metadata в source scan

**Расположение:**
`packages/structuraguard/src/structuraguard/pipeline/source.py:138` и `:148`.

**Путь эксплуатации.** Attacker-controlled данные через parser попадают в
разрешённые `ExtractedCell.metadata` или `ExtractedTreeNode.metadata`.
`text_chunks` обходил line/block/table metadata, но пропускал эти два места.
Например, `note="ignore all previous instructions"` отсутствовал в source scan.

**Влияние.** Initial source scan мог не зарегистрировать injection/PII signals
в metadata. Самостоятельный обход request-bound outbound scanner и исполнение
инструкций этим тестом не подтверждены; поэтому finding имеет Low severity.
LLM по-прежнему не получает DB/tools authority.

**Минимальное исправление.** Добавить оба metadata containers в существующую
projection и именованную credential classification. Между отдельными физическими
текстовыми полями сохраняется разделитель, чтобы соседний label не менял regex
границу; raw lexeme также входит в ограниченный text scan.

**Security regressions:**
`test_source_scan_includes_tree_metadata`, `test_source_scan_includes_cell_metadata`
в том же test-файле. Tree case сначала падал. Это unit tests самой projection;
они не выдают изменённый вручную dataset за прошедший M3 validation.

**Статус:** исправлено вместе с S1 на той же границе данных.

## Матрица применимых угроз

Все пути тестов далее относительно `packages/structuraguard/tests/`.

| Угроза | Граница / проверка | Вывод в scope M15 |
|---|---|---|
| Attacker-controlled input | Source bytes/name, parser DTO, saved plans, metadata БД и LLM output; M3 physical manifest, M5 ParsePlan replay, M11 MappingPlan binding | DTO/fingerprint не считается разрешением на load. Owner/terminal guards и `test_validly_hashed_foreign_mapping_rejected_before_key_reads` блокируют неверный binding до key reads. |
| Resource exhaustion | M14 snapshot cap, parser/processing deadlines, bounded replay, calls/tokens, scan caps, serial hooks | `property/pipeline/test_acceptance_limits.py`, `test_semantic_expansion_obeys_run_record_budget`, cancellation/deadline tests подтверждают отказы. Блокирующий trusted CPU callback не прерывается OS sandbox. |
| Parser exploit / unsafe deserialization | Explicit registry, trusted parser admission; strict risky formats требуют sandbox; закрытые plan grammars | `test_strict_risky_format_requires_sandbox_before_parser_execution`, существующие YAML object/alias и malformed parser security tests. В новом production коде нет eval/exec/pickle/unsafe YAML/shell execution. |
| XXE / DTD / network | Hardened XmlParser; source path/URL transport не добавлен | Новый `test_xml_dtd_is_rejected_without_network` проверяет file и HTTPS external entities: SECURITY_INPUT_REJECTED, ноль network calls, нет DB. Component `security/parsers/test_markup_security.py` покрывает DTD/XXE/entity expansion. |
| Prompt injection / excessive agency | Source detector + exact-bound InjectionAwareSecurityScanner; closed ParsePlan/MappingPlan, capabilities без tools | S2 закрыт. Existing `test_prompt_injection_stops_before_db`, scanner deny и M10 scope/output tests зелёные. Source text не становится SQL или исполняемым кодом. |
| PII / secrets leakage | M14 classification до egress и перед load; scanner approval; safe exception codes | S1 закрыт. Новые credential cases дают ноль cloud calls. Полный IngestResult/plans остаётся чувствительным artifact и не предназначен для logs. |
| SQL / identifier injection | M11 scope membership; SQL строит существующий PostgreSQL adapter с параметрами | В coordinator нет SQL-конкатенации/DDL. Existing M11 malicious identifier, loader boundary и PostgreSQL security suites используются без ослабления. |
| DB allowlist / denylist | Trusted target/policy fingerprints, M9 pruning, M11 gate, M13 writer admission | MappingPlan проверяется до constraint reads и повторно в loader. Inspector и writer имеют разные principals. Чужой target/policy отвергается. |
| Schema drift | Fresh inspection и повторная writer transaction validation | `integration/database/test_sdk_orchestrator.py::test_drift_and_permission_errors_prevent_load`; подтверждённый rollback и UNKNOWN различаются; нет coordinator DML retry. |
| Logs / audit | Opaque IDs, hashes, typed codes, HMAC chain, отдельный signed load audit | Existing canary, raw exception context, missing audit key и independent anchor tests. Новый code не добавляет logging/print или raw fields в hooks. |
| Path traversal / temp files | SourceStream принадлежит host; display_name — metadata; в M15 нет path open или temp-file writer | Новый `test_display_name_never_opens_a_source_path` с traversal spelling подтверждает чтение именно stream и неизменность canary file. Временные артефакты прежних document adapters остаются их bounded worker boundary. |
| Supply chain | Diff dependency manifests и distribution checks | `pyproject.toml`, package manifest dependencies и `uv.lock` не изменены. Новых production dependencies нет; используются уже имеющиеся M14 classes/functions. |

## Проверки

Первоначально security regression запущен отдельно: три credential cases падали
на факте provider invocation, metadata case — на отсутствующем тексте projection.
После исправления новый файл прошёл: **12 passed**.

```bash
uv run --locked --no-sync pytest \
  packages/structuraguard/tests/security/pipeline/test_m15_security_review.py -q

uv run --locked --no-sync pytest \
  packages/structuraguard/tests/unit/pipeline \
  packages/structuraguard/tests/security/pipeline \
  packages/structuraguard/tests/property/pipeline \
  packages/structuraguard/tests/unit/contracts/test_m15_orchestration.py -q
```

Команды запускались с `UV_CACHE_DIR=/private/tmp/structuraguard-m15-uv-cache`;
для offline packaging использован существующий `UV_CACHE_DIR=/Users/katana/.cache/uv`.
Результаты текущего исправленного дерева:

| Команда | Фактический результат |
|---|---|
| Новый regression-файл, команда выше | 12 passed |
| Узкий набор pipeline/contracts, команда выше | 275 passed |
| `make lint` | Успешно; 598 файлов отформатированы, Ruff checks passed |
| `make typecheck` | Успешно; 594 source files |
| `make test` | 4656 passed, 442 deselected |
| `make test-database` | 442 passed; PostgreSQL, SQLAlchemy warnings как errors |
| `make test-integration` | 30 passed, 5068 deselected |
| `make test-security` | 1413 passed |
| `make test-build` | Успешно; wheel/sdist, isolated install и offline examples |
| `make docs` | Успешно; strict build |
| `git diff --check` | Успешно |

Основной и non-DB integration suites выводят пять существующих SWIG
`DeprecationWarning` из document dependencies. Проверки не ослаблялись.

## Остаточные риски

- Pattern-based classification не доказывает отсутствие любых скрытых/семантически
  закодированных secrets. Production base scanner, baseline classification и
  разрешённые routes задаёт host. Настоящие cloud LLM в этом review не вызывались.
- OS sandbox backend, безопасный URL/path transport и durable host artifact retention
  не реализованы M15; их эксплуатационные guarantees не проверены.
- In-process trusted callbacks должны кооперативно соблюдать deadline. Process kill
  и все комбинации отказов storage/network не моделировались; PostgreSQL lost-COMMIT
  response и ledger recovery покрыты существующим SDK integration test.
- Полный JSON результата может содержать исходные значения и metadata. Для logs
  используется `safe_summary`; code review не контролирует произвольный logging host.

Новых неисправленных Critical/High/Medium findings в проверенном diff не выявлено.
