# Модель угроз StructuraGuard

## Статус документа

Этот документ задаёт нормативную модель угроз для design baseline M0 и отмечает
ограниченный runtime subset, подтверждённый в M1–M3. Каждый control имеет два
независимых статуса:

- нормативный статус `REQUIRED`: control обязателен для приёмки затронутой
  возможности;
- статус реализации `IMPLEMENTED` с milestone evidence либо `PLANNED`, если
  реализация должна появиться в указанном будущем milestone.

В M1 статус `IMPLEMENTED` получили import/error controls. В M2 дополнительно
реализованы contract-level controls:

- import и constructors package code не читают environment/files, не обращаются
  к сети и не меняют process-wide event-loop, thread, signal или logging state;
- публичный `StructuraGuardError` ограничивает и санитизирует известные формы
  credentials, сохраняет только имя класса `cause` и делает `details`
  рекурсивно неизменяемыми.
- physical и semantic models разделены; source references проверяются через
  fingerprint-bound manifests/indexes;
- `ParsePlan` и `MappingPlan` используют закрытые декларативные схемы без
  Python, callbacks, shell и SQL, а executor ports принимают checked wrappers;
- LLM request contract не содержит tools, credentials, source/DB handles и
  ограничивает payload canonical JSON, размером и routing evidence; payload
  связан собственным fingerprint, nested tools/credentials/handles/SQL keys и
  известные credential/DSN/private-key canaries отклоняются до provider adapter;
- `contracts`, `domain` и `ports` не импортируют infrastructure и не выполняют
  I/O при импорте.

В M3 дополнительно реализованы parser registry controls: instance-local state,
frozen selection session, проверка content evidence и physical
`ExtractedBatch`, а также opt-in descriptor-only discovery allowlisted entry
points без host-side import или constructor.

Redaction ошибки является defense-in-depth и не распознаёт произвольный secret
под нейтральным именем. Caller обязан передавать только safe `message`, `details`
и `run_id`. Concrete format parser, sandbox runner, pipeline, DB, LLM, stores,
audit и end-to-end output controls остаются `PLANNED`. Отсутствие обязательного
control приводит к явному отказу; небезопасный fallback запрещён.

Модель конкретизирует security boundary defaults из
[ADR 0002](adr/0002-security-boundary-defaults.md). Scope продукта и форматы
задаёт [перечень требований](requirements.md), порядок pipeline —
[архитектура](architecture.md), а состояния и ошибки —
[публичный API](public-api.md). Полное техническое ТЗ остаётся источником
исходных требований; этот документ не повторяет его целиком.

## Контекст и границы ответственности

StructuraGuard — встраиваемая Python-библиотека, а не web-service. В scope этой
модели входят source detection, parser и plugin execution, normalized data,
mapping, optional LLM, inspection БД, staging, load, audit и выдача результата.

Trusted composition owner host application отвечает за аутентификацию и
авторизацию своих пользователей, TLS и network perimeter, выдачу DB/provider
credentials, immutable deployment policies, OS/container hardening, backup и
физическую защиту. Per-run caller, файл, metadata БД, untrusted plugin
descriptor/output, LLM и downstream consumer доверия не получают. Executable
adapter object, уже imported/constructed composition owner в host process,
находится внутри host trust domain; SDK не может изолировать код, выполненный до
его вызова.

Модель рассматривает следующих нарушителей и ошибки конфигурации:

- поставщик злонамеренного или повреждённого source;
- caller, передающий опасную policy, template, alias, rule или target;
- untrusted parser artifact/descriptor либо скомпрометированный dependency;
- LLM provider или LLM output, пытающийся расширить выданные полномочия;
- пользователь БД с ошибочно выданными grants и недоверенные DB metadata;
- consumer log, audit, report или export, интерпретирующий данные как код.

Полностью скомпрометированный host process, root/operator с доступом к памяти и
намеренное использование отдельного `migration_admin` находятся вне защитной
границы SDK. Это не отменяет fail-closed controls на собственных границах SDK.

## Цели безопасности

- **Конфиденциальность.** Raw PII, secrets, credentials и `RESTRICTED` values не
  покидают разрешённую trust domain и не попадают в logs, errors или audit.
- **Целостность.** Source, plan, schema и security decision связаны
  fingerprints; LLM и plugins не могут самостоятельно разрешить запись.
- **Минимальные полномочия.** Inspection и write используют разные DB users;
  writer ограничен allowlist, а DDL запрещён обычному pipeline.
- **Атомарность.** Запись проходит staging и transaction. `atomic` является
  default; cancellation и critical security rejection не смягчаются выбранной
  load policy и не оставляют частично применённый load.
- **Доступность.** Bytes, records, nesting, time, memory и внешние вызовы имеют
  конечные limits; превышение завершается контролируемым отказом.
- **Проверяемость.** Security decisions и изменения состояния оставляют
  redacted audit evidence с tamper-evident связью.

## Активы

- `A-01` — данные и schema production БД: целостность, ограничения, атомарность
  и доступ только к allowlist.
- `A-02` — credentials и ключи: inspection/writer DSN, provider credentials,
  encryption keys и `audit_key`.
- `A-03` — source, snapshot и normalized data: raw values, PII, secrets,
  provenance и source fingerprint.
- `A-04` — mapping artifacts: candidates, immutable `MappingPlan`, его version и
  fingerprint, validation evidence, database fingerprint и `target_identity`.
