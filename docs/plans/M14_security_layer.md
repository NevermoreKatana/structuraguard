# M14 — единый Security Policy layer и audit chain

Статус: частичная реализация M14 подготовлена к ручному commit и PR в `main`;
**полная приёмка milestone не выполнена**. Дата: 2026-09-13.
Поставлены resource limits, DLP/redaction, prompt-injection signals, DB maximum
policy, HMAC audit/transactional intent и parser runner boundary. OS isolation
и внешний outbox dispatcher требуют host backend. Commit и PR не создавались.

[Проверка приёмки M14](M14_acceptance.md) сопоставляет исходные критерии с tests.
Полная приёмка плана пока не подтверждена: implemented, interface-only и
незакрытые пункты отражены отдельно, включая расхождение audit compatibility.

[Security review diff](M14_security_review.md) содержит подтверждённые findings,
минимальные исправления и свежие проверки соответствующих trust boundaries.

Реализованы [централизованные лимиты](../resource-policy.md), [ADR 0030](../adr/0030-centralized-resource-limits.md):
frozen maxima, общие LLM/DB reservations, bounded source intake, parser deadline/
cancellation и safe terminal resource event. [Классификация и маскирование](../privacy-redaction.md),
[ADR 0032](../adr/0032-content-classification-and-redaction.md) покрывают B1–B3 и
safe summaries B4: bounded detectors, conservative classifications, configurable
custom patterns, отдельный encrypted map store с run/principal/TTL gates.
[Prompt-injection signals](../prompt-injection.md), [ADR 0033](../adr/0033-prompt-injection-signals.md)
реализуют C1, signal restrictions C2 и review bridge C3: обязательный base scanner,
original observations до masking, exact outbound scan, local-only/fallback restrictions
и NEEDS_REVIEW без generation/records. Автоматическая полная source/DLP/egress
composition, path/source permissions и export sink policy остаются отдельными
пунктами плана. [DB/audit/runners](../security-controls.md),
[ADR 0031](../adr/0031-security-audit-and-parser-boundaries.md) реализуют D1–D5
на стороне SDK; внешние outbox dispatcher и OS sandbox backend принадлежат host.
Проектируемые ниже имена files/tests для B уточнены в руководстве реализации;
map port расположен в `ports/privacy.py`, regression suite — `tests/security/privacy/`.

## Цель

Связать существующие детерминированные controls одной неизменяемой policy и
проверяемыми security decisions, добавить отсутствующие source/DLP boundaries и
HMAC audit chain, не создавая вторые parsers, validators, router или loader.

### Источники и границы исследования

Из ТЗ извлечены **только** `20. Информационная безопасность` со всеми подразделами,
`30.4. Security tests`, `32.5. Performance` и `M14. Security` командой
`python3 scripts/extract_spec_sections.py '20. Информационная безопасность' '30.4' '32.5' 'M14'`.
Прочитаны [контекст](../codex/PROJECT_CONTEXT.md), [индекс](../codex/SPEC_INDEX.md),
[модель угроз](../threat-model.md), применимые ADR и текущие security tests.

На этапе планирования индекс Security ещё ссылался на M12; теперь ссылка
исправлена на M14. Старые разделы модели угроз сохраняют исторические номера
milestones; актуальная traceability M14 добавлена отдельно. Для этого плана
используется **M14 Security из текущего ТЗ и задачи**, а статус существующего
control определяется кодом и regression evidence M2–M13. Номера `TB-01`–`TB-09`
и `SR-01`–`SR-29` модели угроз сохраняются. Исправление статусов входит в шаг D,
без переопределения нормативных требований.

### Наблюдаемое поведение до M14

Здесь и далее `src/` означает `packages/structuraguard/src/structuraguard/`,
`tests/` — `packages/structuraguard/tests/`. Имена новых файлов ниже — проектируемые.

| Область | Уже есть; владелец control | Чего не хватает в M14 |
|---|---|---|
| Source и parsing | `ports/source.py`: fingerprint-bound `SourceReader`, `ProbeContext`, `ParseContext`, `BatchOptions`; `parsers/registry.py`, `selection.py`, `execution.py`: frozen session, detection и повторная проверка batches | Общая `SecurityLimits`, trusted source/path policy, runtime открытия безопасного source и run-wide budget; `SourceReader` сам snapshot не создаёт |
| XML/YAML/HTML, документы | `parsers/builtin/xml.py`, `yaml.py`, `html.py`, `_documents.py`: XXE/DTD deny, inert HTML, YAML events без конструирования объектов, OOXML traversal/bomb guards, bounded document worker; `parsers/discovery.py`: descriptor-only allowlist | Policy admission и security events поверх результатов; document worker не является OS sandbox; strict documents и plugin activation сейчас отказывают без sandbox |
| PII и LLM | `profiling/pii.py`, `_patterns.py`: локальные PII-признаки и непонижение класса; `contracts/reports.py`, `ports/security.py`: `SecurityScanRequest`, `SecurityReport`, `SecurityApproval`, `SecurityScanner`, `PIIClassifier`; `llm/router.py`, `run.py`: routes, fallback и budgets; `structure/llm_analysis.py`, `mapping/semantic.py`: вызов scanner и проверка evidence | Production `SecurityScanner`, полное покрытие категорий §20.10, redaction/masking lifecycle, общая политика signals. Profiler evidence и тестовые scanners не дают DLP-гарантии |
| Plans и DB | `structure/validation.py`, `mapping/validation.py` и DB adapters: закрытые plans, provenance, scope, separate principals; M13 повторяет проверки в writer transaction, параметризует values, поддерживает staging/rollback | Связь решений с общей policy и audit; повторный SQL validator в security layer не нужен |
| Audit | `AuditEvent`, `AuditStore.append`; `contracts/loading.py::LoadAuditMetadata`, `database/_load_ledger.py`: redacted committed metadata в target transaction при включённом ledger | HMAC, sequence/verification/key lifecycle, security events до load и после rollback, обязательная audit capability для real load. Нынешний SHA-256 fingerprint не является HMAC |