- `A-05` — staging, quarantine и temporary storage: изоляция runs, retention,
  cleanup и отсутствие residual data.
- `A-06` — LLM context и output: минимизированный prompt, masking и strict
  schema result.
- `A-07` — audit trail: полнота событий, порядок, redaction и tamper evidence.
- `A-08` — parser/plugin runtime: CPU, memory, time, filesystem, network и
  provenance adapter.
- `A-09` — reports и exports: целостность результата и безопасная передача
  недоверенных values downstream consumers.

## Недоверенные входы

До детерминированной проверки недоверенными считаются:

- paths, bytes, streams, строки, API data и программные `dict`/`list` records;
- filename, extension, MIME, encoding, archive entries и document metadata;
- TXT, LOG, CSV, JSON, XML, HTML, YAML, XLSX, PDF и DOCX content;
- schema/table/column names, comments, constraints, samples и иные DB metadata;
- aliases, declarative rules, mapping templates, per-run configuration и
  caller-provided identity;
- untrusted parser descriptor/artifact, manifest, dependencies и любой
  parser/adapter output;
- LLM prompt fragments, provider responses, tool-like text и output;
- values, errors и callbacks, передаваемые в stores, listeners и exporters.

Недоверенный input остаётся данными. Наличие в нём SQL, shell commands, template
syntax или инструкций для модели не даёт ему authority и не запускает код.

## Классификация и маршрутизация данных

До любого remote LLM call и до каждого fallback должна действовать data-routing
policy. Неизвестная или неоднозначная классификация обрабатывается как
`RESTRICTED` для egress, logs и audit.

- `PUBLIC`: egress возможен только через явно configured provider/data policy.
- `INTERNAL`: передаются лишь необходимые поля; remote egress требует явного
  разрешения.
- `CONFIDENTIAL`: remote egress по умолчанию запрещён; разрешение требует
  masking и минимизации.
- `RESTRICTED`: raw values не отправляются provider и не входят в logs, errors,
  reports или audit. Masked surrogates допустимы только по отдельной явной
  policy.

PII detection должен как минимум учитывать ФИО, email, телефон, паспорт, ИНН,
СНИЛС, банковские карты и caller-provided regex patterns. API keys, tokens,
passwords, private keys и credentials всегда рассматриваются как `RESTRICTED`.
Detector не считается безошибочным: сомнение блокирует egress и создаёт
security decision, а не включает permissive fallback.

### Masking map и ключи

Control status: `REQUIRED`; implementation status: `PLANNED` (`M8`, `M12`).

- Masking выполняется до provider boundary. Placeholder не должен раскрывать
  исходное значение и должен быть стабилен только в пределах требуемого run.
- Reversible masking map хранится отдельно от prompts, reports и audit в
  caller-provided protected store. Доступ разрешён только операции обратной
  подстановки для соответствующего run.
- Store применяет явный минимальный TTL и удаление по завершении установленной
  business need. Значение «хранить бессрочно» запрещено как default.
- Encryption/HMAC keys поступают через caller-provided ports, не сохраняются
  рядом с ciphertext или audit events и не читаются при import пакета.
- Rotation сохраняет `key_id`, но не key material. Audit keys удерживаются не
  меньше настроенного verification period; masking keys — не дольше TTL map.
- Expiration, deletion и cleanup фиксируются metadata без raw values. Ошибка
  cleanup создаёт security event и не скрывается как успешное удаление.

Остаточный риск: PII detector может дать false negative, а компрометация host
process или caller-provided store раскрывает доступный ему map/key material.

## Принятые security decisions

### `D-06`. LLM routing и privacy

Принято `deterministic-first`. LLM является optional adapter и вызывается только
при configured provider и явно разрешающей provider/data policy. Классификация,
минимизация и masking выполняются до primary call и заново проверяются перед
fallback. Fallback не может расширить разрешённый egress.

### `D-07`. Изоляция parser

`InProcessParserRunner` разрешён только для явно trusted input и adapter.
Strict/untrusted run обязан использовать `SandboxParserRunner`. Если sandbox
недоступен, run завершается с `SECURITY_SANDBOX_REQUIRED`; автоматический запуск
того же parser in-process запрещён.

Object-based registration означает trusted host code. Для untrusted parser
принимается только декларативный descriptor; artifact resolution, import и
constructor происходят внутри sandbox. Для других untrusted executable
extension types sandbox protocol в MVP не обещан.

### `D-09`. Audit, stores и retention

Audit store, masking map, key providers и retention задаёт caller через явные
ports. SDK не создаёт скрытое глобальное хранилище и не владеет deployment
secrets. Для real load durable core audit/outbox record атомарен с target
transaction; внешний listener не заменяет эту capability. Raw `RESTRICTED`
values не входят в audit независимо от backend.

## Trust boundaries и abuse cases

### `TB-01`. Caller → SDK

**Активы:** `A-01`–`A-04`, `A-07`.

**Abuse cases:** type confusion между path и raw text; path вне разрешённого
root; per-run расширение source/target policy; подмена target через identifier;
самосогласованный незарегистрированный target; executable content в aliases,
rules или mapping templates; reuse plan с другим source или закрытым snapshot.

**Controls:** `REQUIRED`; typed contract boundary `IMPLEMENTED` (`M2`), runtime
registry/policy enforcement `PLANNED` (`M9`, `M12`).

- Source type, target, policy и extension inputs проходят typed validation.
  Неизвестные поля, modes и policy values отклоняются deny-by-default.
- Target выбирается только из instance registry trusted composition owner.
  Immutable maximum allowlist/denylist и `target_policy_fingerprint` входят в
  run snapshot; per-run selectors MAY только сузить scope.
- Raw text не определяется эвристически как path. Path transport требует
  immutable `SourcePathPolicy` trusted composition owner; per-run input не
  расширяет `allowed_roots`. Без policy path отклоняется.
- `SourceAnalysis` связывает live bounded snapshot lease с source fingerprint;
  mapping artifacts immutable и versioned. Expired/closed lease не исполняется.
- Aliases, declarative rules и templates являются данными. `eval`, `exec`,
  unsafe YAML,
  `pickle`, macros, source code и SQL-file execution запрещены.
- Caller не может отключить обязательные strict-mode caps или security gates.

**Detection и evidence:** validation issues, policy/version, source fingerprint,
target и redacted reason фиксируются с `run_id`.

**Security outcome:** invalid input не достигает parser, LLM или writer.
Fingerprint mismatch не пишет в БД и возвращает
`SOURCE_FINGERPRINT_MISMATCH`. Запрещённый path или закрытый lease дают
`SOURCE_PATH_NOT_ALLOWED` либо `SOURCE_SNAPSHOT_EXPIRED`.

**Residual risk:** caller, уже имеющий OS/DB/key authority, может обойти SDK и
выполнить действие напрямую; это ограничивается deployment controls вне SDK.

### `TB-02`. Filesystem/stream → detector/parser

**Активы:** `A-03`, `A-05`, `A-08`.

**Abuse cases:** spoofed extension/MIME; malformed container; parser exploit;
path traversal, symlink/TOCTOU race или special file; XXE/DTD; YAML object
construction или alias bomb; HTML active content; archive bomb; macro/executable
payload; resource exhaustion.

**Controls:** `REQUIRED`; registry detection boundary `IMPLEMENTED` (`M3`),
format-specific parsing и sandbox isolation `PLANNED` (`M4`, `M12`).

- Detection сверяет content/signature, declared MIME и extension. В M3
  metadata mismatch возвращает typed warning code, а несовместимые
  strong signals — typed `PARSER_FORMAT_CONFLICT`. Создание `SecurityEvent`
  и routing через review/rejection policy остаются `PLANNED` вместе с
  orchestrator; extension не становится trusted signal.
- Path открывается descriptor-first внутри allowed root без follow symlink.
  Regular-file type, identity и limits проверяются после open; те же bytes
  используются для parsing и fingerprint. Directory, device, FIFO, socket и
  symlink отклоняются.
- Limits применяются до parse, во время streaming/batching и к normalized output.
  Частично полученные records не переходят к load после превышения limit.
- XML parser запрещает external entities, DTD и network; ограничивает depth,
  nodes, text length и time.
- YAML использует только safe loader, не создаёт Python objects и ограничивает
  aliases, nesting и aggregate size.
- HTML parser не выполняет JavaScript, не загружает iframe или external
  resources. Raw HTML не рендерится без sanitization.
- Archive adapter, если он явно включён, ограничивает archive/extracted size,
  file count, nesting и compression ratio; отклоняет absolute/parent paths,
  symlinks и executable entries. Без adapter архив отклоняется.
- XLSX, DOCX и PDF обрабатываются как недоверенные containers; macros, embedded
  executables и external fetch не выполняются.
- PDF без text layer завершается контролируемо с `PARSER_NO_TEXT_LAYER`; OCR не
  включается автоматически.

Числовые baseline defaults `SecurityLimits` канонически задаёт
[требование `SEC-007`](requirements.md). Здесь они применяются как обязательный
control, но не дублируются. Format-specific limits для line length, pages,
archive entries, XML nodes, CPU, memory и pids также должны иметь конечные safe
defaults. Strict mode задаёт absolute caps, которые caller не может повысить.
Любой override валидируется до чтения source.

**Detection и evidence:** actual/declared format, mismatch, parser ID/version,
source hash, сработавший limit и sanitized parser outcome. Raw payload не
записывается в event.

**Security outcome:** опасный или превышающий limits input не создаёт plan и не
доходит до staging. Отказ получает machine-readable `error_code`;
кроме закреплённых M0 кодов остальные security codes должны быть определены в M2
до реализации.

**Residual risk:** safe configuration не исключает parser zero-day; отдельная
изоляция `TB-03` ограничивает blast radius.

### `TB-03`. SDK core → parser runner/plugin

**Активы:** `A-02`, `A-03`, `A-05`, `A-08`.

**Abuse cases:** host импортирует untrusted plugin до sandbox; descriptor
подменяет artifact/entry point; malicious plugin code; dependency substitution;
filesystem или network access; secret discovery; fork/pid exhaustion; forged
normalized output; unsafe in-process fallback.

**Controls:** `REQUIRED`; registry/discovery boundary `IMPLEMENTED` (`M3`),
sandbox execution `PLANNED` (`M12`).

- Registry instance-local и frozen на время run. Adapter ID, version/fingerprint
  и trust decision входят в reproducibility metadata.
- Object registration разрешён только для trusted executable code, уже
  загруженного composition owner. Normalizer, validator, business rule, listener,
  DB и LLM adapter objects имеют ту же host-trust semantics.