После M14 отсутствие обязательной policy/capability, неполный scan или mismatch
evidence дают явный отказ перед соответствующим side effect. У каждого решения
есть control code, policy/version fingerprint, run/artifact binding и безопасное
audit evidence. Ошибка позднего этапа не разрешает partial load.

## Критерии приёмки

- A–D проходят вертикально: отрицательный input → typed decision → отсутствие
  запрещённого I/O/DML → redacted audit outcome. Таблицы controls ниже задают
  обязательные regression tests и остаточный риск для каждого control.
- Trusted composition owner задаёт immutable maximum policy; per-run selectors
  только сужают scope и уменьшают caps. Deny имеет приоритет, неизвестные modes,
  capability и classification не разрешают операцию.
- Отсутствие injection/PII signals само по себе не выдаёт разрешение. Разрешение
  связано с **точным** отправляемым payload, его полным bounded scan и актуальными
  routing/redaction fingerprints; чужой или повторно использованный report не
  разрешает другую операцию.
- Существующие parser safety, ParsePlan/MappingPlan validation, LLM restrictions,
  DB scope, staging и transaction controls продолжают исполняться в своих модулях.
  Security layer не снимает local guards и не превращает hash в authentication.
- Любой real load имеет core audit/HMAC event в той же transaction, что target
  DML; аудит обязателен и без idempotency key. Failure аудита до commit откатывает
  target. `dry_run` не создаёт target/staging mutations и может сохранять только
  redacted control-plane audit через отдельную capability.
- Sandbox scope M14 — interface, admission и contract suite. Без предоставленного
  совместимого runner strict/untrusted execution остаётся запрещённым; fake runner
  не подтверждает реальную OS isolation.
- Покрыты все сценарии §30.4, включая output/formula policy, и собраны измерения
  §32.5. Пропущенный обязательный integration test не считается evidence.

### Checklist фактической приёмки

Отметка означает выполнение исходного критерия целиком. Неполный criterion
остаётся открытым; его текст выше не ослаблен. Подробная матрица A1–D5,
«критерий → наблюдаемый test» и причины ограничений находятся в
[приёмке M14](M14_acceptance.md#m14-acceptance-criteria).

- [ ] **K1:** отдельные A–D boundaries покрыты, но общего
  source→DLP→routing→load→HMAC lifecycle нет.
- [ ] **K2:** frozen maxima, narrowing и deny precedence проверены; полного
  permissions snapshot с filesystem roots/routes пока нет.
- [ ] **K3:** exact payload/evidence binding и routing restrictions проверены;
  автоматическая полная DLP/redaction composition не подтверждена.
- [x] **K4:** существующие parser/plan/LLM/DB/staging guards сохранены;
  fingerprint не выдаёт authority. SQLite central-policy bypass исправлен
  и подтверждён `test_m14_constraint_policy.py`.
- [ ] **K5:** central default и signed mode подтверждены, но буквальное
  «любой real load» не выполнено: ADR 0031 сохраняет legacy mode и trusted
  configuration. Это явное расхождение с исходным критерием.
- [x] **K6:** runner interface/admission/contract suite реализованы; strict mode
  без совместимого backend отказывает. Fake backend не доказывает OS isolation.
- [ ] **K7:** filesystem safe-open, output/formula sink policy и измерения §32.5
  отсутствуют; все сценарии §30.4 пока не закрыты.

### Checklist передачи на ручной commit

- [x] Реализованный scope, незакрытые критерии и ADR exceptions обозначены явно.
- [x] M14-SR-01/02/03 исправлены; повторный review не выявил существенных findings
  в реализованном scope. Существующие tests/lint/typecheck не ослаблены.
- [x] Diff включает только код, tests/fixtures, документацию, config и lock-файл M14;
  suspected secret matches вручную подтверждены как synthetic test canaries.
- [x] Финальные docs/example, PostgreSQL и packaging gates подтверждены;
  результаты и пропуски записаны ниже.
- [x] `PROJECT_STATE.md`, приёмка и security review синхронизированы с итогом.
- [x] Не выполнялись `git add`, commit, push или создание PR.

## Затронутые контракты

### Единый authority и существующие владельцы проверок

Новые `contracts/security.py` и `security/policy.py` содержат `SecurityPolicy`,
`SecurityLimits`, effective snapshot и закрытое решение `allow/review/deny`.
Snapshot связывает версии policy, источника, parser trust, routing и DB scope;
исполнимые adapters и secrets в DTO не входят. `security/session.py` хранит
состояние только одного run: monotonic deadline, counters, terminal outcome и
ссылки на проверенное evidence. Никакого глобального mutable state и import I/O.

Policy компилируется в **существующие** `ParseContext`, format limits, `LLMBudget`,
inspection/mapping/validation/load policies. Effective cap — минимум trusted cap,
per-run ограничения и локального hard cap. Превышающий trusted maximum override
отклоняется, а не молча обрезается. Для record/physical-object/node/byte counters
явно фиксируются единицы; повторный scan не считается новой business record.
LLM calls/tokens резервирует существующий router, policy layer не ведёт второй
независимый счётчик. Общий deadline не сбрасывается при переходе между этапами.

Объединение решений монотонно: `deny` сильнее `review`, `review` сильнее `allow`.
Review завершает текущий run как `NEEDS_REVIEW`, без load; после ручного решения
нужен новый run. Нарушение security policy даёт `REJECTED_SECURITY`; технический
сбой scanner/store и deadline дают `FAILED`, отмена сохраняет cancellation.
Существующие локальные error codes не переименовываются: например,
`LLM_POLICY_DENIED`, `LLM_UNSAFE_CONTENT`, `DATABASE_SCHEMA_DRIFT` сохраняют смысл;
event отдельно указывает стабильный control code и итоговое действие.

`SecurityReport` уже имеет закрытые decision/status invariants. Не подделывать
`allowed` ради `NEEDS_REVIEW`: отдельное `SecurityDecision` хранит action и signals,
а scanner возвращает совместимый report только для своего purpose. Для нового
wire representation нужна явная версия и negative compatibility tests.

### Совместимость и архитектурные решения

- Переиспользовать `SecurityScanner.scan` и `PIIClassifier.classify`; не передавать
  raw dataset в aggregate-only `PIIClassificationRequest`. Full payload scan и
  masking — отдельная локальная операция до `SecurityScanRequest`. Scanner
  проверяет результат и **не меняет** уже approved payload.
- Общие PII recognizers вынести из `profiling/_patterns.py` в чистый
  `domain/pii_patterns.py`, оставив profiling-specific type inference на месте.
  Публичные profiler contracts и семантику `complete/unknown` сохранить; новые
  категории добавлять с явной версией, не заменяя старые имена.
- Сохранить `AuditEvent` и старый `AuditStore.append` как совместимый consumer
  interface. Новый versioned `ChainedAuditEvent` оборачивает typed event и
  недостающие §20.12 metadata; capability chain append/verify и transactional
  append отделена от обычного listener. Старый store без неё не разрешает load.
- ADR 0029 сохранял atomic load без ledger; ADR 0002 требует durable audit для
  real load. **Выбранное изменение M14:** audit обязателен для всех real
  `PostgreSQLLoader.execute`, idempotency остаётся отдельной optional capability.
  Это намеренное ужесточение runtime compatibility: прежняя конфигурация без
  audit отказывает с `AUDIT_DURABILITY_REQUIRED`. Нужны migration note и тест;
  скрытого legacy/no-audit bypass в production path нет.
- Не ослаблять [ADR 0012](../adr/0012-policy-aware-llm-routing.md): `RESTRICTED`
  cloud запрещён даже после masking. Политика модели угроз допускает отдельное
  разрешение masked surrogates, но M14 сохраняет действующий более строгий router.
  Per-destination rescanning не нужен для неизменных payload и approved route set;
  проверка exact evidence/capabilities перед каждым fallback остаётся обязательной.
- Ownership resource limits и совместимость оформлены в ADR 0030. Остальные
  permissions потребуют отдельного ADR; для chain scope, serialization, migration
  и атомарности запланирован `0031-transactional-audit-chain.md`.

## Шаги

Порядок: A → B → C → D. В каждом шаге сначала regression test, затем минимальная
реализация и узкая проверка. Общий compiler вводится в A, audit hooks — typed port;
durability этих hooks подтверждается только в D.

### A. Resource/file/source policy

| Control и граница | Владелец / добавление M14 | Deny-by-default | Regression tests | Residual risk |
|---|---|---|---|---|
| A1. Caller policy → run (`TB-01`) | `security/policy.py`, `session.py`: frozen maximum/effective snapshot, strict caps; исходные guards остаются у adapters | Нет trusted policy, неизвестный mode, forged DTO, расширение roots/routes/targets или cap → отказ до чтения/подключения | Новый `tests/security/policy/test_effective_policy.py`: absent/empty/unknown, nested mutation, override, другой run/fingerprint; spy parser/provider/writer = 0; property: narrowing никогда не расширяет permissions | Trusted host может обойти SDK или передать уже исполнявшийся adapter |
| A2. Filesystem/stream → bounded snapshot (`TB-02`, `TB-04`) | Новые `SourcePathPolicy`, `security/source.py`, `source/path.py`, `source/snapshot.py` поверх `SourceReader`; raw text не угадывается как path | Path без roots; absolute/parent escape, symlink в любом компоненте, directory/FIFO/device/socket, unsupported safe-open → `SOURCE_PATH_NOT_ALLOWED`. Descriptor-first `openat`/no-follow, fstat identity; immutable bounded snapshot до выдачи reader; expiry → `SOURCE_SNAPSHOT_EXPIRED` | Новый `test_source_boundary.py`: symlink swap/TOCTOU через barriers, changed file during snapshot, same bytes/hash, oversized/non-seekable stream, close/expiry/cancel, cross-run temp и quota; forbidden path не читается | Hard links/mount replacement и полномочия owner требуют deployment isolation; fingerprint supplied external reader не доказывает его честность; cleanup не стирает backups |
| A3. Поток → parser/normalized/LLM/DB stages (`TB-02`–`TB-07`) | Compiler передаёт более строгие caps в существующие contexts; `session.py` связывает общий deadline и накопленные counters | Нет конечного cap; bytes/records/columns/depth/text/calls/tokens/time превышены → stop, no partial load, cleanup. Deadline → `PROCESSING_TIMEOUT`; прочие limits → `SECURITY_LIMIT_EXCEEDED` | Новый `test_run_budget.py`: exact N/N+1 для каждого cap, split batches, повтор/fallback, cancellation/cleanup; сохранить parser JSON/text/delimited limits и `llm/test_m06_run_limits.py`; проверить отсутствие DML после late limit | Cooperative cancellation не прерывает произвольный native call; RSS/CPU/pids enforceable только подходящим runner |
| A4. Detection/container → parser admission (`TB-02`, `TB-03`) | Existing registry, safe markup, `SafePackage`; policy переводит warnings/conflicts в decisions/events, не парсит формат повторно | Conflict/security parser error не допускает более permissive parser fallback. Общие архивы disabled без explicit adapter; OOXML всегда проходит container guards; safe XML/YAML/HTML нельзя отключить | Существующие `parsers/test_markup_security.py`, `test_document_security.py`, `test_m04_probe_regressions.py`; новый `test_parser_policy_bridge.py`: warning→review/deny, no unsafe retry; traversal/symlink/bomb/executable/XXE, no actual external I/O | Parser zero-day остаётся; сам input с HTML/script сохраняется как inert data, export проверяется B4 |
| A5. Source → remote parser (`TB-02`, `TB-07`) | Existing `parsers/tika.py` exact snapshot/egress review; общая source/DLP policy добавляет veto, не заменяет transport guards | Без explicit Tika endpoint/policy, полного разрешённого source-bound review или при snapshot/format mismatch → no upload. LLM routing approval не разрешает parser egress | Existing `parsers/test_tika_security.py`, `test_tika_transport_logging.py`; дополнить `test_parser_policy_bridge.py`: центральный deny при локальной opt-in policy, secret в конце source, outbound bytes = 0 | Remote parser видит разрешённый source целиком; endpoint/retention контролирует deployment |

Файлы: новые `src/contracts/security.py`, `src/security/{__init__,policy,session,source}.py`,
`src/source/{__init__,path,snapshot}.py`; уточнения `src/ports/source.py` и wiring
`src/parsers/{registry,selection,execution}.py`. Snapshot transport ограничен
локальными path/bytes/stream; HTTP source downloader и общие archive adapters не
добавляются. `SecurityLimits` получает defaults §20.9: 50 MB, 1 000 000 records,
500 columns, depth 30, 5 000 000 text chars, 50 000 LLM tokens, 10 calls, 300 s.
Единицу MB явно закрепить как 1 000 000 bytes; более строгие действующие caps
сохраняются. Format-specific pages/line/nodes/container limits не заменяются
этими общими числами.

Проверка: `uv run --locked --no-sync pytest packages/structuraguard/tests/security/policy
packages/structuraguard/tests/security/parsers packages/structuraguard/tests/security/llm/test_m06_run_limits.py`
(одна команда). В тестах только synthetic payloads и временные allowed roots.

### B. PII/secrets classification и redaction

| Control и граница | Владелец / добавление M14 | Deny-by-default | Regression tests | Residual risk |
|---|---|---|---|---|
| B1. Source/labels/DB metadata → classification (`TB-01`, `TB-05`, `TB-07`) | `domain/pii_patterns.py`, `security/classification.py`; M8 evidence задаёт минимум, полный bounded scan проверяет конкретный egress payload, включая keys/comments/errors | Unknown/ambiguous/incomplete → `RESTRICTED` для egress/log/audit. Secrets всегда `RESTRICTED`; caller не понижает класс; отсутствие находок не означает `PUBLIC` | Новый `test_classification.py`: ФИО, email, phone, passport, ИНН, СНИЛС, bank card, API key/token/password/private key/DSN/header; нейтральные keys, Unicode, value на границе chunks; old M8 tests, skipped count, hostile classifier output | Косвенные identifiers, неизвестный secret и multilingual names дают false negatives; высокий class floor и minimization уменьшают последствия |
| B2. Caller regex → scanner (`TB-01`, `TB-07`) | Trusted pattern IDs в `security/patterns.py`; переиспользовать grammar `validation/_schema_regex.py` через общий pure helper `domain/bounded_regex.py`; custom patterns только добавляют findings | Backreferences/lookarounds, неоднозначные/вложенные repeats, unlimited expression/input/work запрещены; unsupported pattern/error/timeout не дают `clean` | Новый `test_pattern_budget.py`: catastrophic `(a+)+$`, long near-match, zero-width match, forbidden syntax, aggregate pattern count и N/N+1; сохранить `security/validation/test_schema_boundary.py`; отсутствие event-loop hang | Safe ASCII subset не поддержит часть пользовательских regex; arbitrary regex engine откладывается, новый dependency без обоснования не добавляется |
| B3. Raw → masked egress/protected map (`TB-07`, `TB-08`) | `security/redaction.py`, `ports/security.py`: separate masking-store/key ports, run-local opaque placeholders, exact redaction fingerprint; M10 minimization сохраняется | Raw restricted/secrets не уходят provider. Default irreversible redaction; reversible mode требует protected store + конечный TTL, без них deny. Map шифруется отдельным caller key, доступ только authorized restore того же run. Unknown token/run/expiry не восстанавливается | Новый `test_redaction.py`: repeated/overlapping values, placeholder spoof/collision, map отсутствует в prompt/report/audit, другой run, TTL/rotation, cleanup success/failure/cancel; пересчёт payload/approval при redaction change | Masked context сохраняет связь и косвенные признаки; host/store compromise раскрывает map; физическое удаление не гарантируется |
| B4. SDK diagnostics/output → sink (`TB-08`, `TB-09`) | `security/output.py`: typed safe summary и sink policy; существующие `exceptions.py`, contract redaction, HTTP diagnostics и `html_safe_json` сохраняются | Неизвестный sink/raw HTML/formula output без policy → deny. Secrets/raw PII не сериализуются для logger/listener/audit; control chars escaped; HTML text escaped, raw HTML запрещён без sanitizer; CSV/XLSX опасные строковые cells нейтрализуются согласно policy | Новый `test_output_policy.py`: canaries в repr/str/traceback/Pydantic views/warnings/caplog/listener; `= + - @`, tab/CR/whitespace prefixes, escaping idempotence, сохранение типизированных чисел; исходный parser value неизменен; unsafe sink не вызван | Caller может взять raw DTO и самостоятельно обойти safe renderer; CSV/XLSX приложения различаются, гарантия ограничена tested sink |

Файлы: новые `src/domain/{pii_patterns,bounded_regex}.py`,
`src/security/{classification,patterns,redaction,output}.py`; расширения
`src/ports/security.py`, `src/contracts/security.py`, `src/profiling/{pii,_patterns}.py`,
совместимый delegating helper `src/validation/_schema_regex.py`.
Никакой общей regex-«очистки» всего `model_dump`: sensitive raw DTO не становится
safe summary автоматически. Полные HTML/CSV/XLSX exporter products не входят
в M14; входит исполнимая sink policy с regression fixture, применяемая до записи.

Проверка: `uv run --locked --no-sync pytest packages/structuraguard/tests/security/privacy
packages/structuraguard/tests/security/profiling packages/structuraguard/tests/security/validation/test_schema_boundary.py
packages/structuraguard/tests/unit/contracts/test_m02_security_regressions.py`.

### C. Prompt-injection signals и LLM routing restrictions

| Control и граница | Владелец / добавление M14 | Deny-by-default | Regression tests | Residual risk |
|---|---|---|---|---|
| C1. Source/DB metadata/templates → LLM context (`TB-01`, `TB-05`, `TB-07`) | `security/signals.py`, `scanner.py`: bounded deterministic signals и typed event. Общие существующие literal checks вынести в чистые helpers, не копировать | Instruction/tool/credential/SQL authority запрос не исполняется. Suspicious signal → минимум review и no load; высокий риск → deny/no provider. Ошибка scan → no approval; clear text не отменяет другие guards | Новый `test_prompt_signals.py`: RU/EN, role markers, encoded/Unicode variants в пределах declared scan coverage, source keys/DB comments/templates, benign quoted examples; risk monotonicity, event без excerpts, provider/writer spies | Detector обходится paraphrase/obfuscation и даёт false positives; source/system separation, closed output и validators остаются основными controls |
| C2. Prepared payload → provider/каждый fallback (`TB-07`) | Production `SecurityScanner` через существующие M6/M10 call sites; `PolicyAwareLLMRouter` и `LLMRun` остаются единственным route/budget coordinator | No provider/policy/approval, unknown locality, changed payload/policy/redaction/caps → no egress. `RESTRICTED` cloud запрещён; `CONFIDENTIAL` cloud только при явной разрешающей policy + masking, `privacy_first` его запрещает. Fallback не расширяет approved route set | Новый `test_scanner_routing.py`: полный scan перед first byte, forged/self-consistent foreign evidence, changed destination, blocked primary/fallback = 0 calls, shared budgets, cancellation; reuse `llm/test_routing_policy.py`, `mapping/test_semantic_policy.py` | Allowed provider видит masked payload; caps честно декларирует trusted owner; SDK не контролирует provider retention |
| C3. LLM/plugin plan → executor/DB (`TB-03`, `TB-06`, `TB-07`) | Existing ParsePlan/MappingPlan validators и checked wrappers; M14 только связывает их outcomes с policy/run/artifacts | Нет tools/SQL/credentials/DB handles; unknown candidate/operator/identifier, changed source/catalog/plan или `review/deny` → no checked execution. Нельзя понизить plan/security failure до record warning | Существующие `structure/test_llm_plans.py`, `test_plan_execution_security.py`, `mapping/test_m11_validation_security.py`; новый `test_plan_policy_bridge.py`: самосогласованный foreign report, valid schema + unauthorized ID, no staging/DML | Корректная по schema и permissions рекомендация может быть семантически ошибочной; требуется validation/review по качеству |

Файлы: новые `src/security/{signals,scanner,llm}.py`; wiring
`src/structure/llm_analysis.py`, `src/mapping/semantic.py`, при необходимости
общие pure helpers для `src/llm/_content.py`. `SecurityApproval` создаётся только
после exact scan в trusted composition; его DTO доступность не является
криптографической capability против скомпрометированного host.
Signals исходного bounded content вычисляются до masking; их action сохраняется
после redaction. Финальный scan проверяет exact outbound payload, но исчезновение
опасной строки при masking не понижает ранее полученный `review/deny`.

Проверка: `uv run --locked --no-sync pytest packages/structuraguard/tests/security/injection
packages/structuraguard/tests/security/llm packages/structuraguard/tests/security/structure
packages/structuraguard/tests/security/mapping`.

### D. Database policy, audit events/HMAC chain и sandbox runner interface

Реализация: `contracts/{audit,database_policy,sandbox}.py`, `domain/database_policy.py`,
`security/{database,audit,audit_store,events}.py`, `ports/{audit,sandbox}.py`,
`database/audit.py`, `parsers/runners.py` и существующие DB/loader/parser guards.
Central selectors используют полный column scope; legacy flat selectors не
меняют поведение. Signed audit включается configuration/signer dependency;
central DB policy требует его по умолчанию. Existing ledger сохраняет reference
на signed head, bootstrap не мигрирует historical rows автоматически.
Точные contracts, compatibility и границы D4/D5 зафиксированы в ADR 0031.

Evidence: `security/database/test_m14_database_policy.py`,
`security/audit/test_m14_audit_chain.py`, `security/sandbox/test_m14_runner_boundary.py`,
`integration/database/test_m14_audit_database.py`. Таблица ниже сохраняет исходную
целевую матрицу; delivery/OS guarantees подтверждаются отдельной host приёмкой.

| Control и граница | Владелец / добавление M14 | Deny-by-default | Regression tests | Residual risk |
|---|---|---|---|---|
| D1. Policy/plan → inspector/writer (`TB-01`, `TB-05`, `TB-06`) | Existing target registry/scope/principal/catalog/plan checks; `security/database.py` связывает maximum policy и gates, DB adapter продолжает строить SQL | Unknown target, denylisted/system/internal object, расширение scope, общий inspector/writer principal, mismatch/drift или DDL → no load. Mandatory recheck в writer transaction; staging/audit tables доступны только dedicated ports | Existing `database/test_inspection_policy.py`, `loading/test_loader_boundary.py`, `mapping/test_m11_validation_security.py`; PostgreSQL tests: actual row/permissions state, SQL values и hostile names, target A/B, schema drift до и после DML, отсутствие runtime DDL | DBA grants/triggers/driver defects не устраняются общей policy; target fingerprint не является внешней аттестацией |
| D2. Security/report/load outcome → audit event (`TB-08`) | `security/events.py`, `contracts/audit.py`: closed metadata, audit-safe actor/service ID от trusted owner; `LoadAuditMetadata` включается как evidence | Неизвестный/free-form sensitive field, forged actor/run/status, нет required stage evidence → no append. Не логировать prompts/response/SQL/binds/DSN/map/PII; hash raw короткого secret не считать redaction | Новый `test_audit_events.py`: все поля §20.12, этапы без ещё вычисленного source/DB hash, canaries и log forging, counts/status/report mismatches, size bound; invalid event не достигает store | Даже hashes/metadata позволяют корреляцию; доступ и retention audit задаёт deployment |
| D3. Event/key/tail → append/verify (`TB-08`) | `security/audit.py`, signer/store ports; HMAC-SHA-256 поверх canonical bytes, append-only per-run chain | Нет key/safe tail, неизвестная версия/algorithm, wrong signature/sequence/chain/key ID → explicit failure; empty store не объявляется verified full history | Новый `test_audit_chain.py`: known vectors, mutate/reorder/middle delete/duplicate/cross-run splice, concurrent append, replay, key rotation/missing key, canonical Unicode/UTC, expected-head truncation | Без внешнего anchor незаметны suffix truncation/полное удаление; key holder способен переписать цепочку; HMAC не даёт non-repudiation |
| D4. Target transaction → durable audit/outbox (`TB-06`, `TB-08`) | Расширение существующей M13 ledger infrastructure; HMAC event и delivery intent в target transaction независимо от idempotency; `AuditStore.append` listener не заменяет capability | Нет transactional capability/key/layout/grants → `AUDIT_DURABILITY_REQUIRED` до DML. Append/sign failure до commit → rollback. Post-commit delivery error не изображает rollback; ambiguous commit → existing reconciliation без blind replay | Новый `integration/database/test_postgresql_audit_chain.py`: target/audit/outbox атомарны, permissions failure после DML, cancel до/в commit, duplicate delivery, restart, same/different key concurrent runs, rollback event failure → visible audit gap; reuse M13 outcomes/acceptance suites | Post-rollback audit может быть недоступен; terminal error сообщает gap. Exactly-once внешняя доставка не обещается; consumer deduplicates event ID |
| D5. Host → parser runner/plugin (`TB-03`, также `TB-02`, `TB-04`) | `ports/parser.py`/`ports/sandbox.py`, typed descriptor/request/capability/result; `parsers/runners.py` сохраняет existing core output validation | `InProcessParserRunner` только при explicit trusted input + adapter. Strict/untrusted требует `SandboxParserRunner` и полного capability profile; missing → `SECURITY_SANDBOX_REQUIRED` до read/import. Никакого host `EntryPoint.load`, fallback или доверия self-approved output | Новый `tests/contract_suites/sandbox.py`, `security/sandbox/test_runner_boundary.py`: no import/constructor, missing each capability, forged output, bounded IPC, timeout/kill/cleanup/cancel. Existing discovery/document strict tests; real OS suite отдельно | Interface/fake runner не обеспечивают isolation; kernel escape и malicious trusted adapter остаются вне гарантии SDK |

#### Формат и lifecycle audit chain

- Chain scope — trusted namespace + один opaque run ID; `chain_id` и effective
  policy fingerprint входят в подписанное тело. События разных runs не требуют
  общего lock. После binding target его identity не меняется в этой цепочке.
  Нет обещания глобального порядка между runs.
- Envelope: `chain_id`, последовательный `sequence`, `key_id`, версия формата,
  typed event/metadata, `previous_hash`, `current_hash`. Body для подписи содержит
  все эти поля **кроме обоих hash fields**. Формула §20.12:
  `HMAC-SHA-256(audit_key, previous_hash_bytes || canonical_json(body).encode('utf-8'))`.
  `previous_hash_bytes` — ровно 32 bytes; genesis — 32 нуля. HMAC имеет отдельный
  typed wire format, его нельзя выдавать за обычный `sha256:` artifact fingerprint.
  Canonical serializer использует существующие frozen contracts, UTC timestamps,
  закрытые поля и явную версию; unknown version не проверяется эвристически.
- Sequence, chain identity и `key_id` защищены подписью. Verify выполняет bounded
  streaming проверку, constant-time digest comparison и сверяет ожидаемый
  trusted head/length, если caller их передал. Без anchor результат явно означает
  только целостность предоставленного сегмента, а не полноту истории.
- Transaction-scoped lock сериализует append внутри chain до чтения tail;
  unique `(chain_id, sequence)` и event ID не допускают fork/duplicate. Tail
  берётся из committed store, а не из process memory. Повтор того же event ID
  допускается только с тем же body; другой body — отказ. Для load lock, append
  и target DML используют одну DB transaction, без отдельного auto-commit store.
  Для load-bound run chain store выбирается до первого event в той же target DB;
  pre-load events пишутся короткими отдельными transactions. Перенос tail из
  другого backend перед commit не допускается. Несовместимый store не выдаёт
  transactional capability. Standalone run использует явно переданный chain store.
- Caller-provided signer/key provider не читает environment при import; keys
  отсутствуют в DTO/repr/log/DB. HMAC default — stdlib `hmac`/`hashlib`, без новой
  crypto dependency. `key_id` выбирает trusted key, rotation продолжает цепочку
  через предыдущий digest. Retired verification key хранится по verification
  period; отсутствующий key не трактуется как успешная проверка.
- В event отражаются §20.12 run/time/SDK/source/DB/plan/provider/model/prompt/actor,
  status, validation summary, insert/update counts и decision. До вычисления
  source/DB/plan поля явно отсутствуют по stage schema, а не заполняются фиктивным
  hash. Terminal successful event требует соответствующего evidence.
- `LoadAuditMetadata` M13 не подписывается ретроспективно как будто историческая
  HMAC chain уже существовала. Явный admin bootstrap/migration versioned schema
  создаёт chain storage на базе существующей ledger infrastructure и связывает
  старые rows только migration metadata. Existing ledger v1 остаётся читаемым для
  reconciliation; runtime отказывает при несовместимом layout, без DDL/auto-upgrade.
  Idempotency marker ссылается на единственное committed audit event; второй
  независимый audit log для load не создаётся.
- После подтверждённого rollback terminal event пишется отдельно. Failure этой
  записи возвращает безопасный audit-gap outcome; callback не меняет outcome.
  Post-commit outbox delivery имеет event ID/durable retry evidence и semantics
  at-least-once. Изменяемый delivery state отделён от append-only signed events.

#### Граница sandbox interface

Capability profile требует non-root, read-only filesystem кроме отдельного temp,
no network, CPU/memory/pid limits, timeout, отсутствие host secrets и Docker
socket, cleanup при success/failure/cancellation. Untrusted descriptor задаёт
pinned artifact/version и allowlisted entry point; resolution/import/constructor
происходят только внутри runner. Request передаёт bounded source bytes/lease и
limits, не DB/provider/audit handles; response содержит bounded physical DTO и
safe exit/cleanup metadata, не неограниченные stdout/stderr.

Capabilities — declaration trusted adapter owner, не boolean от plugin. Core
повторяет schema/resource/provenance validation output через existing parser
boundary. Если OS sandbox не поставляется в M14, `SR-09` отмечается как
**interface covered / real isolation unverified**; strict path остаётся закрыт.

Файлы D: новые `src/contracts/audit.py`, `src/security/{database,events,audit}.py`,
`src/ports/{audit,sandbox}.py`, `src/parsers/runners.py`; точечные изменения
`src/ports/{parser,stores}.py`, `src/database/{loader,_load_transaction,_load_ledger,_load_ledger_schema,ledger}.py`,
`src/contracts/loading.py`. Обновить `docs/threat-model.md`, `docs/security.md`,
`docs/loader.md`, `docs/codex/SPEC_INDEX.md`, migration note и указанные ADR.

Проверка: `uv run --locked --no-sync pytest packages/structuraguard/tests/security/audit
packages/structuraguard/tests/security/sandbox packages/structuraguard/tests/security/database
packages/structuraguard/tests/security/loading packages/structuraguard/tests/security/stores`;
отдельно `make test-database` на реальном PostgreSQL с разными principals.

### Приёмочная матрица и performance

| Требование | Existing evidence, которое сохраняется | Новое evidence M14 |
|---|---|---|
| §30.4 XXE/YAML object/HTML script | `security/parsers/test_markup_security.py`, `test_m04_probe_regressions.py` | A4 policy bridge, B4 sink boundary; отсутствие actual I/O/constructor |
| Prompt injection | `security/llm/test_provider_boundary.py`, `security/mapping/test_semantic_policy.py`, `security/structure/test_llm_plans.py` | C1 signals/events, C2 production scanner + fallback; test fake scanner не заменяет runtime scan |
| SQL values / malicious column / unauthorized mapping | `security/mapping/test_m11_validation_security.py`, `security/validation/test_constraint_reader.py`, PostgreSQL security tests | D1 effective scope + D4 actual DB state и signed decision |
| Path traversal / archive bomb | `security/parsers/test_document_security.py`, unit discovery tests | A2 source roots/TOCTOU; A4 OOXML reuse. Общие архивы disabled, не объявлять их поддержанными |
| Oversized / excessive nesting | Text/JSON/delimited/document security tests, profiling/mapping resource tests | A3 cumulative per-run caps, no partial load после late violation |
| Secret leakage in logs | M2 contract regression, LLM/Tika HTTP/error tests, M8/M13 safe summaries | B1–B4 full DLP и D2–D4 audit canaries, отдельно защищённый masking store |
| Formula injection on export | `security/parsers/test_delimited_security.py::test_formula_like_cells_remain_raw_and_inert` проверяет только ingestion | B4 mandatory sink-level fixture; не приписывать parser test безопасность exporter |
| Schema drift before load | `integration/database/test_postgresql_load_outcomes.py`, `test_postgresql_load_acceptance.py`, M11 validation | D1/D4: drift, rollback и audit outcome связаны; отсутствие committed target rows проверяется запросом |
| Audit/sandbox из M14 и модели угроз | M13 transactional audit subset; M3/M4 strict refusal | D2–D5; `SR-21/22/28` chain/rotation/atomicity, `SR-09` зависит от real runner deployment |

Добавить `tests/performance/test_m14_security_overhead.py` и воспроизводимый
`scripts/benchmark_m14_security.py`: fixed synthetic TXT/CSV/JSON/OOXML corpora,
малые/большие batches, PII плотность, разрешённый и denied paths, fake LLM и
отдельный PostgreSQL прогон. Для paired baseline/current измерять p50/p95 latency,
records/s, peak RSS, DB queries/batch, technical-parser throughput и ParsePlan
execution throughput (§32.5); отдельно scan/HMAC latency и bytes scanned.

Baseline сохраняется с commit, runtime/OS, corpus fingerprint и конфигурацией.
Не выдумывать абсолютный SLA из §32.5: первый baseline фиксируется до изменения,
рост времени/памяти объясняется в acceptance report. Проверяемые обязательные
пределы: no full dataset materialization ради DLP/HMAC, bounded streaming state,
неповторный format parse, отсутствие DB query на каждую cell/record ради audit;
event rate ограничен stage/batch policy и накопленными counters. Cross-run chain
не сериализует все загрузки. Cap violation/rejection не требует дорогого scan
всего заведомо запрещённого input.

Перед завершением реализации: `make lint`, `make typecheck`, `make test`,
`make test-integration`, `make test-security`, `make docs`, дополнительно
`make test-database` для D. Затем diff review по `structuraguard-review` и security
review по `structuraguard-security`; acceptance report разделяет implemented,
interface-only и deployment-dependent evidence.

## Риски

- Разделение M13 audit/idempotency потребует versioned schema migration и
  ужесточения прежней конфигурации loader. Приоритет — fail-closed audit;
  rollout обязан сначала подготовить storage, keys и grants отдельным admin API.
- PII/injection detection имеет ограниченную полноту. Нельзя заменять им closed
  plans, authority separation, minimization или policy classification floor.
- Whole-payload scan, snapshot и audit I/O увеличивают latency/диск/RSS; нужен
  streaming и performance evidence, а не отключение scans для больших inputs.
- OS isolation, protected stores, grants/trigger review, key custody, retention и
  anchoring остаются обязанностью deployment. M14 interface не доказывает эти
  свойства за пределами протестированного backend.

Обязательный scope — controls A–D, compatibility/migration contracts, regression
matrix и измерения. Последующие улучшения: production OS/container runner,
общие archive adapters, полноценные exporters, внешнее anchoring/WORM backend,
NER/ML DLP, произвольные regex и vendor key-store integrations. До их появления
неподдержанная capability отклоняется, а не получает permissive fallback.

### Проверки при подготовке этого плана

- `make lint` — успешно: Ruff, 505 files.
- `make typecheck` — успешно: mypy, 501 source files.
- `make test-security` — **959 passed**. Первый sandbox-прогон дал 933 passed /
  26 failed; точечная диагностика показала недоступный Darwin document memory
  watchdog и запрет loopback socket. Полный повтор с необходимым доступом прошёл,
  без изменения кода или отключения tests. Использован writable
  `UV_CACHE_DIR=/private/tmp/structuraguard-m14-uv-cache`.
- `make docs` — strict build прошёл; внутренние ссылки проверены MkDocs.
- Review плана по `structuraguard-review` / `structuraguard-security`: проверены
  ownership controls, deny paths, compatibility, lifecycle, concurrency и scope
  evidence; существенных незакрытых замечаний к плану нет.

Эти результаты относятся к исходному планированию до реализации resource limits.
На том этапе полные `make test`, `make test-integration`, PostgreSQL
`make test-database` и performance не запускались. Актуальные проверки текущего
среза перечислены ниже; весь M14 ещё не принят.

## Проверки перед ручным commit {#m14-commit-checks}

Дата: 2026-09-13. Последнее изменение исполняемого кода — fix M14-SR-03;
после него менялась только документация. Канонические требования:
[M14 ТЗ][spec-m14], [§30.4][spec-security-tests], [§32.5][spec-performance].

Команды запускались из корня workspace. `uv run` использует `--locked --no-sync`.
Для pytest/lint/typecheck/docs применялся
`UV_CACHE_DIR=/private/tmp/structuraguard-m14-uv-cache`; `make lock-check test-build`
использовал обычный cache. Document workers, local HTTP fixtures и Testcontainers
работали с предоставленным доступом к локальному runtime. Платные/external LLM
API не вызывались.

| Фактически выполненная команда | Результат и срез |
|---|---|
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/security/database/test_m14_constraint_policy.py` | До M14-SR-03: 13 failed / 2 passed; после: 15 passed |
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/security/database packages/structuraguard/tests/security/validation/test_constraint_reader.py packages/structuraguard/tests/unit/database packages/structuraguard/tests/integration/test_sqlite_record_validation.py packages/structuraguard/tests/security/policy/test_llm_database_resources.py` | 283 passed; 3.83 s, после M14-SR-03 |
| `make lint typecheck` | Ruff: 570 files; mypy: 566 source files; passed после M14-SR-03 и повторно при подготовке передачи |
| `make test` | 4373 passed, 430 deselected; 130.37 s, после M14-SR-03 |
| `make test-integration` | 30 passed, 4773 deselected; 7.40 s, после M14-SR-03 |
| `make test-security` | 1391 passed; 32.64 s, после M14-SR-03 |
| `make test-database` | 430 passed, PostgreSQL 16/18; 211.24 s, финальный повтор после M14-SR-03 |
| `make lock-check test-build` | Lock: 109 packages; wheel/sdist, isolated installation и distribution verification OK на коде с M14-SR-03 |
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/docs` | 207 passed; 13.30 s, включая 7 M14 offline examples и 63 public docstring cases |
| `make docs`, `git diff --check` | Strict build и whitespace check passed после обновления plan/state/traceability |

В main/integration остались пять прежних SWIG deprecation warnings. Strict docs
может выводить прежний Material advisory и INFO о страницах вне nav; они не
превращались в ignored test failures. В раннем финальном review `test-build`
не прошёл isolated rebuild: во временном offline-cache не было hatchling.
Повтор с обычным cache прошёл; при подготовке передачи lock/build проверены снова.
Первый docs build этого шага нашёл четыре неверных новых anchors; добавлены
явные anchors, strict повтор прошёл без отключения link validation.

### Состав diff и ручной review

В передаваемом дереве **128 файлов: 50 tracked changes и 78 новых файлов**.
Новые файлы: 65 Python, 11 Markdown, 2 JSON fixtures. Scope — contracts/ports/domain,
security implementation, integration hooks parsers/LLM/DB, tests, ADR/руководства,
MkDocs, optional dependency metadata и lock. Production dependency добавлена
только как extra `security`: подробности в [supply-chain review](M14_security_review.md#m14-supply-chain).

AST-проверка 70 изменённых production Python files не нашла debug print/breakpoint,
eval/exec/pickle, os.system или shell=True. Credential-signature scan всего diff
дал пять locations: три synthetic DSN и два fake private-key markers в security
tests; они проверены вручную. Реальных secrets не обнаружено. Случайных временных,
binary/generated файлов среди tracked/untracked changes нет; `site/`, `dist/`
и caches остаются ignored и не входят в передаваемый diff. Эти проверки не
являются аттестацией произвольного upstream package или host окружения.

Повторный review исправленного кода проверил deny precedence, parameterized SQL,
fresh metadata, bounded preflight, typed errors/cleanup и совместимость legacy
target. Существенных незакрытых findings в реализованном scope не обнаружено.
Изменения публичных contracts и migration semantics описаны в руководствах и
ADR 0030–0033. Нет оснований объявлять весь M14 завершённым или обещать host isolation.

### Пропуски и причины

- Main, integration и security suites в документационном шаге повторно не
  запускались: они прошли после последнего runtime fix; исполняемый код и tests
  с того прогона не менялись. Сохранены точные результаты выше.
- Filesystem roots/safe-open/no-follow/TOCTOU, export/formula sinks, общий
  warning→review/audit bridge, central Tika DLP veto и полный
  source→DLP→routing→load→HMAC workflow ещё не реализованы. Их tests нельзя
  объявить passed; соответствующие K1–K3/K7 остаются открытыми.
- §32.5 не измерен: нет согласованного corpus и baseline p50/p95, throughput/RSS.
  Logical N/N+1 tests не подменяют performance измерения.
- Настоящий host sandbox, внешний outbox dispatcher с delivery/ack/retry и
  crash/restart целого signed ingest не проверены: в core нет такого host backend.
  SDK interface и durable store проверяются доступными contract/DB suites.
- Key/store compromise recovery, physical map erasure, provider retention и
  абсолютная полнота DLP/injection detection не доказываются SDK tests. Требуются
  key custody, independent anchor и deployment-specific verification.
- K5 сохраняет исключение ADR 0031 для legacy/trusted configuration; configurable
  signal restrictions следуют ADR 0033. Эти различия описаны явно, а не закрыты
  ослаблением исходных критериев.

[spec-m14]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m14-security
[spec-security-tests]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#304-security-tests
[spec-performance]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#325-performance