- Untrusted parser принимается только как декларативный descriptor. Resolution
  pinned artifact/module, import и constructor выполняются внутри
  `SandboxParserRunner`; host-side import/constructor отсутствует.
- Entry-point discovery запускается только явным вызовом с непустым allowlist и
  exact group `structuraguard.parsers`. Недоверенные metadata ограничены и
  нормализуются до descriptor; `EntryPoint.load()` при discovery запрещён.
- Filesystem `.dist-info` читается только через pinned directory/file
  descriptors, fixed filenames, `O_NOFOLLOW`, regular-file checks и hard byte
  caps. ZIP/custom metadata providers, symlink/FIFO/device и небезопасная
  платформа отклоняются до обращения к metadata getters.
- Сбой metadata одной allowlisted distribution отражается typed issue без raw
  metadata и не останавливает discovery остальных distributions. Успешный
  результат и issues возвращаются вместе, поэтому partial discovery не
  изображается полностью успешным.
- Strict/untrusted execution допускается только через `SandboxParserRunner`.
- M3 `activate_plugin()` является refusal boundary: известный descriptor без
  sandbox даёт `SECURITY_SANDBOX_REQUIRED`, не вызывая import/load target.
- Sandbox запускается non-root, без network, secrets и Docker socket, с
  read-only filesystem кроме isolated temporary directory, CPU/memory/pid limits
  и timeout. Cleanup выполняется при success, failure и cancellation.
- Plugin output повторно проходит schema, provenance и resource validation на
  стороне core. Plugin не может пометить собственный output безопасным.
- Dependencies и происхождение plugin должны быть pinned и проверяемы; plugin
  не получает DB connection, provider credentials или audit keys.
- Untrusted executable extension другого типа отклоняется, пока для него не
  определён отдельный sandbox protocol.

**Detection и evidence:** adapter fingerprint, runner type, trust mode, limits,
timeout/exit status и cleanup outcome без stdout/stderr secrets.

**Security outcome:** отсутствие требуемого sandbox даёт
`REJECTED_SECURITY`/`SECURITY_SANDBOX_REQUIRED`; fallback in-process не
выполняется.

**Residual risk:** sandbox зависит от OS/runtime и не гарантирует защиту от
kernel escape. Trusted in-process extension имеет полномочия host process, а
SDK не может отменить side effects import/constructor, выполненные composition
owner до регистрации. Ошибочная trust policy остаётся высоким deployment risk.

### `TB-04`. Pipeline → snapshot/temp/quarantine

**Активы:** `A-03`, `A-05`.

**Abuse cases:** path traversal; symlink race; чтение данных другого run;
переполнение диска; преждевременный cleanup либо забытый caller lease;
оставшиеся после failure raw values; смешение quarantine и trusted source.

**Controls:** `REQUIRED`; implementation `PLANNED` (`M3`, `M11`, `M12`).

- Каждый run использует отдельный namespace/directory с restrictive access.
  User-controlled names не участвуют в path construction.
- Symlinks и выход за разрешённый root отклоняются. Quarantine не становится
  trusted source без нового явного run и повторной validation.
- Bytes/quota/retention ограничены. Cleanup обязателен на success, failure,
  timeout и cancellation; cleanup должен быть idempotent.
- Snapshot связан с source fingerprint. Подмена после analysis не меняет
  содержимое, разрешённое для plan/execute.
- Успешный `inspect_source` передаёт bounded lease только через
  `SourceAnalysis`, не раскрывая internal path. Lease имеет конечный expiry,
  поддерживает idempotent `aclose()` и после close/expiry не допускает execute.
  Combined `ingest` закрывает lease сам.
- Sensitive storage предоставляет caller через port и защищает в соответствии с
  data class; SDK не создаёт постоянный cache при import.

**Detection и evidence:** storage backend ID, run namespace, size, retention
decision и cleanup status. Paths и raw values в audit не публикуются.

**Security outcome:** нарушение изоляции или quota останавливает run до load;
закрытый lease даёт `SOURCE_SNAPSHOT_EXPIRED`. Cleanup failure видим как
security event, а не скрывается success status.

**Residual risk:** обычное удаление файла не гарантирует physical erasure на
copy-on-write disks и backups; требуемый уровень уничтожения задаёт deployment.

### `TB-05`. SDK → DB inspector

**Активы:** `A-01`, `A-02`, `A-04`.

**Abuse cases:** metadata-based prompt/identifier injection; обход schema
filters; чтение system catalogs или rows сверх задачи; утечка DSN через error;
использование writer credentials для inspection; catalog БД A используется для
writer session БД B.

**Controls:** `REQUIRED`; implementation `PLANNED` (`M5`, `M12`).

- `schema_inspector` является отдельным read-only principal и читает только
  необходимую metadata разрешённых schemas. Он не получает write grants.
- System schemas и неразрешённые objects закрыты deny-by-default. Metadata,
  comments и identifiers считаются недоверенными и не получают authority в LLM.
- DB adapter использует bounded queries/timeouts. DSN credentials и samples с
  sensitive values не попадают в logs, errors, reports или prompts.
- Catalog получает deterministic database fingerprint, используемый при
  validation и перед execute.
- Catalog и plan получают non-secret immutable `target_identity`, связывающий
  dialect, database/cluster identity и `target_policy_fingerprint` trusted
  composition owner. Совпадение schema names не доказывает совпадение target.

**Detection и evidence:** database fingerprint, adapter/version, разрешённые
schemas и redacted principal identity; DSN и raw metadata values исключены.

**Security outcome:** попытка выйти за inspection scope отклоняется до mapping;
inspector не способен изменить production data по выданным SDK grants.

**Residual risk:** ошибочно выданные DBA grants нельзя полностью компенсировать
на уровне библиотеки; deployment обязан проверять effective privileges.

### `TB-06`. Loader → staging/production DB

**Активы:** `A-01`, `A-02`, `A-04`, `A-05`, `A-07`.

**Abuse cases:** SQL/identifier injection; запись вне allowlist; DDL или DELETE;
writer указывает на иной endpoint/database; schema/source drift между analysis
и execute; partial commit; audit потерян после commit; cross-run staging;
rollback/cleanup failure; скрытые side effects DB triggers.

**Controls:** `REQUIRED`; implementation `PLANNED` (`M9`, `M11`, `M12`).

- `data_importer` отделён от `schema_inspector` и имеет только необходимые
  `SELECT`/`INSERT`/`UPDATE` grants на allowlisted staging, target и
  transactional audit/outbox tables. `migration_admin` не передаётся обычному
  pipeline.
- Schema, table и column сверяются с allowlist и reflected catalog. Identifiers
  не берутся напрямую из source, template или LLM. Values всегда
  parameterized; SQL формирует только DB adapter.
- Target denylist имеет приоритет над include/allowlist. `pg_catalog`,
  `information_schema`, `pg_toast*`, `pg_temp_*` и SDK-owned internal
  objects не являются mapping/load targets; per-run input не ослабляет denylist.
  Staging и audit/outbox доступны только dedicated ports.
- `CREATE`, `ALTER`, `DROP`, `TRUNCATE`, `GRANT` и `REVOKE` недоступны ordinary
  ingest capability. `propose_schema` возвращает только декларативное
  предложение и не получает административное соединение.
- Writer session перед staging и commit подтверждает тот же `target_identity`,
  зарегистрированный `target_policy_fingerprint`, per-run narrowing, valid
  immutable plan, source/database fingerprints и validation evidence.
- Staging изолирован по `run_id`. Atomic transaction является default; failure и
  timeout вызывают rollback, после чего staging очищается.
- `insert_only` и `upsert` используют явную record identity и idempotency
  evidence. Replay подтверждённого run не должен создавать неконтролируемые
  дубликаты; неопределённый commit запрещено молча повторять как новый load.
- `dry_run` не оставляет persistent source/target/staging data-plane
  mutations. Если используется transient rollback-only staging для DB
  constraints, он всегда завершается rollback и cleanup до возврата результата.
- `auto_safe` run-level gates всегда veto. `quarantine_invalid` и
  `best_effort` требуют explicit opt-in и применяются только к recoverable
  record-level failures; они не смягчают plan/dataset structural, security,
  identity, transaction или system failure.
- Durable core audit/outbox record участвует в transaction target mutations.
  Без capability или preflight availability `LOADING` не начинается.
- Cancellation закрывает resources и выполняет необходимый rollback/cleanup.
  Результат не сообщает ложный success или rollback, если он не подтверждён.

**Detection и evidence:** plan/source/database fingerprints, target identity,
allow/denylist version, staging namespace, counts, transaction/audit/rollback/
cleanup outcome и security decision. SQL text, bind values и credentials в
audit не входят.

**Security outcome:** drift возвращает `SOURCE_FINGERPRINT_MISMATCH` либо
`DATABASE_FINGERPRINT_MISMATCH`; другой DB target —
`DATABASE_TARGET_MISMATCH`; DDL — `DDL_FORBIDDEN`; target вне allowlist —
`TARGET_NOT_ALLOWED`; отсутствие atomic audit —
`AUDIT_DURABILITY_REQUIRED`. До commit это не меняет основные таблицы. Failure
после начала load приводит к rollback и terminal outcome по public API.

**Residual risk:** DB triggers, rules и внешние observers могут создавать side
effects внутри разрешённой операции; adapter contract tests и DBA review должны
учитывать их отдельно. Overprivileged writer увеличивает impact ошибки.

### `TB-07`. Semantic analyzer/mapper → LLM provider

**Активы:** `A-02`–`A-04`, `A-06`, `A-07`.

**Abuse cases:** prompt injection из source или DB comments; PII/secrets egress;
automatic provider fallback; cost/token exhaustion; model возвращает SQL,
неизвестный target или tool request; provider сохраняет разрешённый context.

**Controls:** `REQUIRED`; contract boundary `IMPLEMENTED` (`M2`), provider,
routing и runtime enforcement `PLANNED` (`M6`, `M8`, `M9`, `M12`).

- Deterministic mapping выполняется первым. LLM disabled без explicit provider
  и provider/data policy; отсутствие provider не является причиной неявного
  cloud fallback.
- Source content и DB metadata отделены от system instructions и помечены как
  data. Инструкция внутри data не получает authority.
- Classification, minimization и masking происходят до call и перед каждым
  fallback. Raw `RESTRICTED` content, credentials и reversible map не уходят.
- `LLMRequest` принимает typed `SecurityApproval` с полным разрешающим
  `SecurityReport`; canonical report, payload, classification, routing и
  redaction fingerprints проверяются как единая lineage до provider call.
- Structured payload keys нормализуются по punctuation/acronym/camelCase перед
  denylist; известные bare API-token/credential/DSN canaries и Unicode
  log-forging controls отклоняются на contract boundary. Persisted issues не
  содержат free-form messages, audit IDs используют точные формы
  `event-<UUID|ULID>`/`run-<UUID|ULID>`, а terminal audit
  связывает конкретный allowed/blocked security report и не допускает evidence
  из будущего.
  Content-aware DLP остаётся runtime control.
- LLM не получает tools, DB connection, DSN, secrets или прямой filesystem
  access. Модель не создаёт executable SQL.
- Модель MAY предложить strict-schema `ParsePlan` из bounded physical context
  либо ранжировать переданные DB candidates. Оба output недоверенны и проходят
  соответствующую independent deterministic validation.
- Tokens, calls и processing time ограничены `SecurityLimits`. Provider/model и
  policy capabilities проверяются до egress.

**Detection и evidence:** provider/model, prompt version, policy/masking version,
token/call counts, candidate-set fingerprint, validation outcome и suspicious
content event. Prompt, response и raw values не входят в audit.

**Security outcome:** подозрительный content приводит к `NEEDS_REVIEW`; высокий
риск или policy violation — к `REJECTED_SECURITY`. Ни один из этих outcomes не
переходит к loading. Конкретные дополнительные error codes должны быть
определены в M2 до реализации и не заменяются свободным текстом.

**Residual risk:** provider видит явно разрешённый masked context; masking может
сохранить косвенные identifiers, а модель остаётся вероятностной. Поэтому LLM
output никогда не является единственным security control.

### `TB-08`. SDK → stores/listeners/logs

**Активы:** `A-02`, `A-03`, `A-05`, `A-07`.

**Abuse cases:** secret/PII leakage; log forging через control characters;
tampering, deletion или reorder audit; target commit без durable audit; callback
пытается изменить security decision; общий store смешивает runs; ключ хранится
рядом с event chain.

**Controls:** `REQUIRED`; implementation `PLANNED` (`M12`).

- Redaction выполняется до любого logger, listener, error, report и store port.
  Structured fields ограничены по размеру; untrusted strings не формируют log
  format и экранируют control characters.
- Audit содержит `run_id`, timestamp, SDK version, source hash, database и plan
  fingerprints, provider/model и prompt version при LLM call, caller identity,
  status, validation summary, counts, security decision и chain hashes.
- Passwords, credential-bearing DSN, Authorization headers, API keys, private
  keys, masking map и raw `RESTRICTED` values никогда не входят в audit.
- Tamper-evident chain вычисляется как
  `HMAC(audit_key, previous_hash || canonical_json(event))`. Key material
  хранится отдельно; event содержит только подходящий `key_id`.
- Для real load минимальный core audit/outbox record участвует в той же
  transaction, что и target mutations, через `TransactionalAuditPort` либо
  эквивалентный DB adapter. Capability и availability проверяются до `LOADING`;
  при их отсутствии commit запрещён.
- External store/listener является caller-provided post-commit port, изолирует
  data по `run_id` и применяет retention. Callback не может понизить или
  отменить security decision. Delivery failure оставляет durable retry evidence
  и не создаёт ложный rollback.
- После rollback terminal audit event записывается отдельной операцией. Если она
  не удалась, run завершается `FAILED`: `LoadReport` сообщает подтверждённый
  rollback, а `SecurityReport` — audit gap.

**Detection и evidence:** previous/current hash, sequence/run ID, redaction
summary, transactional outbox ID, commit/store/delivery outcome и key ID.
Evidence само не содержит sensitive payload.

**Security outcome:** sensitive data не покидает redaction boundary; отсутствие
atomic audit даёт `AUDIT_DURABILITY_REQUIRED` до mutation. Post-commit listener
failure MAY дать `COMPLETED_WITH_WARNINGS`, но не меняет committed outcome.

**Residual risk:** hash chain обнаруживает изменение сохранённых событий, но
сама по себе не предотвращает полное удаление или усечение chain. Для этого
deployment нужен внешний append-only store или периодическое anchoring.

### `TB-09`. SDK output → downstream sinks

**Активы:** `A-01`, `A-03`, `A-09`.

**Abuse cases:** XSS/raw HTML rendering; CSV/XLSX formula injection; shell или
template injection; интерпретация source/LLM text как SQL; leakage через report.

**Controls:** `REQUIRED`; implementation `PLANNED` (`M12`, `M13`).

- Source и LLM values сохраняют признак untrusted data. Экспорт выполняет
  context-specific escaping/sanitization на последней детерминированной границе.
- HTML output экранируется; raw HTML нельзя рендерить без отдельной sanitization
  policy. JavaScript и external resources не активируются.
- CSV/XLSX exporter применяет формализованную formula-injection policy к values,
  начинающимся с `=`, `+`, `-` или `@`.
- Untrusted values не передаются напрямую в SQL, shell или template engine. SQL
  остаётся parameterized, а identifiers — catalog/allowlist-bound.
- Reports содержат только минимальные redacted details. Consumer обязан явно
  выбрать safe renderer/exporter; формат не определяется из attacker input.

**Detection и evidence:** output policy/version, sink type, redaction/escaping
counts и rejected constructs без сохранения raw value.

**Security outcome:** unsafe sink без подходящей policy отклоняется либо получает
данные только в inert representation; SDK не выполняет output.

**Residual risk:** caller может извлечь typed raw data и затем обойти safe
exporter. Безопасность такого downstream использования лежит за boundary SDK.

## Security gates и состояния pipeline

Названия состояний канонически определены в
[разделе 25 ТЗ][spec-pipeline-statuses]. Security gates обязательны в следующих
точках:

1. `CREATED` → `SOURCE_PROBING`: validate input/policy, включая immutable
   path policy, и установить limits.
2. `SOURCE_PROBING`/`TECHNICAL_PARSING`: проверить format, parser trust, sandbox и
   container-specific controls.
3. `STRUCTURE_PROFILING`/`STRUCTURE_ANALYZING`: классифицировать и минимизировать
   source samples до любого provider egress.
4. `PARSE_PLAN_VALIDATING`: проверить closed operators, physical references и
   fingerprint lineage до semantic parsing.
5. `SEMANTIC_PARSING`/`NORMALIZED_DATA_PROFILING`: применять только checked plan
   и повторно проверить limits/provenance.
6. `DATABASE_INSPECTING`: использовать только `schema_inspector`, построить
   database fingerprint/target identity и применить allow/denylist.
7. `MAPPING`/`MAPPING_PLAN_VALIDATING`: ограничить LLM candidates, проверить output,
   allowlist, DDL deny и plan fingerprint.
8. `NORMALIZING`/`VALIDATING`: не выполнять values и не понижать plan errors до
   record warnings.
9. До `STAGING` и `LOADING`: на writer session повторно сверить source,
   database и target identity, plan evidence, allow/denylist и transactional
   audit/outbox availability.
10. Перед terminal outcome: выполнить atomic commit либо подтверждённый rollback,
   cleanup, durable core audit и post-commit delivery согласно outcome.

Неоднозначный, но допускающий ручное решение случай завершается
`NEEDS_REVIEW`. Критический event до load завершается `REJECTED_SECURITY`.
Security event после начала transaction требует rollback; terminal status не
должен утверждать rollback, пока adapter его не подтвердил. Из terminal state
возобновление запрещено; после review создаётся новый run.

## Контролируемые outcomes и error codes

Status и error code — разные части contract. Status описывает итог run, а typed
error содержит стабильный `error_code`, безопасный `message`, redacted `details`,
`run_id`, `retryable` и sanitized `cause`.

- Strict/untrusted parser без sandbox: `REJECTED_SECURITY`,
  `SECURITY_SANDBOX_REQUIRED`.
- PDF без text layer: контролируемый parser failure без OCR fallback,
  `PARSER_NO_TEXT_LAYER`.
- Source изменён после analysis: без записи,
  `SOURCE_FINGERPRINT_MISMATCH`.
- Path не разрешён immutable policy либо не является безопасным regular file:
  без чтения content, `SOURCE_PATH_NOT_ALLOWED`.
- Snapshot lease закрыт или истёк: без staging/load,
  `SOURCE_SNAPSHOT_EXPIRED`.
- Schema БД изменилась после analysis: без записи,
  `DATABASE_FINGERPRINT_MISMATCH`.
- Inspection и writer относятся к разным targets: без staging/load,
  `DATABASE_TARGET_MISMATCH`.
- Atomic audit/outbox capability отсутствует: без load,
  `AUDIT_DURABILITY_REQUIRED`.
- Configured deadline истёк: после rollback/cleanup, `FAILED`,
  `PROCESSING_TIMEOUT`.
- Запрошена DDL operation: `REJECTED_SECURITY`, `DDL_FORBIDDEN`.
- Target отсутствует в allowlist: `REJECTED_SECURITY`, `TARGET_NOT_ALLOWED`.
- Suspicious prompt/content: `NEEDS_REVIEW` либо `REJECTED_SECURITY` согласно
  risk policy, `PROMPT_INJECTION_DETECTED`.
- Превышен security limit: `REJECTED_SECURITY` без partial load;
  `SECURITY_LIMIT_EXCEEDED`.
- Запрещён PII/provider egress: `REJECTED_SECURITY`,
  `LLM_DATA_ROUTING_FORBIDDEN`.
- Невалидный LLM/plugin output: без staging/load, `LLM_OUTPUT_INVALID`.

Новые codes должны быть machine-readable, не переиспользовать старый смысл и
не включать sensitive values. До определения кода feature не считается готовой;
перечисленные выше имена входят в стабильный M0 contract.

## План security regression evidence

Все сценарии ниже остаются `REQUIRED`. M3 даёт registry-level evidence для
`SR-01`, descriptor fail-closed части `SR-08`, physical output boundary
`SR-10` и host-import запрета `SR-24`. Format-specific, sandbox, pipeline и DB
части остаются `PLANNED`. Остальные contract-level invalid-state,
SQL/code-field, import-boundary и canonical payload tests имеют evidence M2.

- `SR-01` (`M3`, `M4`): extension/MIME/signature conflict в M3 возвращает
  warning code либо typed `PARSER_FORMAT_CONFLICT` и не обходится
  молча. `SecurityEvent` и policy routing остаются `PLANNED` до
  orchestrator.
- `SR-02` (`M4`, `M12`): malformed container не раскрывает raw bytes/paths в
  error.
- `SR-03` (`M4`, `M12`): XML XXE/DTD не читает file и не выполняет network
  request.
- `SR-04` (`M4`, `M12`): YAML object tag и alias bomb отклоняются bounded safe
  loader.
- `SR-05` (`M4`, `M12`): HTML script/iframe/external resource не выполняется и
  не загружается.
- `SR-06` (`M4`, `M12`): archive traversal, symlink, executable и compression
  bomb отклоняются.
- `SR-07` (`M4`, `M12`): каждый resource limit даёт explicit failure, cleanup и
  no partial load; pre-commit deadline даёт `FAILED`/`PROCESSING_TIMEOUT`.
- `SR-08` (`M3`, `M12`): strict/untrusted parser без sandbox даёт
  `SECURITY_SANDBOX_REQUIRED`.
- `SR-09` (`M12`): sandbox не видит network, secrets, Docker socket и чужой
  temp.
- `SR-10` (`M3`, `M12`): forged plugin output повторно отклоняется core
  validation.
- `SR-11` (`M8`, `M12`): prompt injection из source/DB comments не расширяет
  candidates или authority.
- `SR-12` (`M8`, `M12`): raw `RESTRICTED` data не уходят primary provider или
  fallback.
- `SR-13` (`M8`, `M9`): LLM SQL, unknown identifier и malformed schema не
  создают valid plan.
- `SR-14` (`M5`, `M11`, `M12`): inspector не пишет, writer не видит target вне
  allowlist.
- `SR-15` (`M9`, `M11`, `M12`): SQL/identifier injection остаётся data, DDL
  даёт `DDL_FORBIDDEN`.
- `SR-16` (`M9`, `M11`): source/schema drift даёт fingerprint code до mutation.
- `SR-17` (`M11`, `M12`): pre-commit failure/cancellation откатывает atomic
  load и очищает staging.
- `SR-18` (`M11`, `M12`): dry run не оставляет persistent data-plane
  mutations или staging residue, но сохраняет redacted control-plane evidence.
- `SR-19` (`M3`, `M12`): temp/quarantine изолированы по run; cleanup failure
  наблюдаем.
- `SR-20` (`M12`): logs/errors/audit редактируют secrets, raw PII и control
  characters.
- `SR-21` (`M12`): изменение или reorder audit event ломает HMAC verification.
- `SR-22` (`M12`): masking map истекает по TTL; key rotation сохраняет
  verification policy.
- `SR-23` (`M12`, `M13`): HTML и CSV/XLSX payload остаются inert;
  shell/template execution отсутствует.
- `SR-24` (`M3`, `M12`): untrusted parser descriptor не вызывает host-side
  import/constructor; artifact загружается только внутри sandbox.
- `SR-25` (`M3`, `M12`): path вне root, symlink swap, directory, device, FIFO
  и socket отклоняются; opened-handle identity не меняется между check/read.
- `SR-26` (`M5`, `M11`, `M12`): inspection БД A и writer БД B дают
  `DATABASE_TARGET_MISMATCH` до staging.
- `SR-27` (`M3`, `M11`): live `SourceAnalysis` работает между staged calls,
  а closed/expired lease даёт `SOURCE_SNAPSHOT_EXPIRED` и cleanup.
- `SR-28` (`M11`, `M12`): target mutations и core audit/outbox commit
  атомарны; post-commit listener failure сохраняет retry evidence.
- `SR-29` (`M5`, `M11`, `M12`): unregistered target и per-run расширение
  maximum allowlist дают `TARGET_NOT_ALLOWED` до staging.

Test fixtures не должны содержать реальные credentials или PII. Network-denial
tests обязаны проверять отсутствие фактического запроса, а rollback tests —
состояние БД после transaction, не только возвращённый status.

## Остаточные риски и ограничения

- Runtime protection M1 ограничена import/construction boundary и публичными
  ошибками. Остальные controls этой модели остаются `PLANNED` до milestone
  evidence.
- Parser, sandbox runtime, DB driver и dependencies могут содержать zero-day.
- PII detection и masking не гарантируют обнаружение косвенных identifiers.
- Explicitly allowed LLM provider получает masked context и применяет собственные
  retention/processing controls, проверяемые deployment owner.
- Overprivileged DB account, unsafe triggers и ручной обход SDK увеличивают
  impact, который библиотека не может устранить самостоятельно.
- Cleanup не гарантирует physical erasure из filesystem snapshots и backups.
- HMAC chain без внешнего anchoring не доказывает отсутствие полного удаления
  или усечения audit history.
- Failure отдельной записи terminal rollback event может оставить audit gap;
  такой outcome обязан быть видим caller и внешнему monitor.
- Safe exporter не защищает downstream consumer, который намеренно трактует raw
  values как HTML, formula, SQL, shell или template code.

## Out of scope M0/MVP

- Разработка web-service, endpoint authentication, session management и UI.
- Выполнение DDL, SQL-файлов, source code, macros или model output.
- Изображения, видео, аудио, OCR и распознавание рукописного текста.
- Автоматическая обработка архивов без явно включённого bounded adapter.
- Sandbox execution executable extensions кроме parser без отдельного protocol.
- Гарантии безопасности host OS, external LLM provider, БД, backup и
  caller-provided stores за пределами проверяемых adapter contracts.
- Заявление о реализованности controls до появления code, tests и review
  evidence соответствующего milestone.

[spec-pipeline-statuses]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#25-статусы-pipeline
