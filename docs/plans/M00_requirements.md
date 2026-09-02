# План M00 «Зафиксировать требования»

Статус реализации: выполнен 2026-09-02. Нормативные результаты находятся в
`docs/requirements.md`, `docs/architecture.md`, `docs/threat-model.md` и
`docs/public-api.md`; междокументные решения закреплены в двух ADR.

## Цель

Создать согласованный документальный baseline для последующих milestone: границы
MVP, архитектурные инварианты, модель угроз и публичные сценарии SDK должны быть
однозначны, проверяемы и не требовать написания production-кода.

## Основание и ограничения

План опирается только на текущую задачу, корневой `AGENTS.md`,
`docs/codex/PROJECT_CONTEXT.md`, `docs/codex/SPEC_INDEX.md` и выбранные разделы
ТЗ: `0`, `1`, `2`, `3`, `5`, `6`, `7`, `20`, `21`, `23`, `33` и `M0`.

В рамках M00:

- не создаются Python-пакет, DTO, protocols, adapters, migrations и тестовый код;
- не добавляются production dependencies и не меняется схема БД;
- не проектируются demo API, worker или UI как часть ядра;
- не переписывается полное ТЗ: документы выделяют нормативный baseline и дают
  ссылки на источник требований;
- спорные долгоживущие решения выносятся в ADR только после выбора варианта;
  создание ADR не должно подменять четыре обязательных документа M0.

## Наблюдаемое состояние

### До M00

- требования существуют в полном ТЗ и кратких файлах `docs/codex/`, но четыре
  документа M0 отсутствуют;
- каталог `docs/plans/` и этот план отсутствовали;
- public API показан примерами, но его имена, режимы, побочные эффекты и ошибки
  ещё не образуют отдельный согласованный contract;
- package implementation, tests, `pyproject.toml`, `Makefile` и docs builder пока
  отсутствуют, поэтому нельзя ссылаться на них как на существующие проверки.

### После M00

- четыре документа M0 образуют непротиворечивый нормативный набор и ссылаются
  друг на друга вместо копирования одинаковых определений;
- форматы, исключения, режимы, trust boundaries, роль LLM, запрет DDL, staging и
  публичные сценарии имеют одного владельца истины;
- все решения, блокирующие M1 или M2, закрыты; прочие имеют статус, владельца,
  срок пересмотра и целевой milestone;
- поведение репозитория как программы не меняется: результат M00 — только
  проверенные Markdown-документы.

## Создаваемые документы

- `docs/requirements.md` — нормативный scope и проверяемые требования. Файл
  содержит цель SDK, обязательные входы и форматы, plugin-only форматы, явные
  исключения, режимы, NFR и матрицу трассировки `AC-01`–`AC-33`.
- `docs/architecture.md` — архитектурные границы и решения. Файл описывает SDK
  как библиотеку, ports/adapters, направление зависимостей, pipeline,
  async-first/sync facade, lifecycle run, staging/transaction/rollback, support
  tiers СУБД и журнал решений.
- `docs/threat-model.md` — модель доверия и защитные обязательства. Файл
  перечисляет активы, trust boundaries, недоверенные входы, угрозы, controls,
  security outcomes, остаточные риски, data classification, masking, audit и
  parser isolation.
- `docs/public-api.md` — пользовательский contract SDK. Файл описывает
  пошаговый и одношаговый сценарии, analysis-only, dry run, sync facade,
  extension points, side-effect matrix, ошибки/статусы и совместимость.

При изменении или уточнении долгоживущего решения относительно ТЗ создаётся
отдельный `docs/adr/NNNN-<decision>.md`. Если выбранный вариант лишь дословно
фиксирует уже заданный инвариант, достаточно записи в журнале решений
`docs/architecture.md`.

При реализации созданы минимальные ADR:

- `docs/adr/0001-public-api-and-run-policies.md`;
- `docs/adr/0002-security-boundary-defaults.md`.

## Нормативный baseline M0

### Форматы, входы и явные исключения

`docs/requirements.md` должен разделять формат содержимого и транспорт входа.
Это исключает ошибочное обещание «поддержки любого бинарного файла».

- Встроенные текстовые форматы: TXT, LOG и MD.
- Встроенные табличные форматы: CSV, TSV и XLSX.
- Встроенные структурированные форматы: JSON, JSONL, NDJSON, XML и YAML.
- Встроенные web-документы: HTML и XHTML.
- Встроенные документные форматы: DOCX и PDF только с текстовым слоем.
- Программные входы: `dict`, `list`, JSON body, `bytes`, `bytearray`,
  `BinaryIO`, `TextIO`, `Iterable[dict]` и `AsyncIterable[dict]`.
- Только через plugin/adapter: XLS, ODS, RTF, EML, EPUB, DBF, корпоративные XML,
  выгрузки 1С, специальные журналы, разрешённые архивы, Apache Tika и
  пользовательские форматы.

Из базовой версии явно исключаются:

- фотографии и другие изображения, видео, аудио;
- OCR и распознавание рукописного текста;
- выполнение исходного кода, SQL-файлов, макросов и бинарных файлов;
- сканированные PDF без текстового слоя; ожидаемый код ошибки —
  `PARSER_NO_TEXT_LAYER`;
- автоматическое применение DDL; в MVP допустимо только безопасное предложение
  схемы для ручного рассмотрения;
- архивы без отдельно включённого adapter и ограничений на размер, количество,
  вложенность, compression ratio, пути и symlinks.

### Уже зафиксированные инварианты

- продукт является встраиваемым Python SDK, а не web-сервисом;
- основной API асинхронный, sync API является отдельной оболочкой;
- ядро не зависит от web frameworks, UI, конкретной LLM, СУБД или file store;
- PostgreSQL — поддерживаемый основной стенд, SQLite — дополнительная среда
  тестирования; расширение выполняется через adapters;
- deterministic mapping работает без LLM; LLM ограничена выбором из candidates,
  не получает DB connection, credentials или tools и не создаёт исполняемый SQL;
- обычный ingest не выполняет DDL;
- запись ограничена allowlist, проходит staging и выполняется транзакционно с
  rollback;
- path разрешается immutable `SourcePathPolicy`, а staged flow удерживает
  explicit bounded `SourceAnalysis` lease;
- inspection/writer principals различны, но связаны одним `target_identity` и
  composition-owned maximum target policy;
- core audit/outbox real load атомарен с target transaction;
- source content, metadata БД, adapter output, LLM output и mapping templates
  считаются недоверенными; executable object adapters являются trusted host code;
- импорт пакета не имеет I/O и иных перечисленных в `NFR-001` побочных эффектов;
- public API типизирован, расширяем через instance-level dependencies и не
  использует глобальное mutable state.

## Публичные сценарии SDK

`docs/public-api.md` описывает наблюдаемое поведение, а не внутреннюю реализацию:

- анализ без записи — `analyze(source, target)` либо утверждённый эквивалент.
  Сценарий читает источник и metadata БД, возвращает format, profile, catalog,
  candidates, confidence и issues, но не меняет данные или схему БД;
- пошаговый async flow — `inspect_source` → `inspect_database` → `create_plan` →
  `validate_plan` → `execute`. Каждый шаг возвращает типизированный результат;
  execute разрешён только для валидного plan и неизменившихся fingerprints;
- одношаговый безопасный импорт — `ingest(..., safety_policy="auto_safe")`. При
  достоверности, конфликте, schema drift или критическом security event сценарий
  не пишет основные таблицы;
- dry run — `ingest(..., dry_run=True)`. Сценарий возвращает предполагаемые
  операции и отчёты; политика временного staging закрывается решением `D-04`;
- sync facade — `StructuraGuard.ingest(...)`. Он эквивалентен async contract, но
  внутри активного event loop завершается контролируемой типизированной ошибкой;
- пользовательский parser — object registration только для trusted host code;
  untrusted parser загружается по declarative descriptor внутри sandbox.
  Регистрация instance-local и не изменяет глобальное состояние;
- aliases и business rules — `sdk.mapping.aliases.register(...)` и
  `sdk.validation.rules.register(...)`. Расширения участвуют в воспроизводимом
  plan/report и имеют версию или fingerprint;
- предложение схемы — `propose_schema(source)`. Результат декларативен и не
  применяет DDL;
- контролируемый отказ — result/status или typed error. Документируются как
  минимум `NEEDS_REVIEW`, `REJECTED_SECURITY`, `PARSER_NO_TEXT_LAYER`,
  `SECURITY_SANDBOX_REQUIRED`, timeout, cancellation и rollback.

Для каждого сценария документ фиксирует тип входа и результата, допустимые
побочные эффекты, cancellation/timeout, ownership ресурсов, безопасные details
ошибки и достаточные metadata для воспроизводимости. Точные DTO остаются scope M2.

## Активы и trust boundaries

### Активы

- Production DB: целостность данных и схемы, ограничения, доступ только к
  allowlist и атомарность изменений.
- Credentials и ключи: inspection/writer DSN, provider secrets и audit HMAC key;
  они не попадают в prompts, logs, errors или reports.
- Source и normalized data: конфиденциальность raw values, PII/secrets,
  provenance и защита от подмены.
- Mapping artifacts: целостность candidates, plan, версий, source/DB
  fingerprints и решения validation.
- Staging, quarantine и temp: изоляция, минимальный срок хранения, cleanup и
  запрет смешения runs.
- LLM context/output: минимизация отправляемых данных, schema-bound output и
  защита от prompt injection.
- Audit trail: полнота, безопасное содержание, порядок событий и tamper evidence.
- Parser/plugin execution: CPU, memory, time, filesystem/network isolation и
  происхождение adapter.

### Границы

- Caller → SDK. Недоверенный поток: source, config, aliases, rules и templates.
  Controls: типизированная валидация, лимиты, fingerprints и отсутствие
  неявного исполнения.
- Filesystem/stream → detector/parser. Недоверенный поток: bytes, MIME,
  filename, archives и documents. Controls: content-based detection, событие
  extension mismatch, safe parsers и resource limits.
- SDK core → parser runner/plugin. Trusted object уже исполнен host; untrusted
  descriptor/import выполняется только в sandbox, а любой output перепроверяется.
  Controls: явная trust policy, no network/secrets, timeout и cleanup.
- SDK → DB inspector. Недоверенный поток: metadata и существующие values.
  Controls: отдельный read-only user, schema/table filters и безопасные logs.
- Loader → staging/production DB. Недоверенный поток: планируемые записи и DB
  errors. Controls: отдельный writer, target identity, allow/denylist,
  параметризованный SQL, validation plan, transaction/rollback и запрет DDL.
- Mapper → LLM provider. Недоверенный поток: candidates и разрешённый source
  context. Controls: privacy routing, masking, token/call limits, отсутствие
  tools/credentials/DB и strict output schema.
- SDK → stores/listeners/logs. Недоверенный поток: reports, audit и callbacks.
  Controls: transactional core outbox, redaction, bounded payloads и явные
  post-commit ports; сбой consumer не обходит security decision.
- SDK output → HTML/CSV/XLSX/shell/templates. Недоверенный поток: значения из
  source или LLM. Controls: escaping/sanitization, formula-injection policy и
  запрет прямого исполнения.

`docs/threat-model.md` для каждой границы связывает threat scenario, preventive
и detective controls, security outcome, audit evidence и остаточный риск.
Threat model не утверждает наличие ещё не реализованного sandbox или secure
store.

## Открытые архитектурные решения

Ниже перечислены решения, которые требовалось закрыть в M0. После реализации
все они имеют статус `ACCEPTED_M0`; нормативные формулировки находятся в
итоговых документах и ADR.

### `D-01`. Слои public API

- Варианты: только combined `analyze/create_plan`, только пошаговый flow или оба
  слоя.
- Trade-off: один слой проще; два покрывают automation и review, но требуют
  строгой эквивалентности.
- Принято: пошаговый async flow является canonical, а `analyze` и
  `ingest` — thin convenience orchestration. Фиксация: `docs/public-api.md`; ADR
  нужен при отклонении от ТЗ.

### `D-02`. Тип и повторное чтение source

- Варианты: отличать path от raw text эвристикой либо явным source type; повторно
  читать оригинал либо использовать snapshot.
- Trade-off: heuristics удобнее, но создают I/O и security ambiguity; tagged
  inputs и snapshot явнее, но требуют дополнительного contract.
- Принято: `str` в path-примерах означает путь, raw text передаётся явным
  source type; path требует immutable `SourcePathPolicy`. `inspect_source`
  передаёт caller async-closeable fingerprint-bound `SourceAnalysis` lease.
  Фиксация: `docs/public-api.md` и `docs/architecture.md`.

### `D-03`. Модель режимов

- Варианты: объединить `auto_safe`, load error policies и `dry_run` в одном
  `mode` либо сделать их независимыми.
- Trade-off: один enum короче, но смешивает orchestration, error handling и side
  effects.
- Принято: разделить safety/orchestration policy, load error policy и dry-run
  flag; `auto_safe` run-level gates всегда veto, а partial policies применяются
  только к recoverable record-level failures. `atomic` оставить default.

### `D-04`. Side effects dry run

- Варианты: запретить любые DB writes, разрешить rollback-only staging либо
  сохранять preview в staging.
- Trade-off: полный запрет проще проверить; staging точнее моделирует DB
  constraints, но может оставить следы.
- Принято: persistent source/target/staging data-plane mutations запрещены;
  допустимы transient/rollback-only staging с cleanup и redacted control-plane
  audit/events.
  Фиксация: `docs/architecture.md` и `docs/public-api.md`.

### `D-05`. Идентичность и повторное использование MappingPlan

- Варианты: mutable plan либо versioned immutable plan.
- Trade-off: mutation удобна для UI, но ломает fingerprint и audit.
- Принято: редактирование создаёт новую версию, execute повторно сверяет
  source/DB fingerprints, `target_identity` и validation evidence. Фиксация:
  `docs/architecture.md`.

### `D-06`. LLM routing и privacy

- Варианты: automatic fallback либо вызов только по явной provider/data policy.
- Trade-off: automatic fallback повышает coverage, но создаёт privacy и cost
  risks.
- Принято: deterministic-first, provider optional, egress только по
  явной policy; `RESTRICTED` не отправляется без отдельного разрешения и masking.
  Фиксация: `docs/threat-model.md` и ADR.

### `D-07`. Изоляция parser

- Варианты: in-process по умолчанию либо sandbox для недоверенных данных.
- Trade-off: in-process проще; sandbox снижает blast radius, но требует
  отдельного runtime.
- Принято: object registration означает trusted host code. Untrusted parser
  импортируется и создаётся только внутри `SandboxParserRunner`; без него
  возвращается `SECURITY_SANDBOX_REQUIRED`. Фиксация:
  `docs/threat-model.md` и ADR.

### `D-08`. Support tiers СУБД и staging

- Варианты: обещать общую SQL portability либо определить узкие support tiers.
- Trade-off: portability расширяет API, но пока не подтверждена adapters; только
  PostgreSQL сужает extension story.
- Принято: PostgreSQL — supported production target, SQLite — test
  target, остальные СУБД — extension adapters; staging provision/cleanup явны и
  не выполняются при import. Фиксация: `docs/architecture.md`.

### `D-09`. Audit, masking map, keys и retention

- Варианты: встроенные stores либо caller-provided ports.
- Trade-off: встроенное хранилище удобно, но нарушает deployment-neutral SDK и
  повышает риск утечки.
- Принято: caller передаёт stores/keys через ports, retention настраивается,
  restricted raw values не входят в audit; для real load core audit/outbox
  атомарен с target transaction. Фиксация: `docs/threat-model.md` и ADR.

### `D-10`. Граница DDL

- Варианты: schema proposal либо также отдельный administrative apply API в MVP.
- Trade-off: admin API расширяет продукт, но резко увеличивает права и attack
  surface.
- Принято: M0 фиксирует только proposal; применение DDL исключается
  из MVP и обычного pipeline. Фиксация: `docs/requirements.md`.

### `D-11`. Lifecycle registry

- Варианты: mutable instance registry либо immutable composition.
- Trade-off: runtime registration удобна; shared registry создаёт races и hidden
  state.
- Принято: регистрация разрешена до run на конкретном SDK instance;
  lifecycle/concurrency semantics фиксируются до M2 в `docs/architecture.md` и
  `docs/public-api.md`.

### `D-12`. Resource policy defaults

- Варианты: нормативные defaults либо только deployment configuration.
- Trade-off: жёсткие числа безопаснее, но зависят от deployment; отсутствие
  defaults небезопасно.
- Принято: безопасные defaults с явными overrides и абсолютными
  caps для strict mode. Фиксация: `docs/requirements.md` и
  `docs/threat-model.md`.

Семантика load error policies подтверждена узким чтением раздела `18.4` ТЗ и
зафиксирована без смешения с `safety_policy` и `dry_run`.

## Критерии готовности документов

### Общие

- каждый нормативный термин имеет одно определение и одного владельца истины;
- все cross-document ссылки относительные и ведут к существующему файлу/anchor;
- API names, policy names, statuses и error codes одинаковы во всех четырёх
  документах;
- каждый `AC-01`–`AC-33` связан с требованием, будущим evidence и milestone, без
  заявления о ещё не реализованной функции;
- ни одно решение со статусом `OPEN` не блокирует M1/M2; для неблокирующих
  решений указаны owner, target milestone и условие пересмотра;
- документы отделяют normative requirements от rationale, examples и future
  scope;
- Markdown lint, проверка ссылок и фактическая whitespace-проверка новых файлов
  завершаются успешно.

### `docs/requirements.md`

- обязательные, plugin-only и исключённые форматы перечислены раздельно;
- PostgreSQL/SQLite support tiers, режимы, DDL prohibition, staging, rollback,
  LLM role и NFR зафиксированы проверяемыми формулировками;
- каждому требованию назначен стабильный ID; критерии приёмки трассируются без
  дублирования полного текста ТЗ;
- scope MVP и deferred scope не допускают обещания «абсолютно любого формата».

### `docs/architecture.md`

- показаны product boundary, layers, ports/adapters и допустимые направления
  зависимостей;
- pipeline и жизненный цикл run описывают side effects, fingerprints, staging,
  commit/rollback, cancellation и cleanup;
- DB roles, plugin lifecycle, LLM boundary и отсутствие import-time side effects
  согласованы с требованиями;
- журнал решений содержит статус и ссылки на ADR только там, где ADR нужен.

### `docs/threat-model.md`

- перечислены все недоверенные входы, активы и границы из этого плана;
- для каждой значимой угрозы указаны control, detection/audit evidence, outcome
  и residual risk;
- отдельно покрыты prompt injection, XXE/DTD, HTML/YAML safety, archives, resource
  exhaustion, PII/secrets, parser isolation, DB privilege separation и output
  injection;
- секреты и restricted raw values не попадают в примеры logs/reports/audit.

### `docs/public-api.md`

- все публичные сценарии из этого плана имеют вход, результат, side effects и
  контролируемый failure path;
- canonical async flow и convenience APIs не противоречат друг другу;
- sync facade, cancellation, timeout, ownership streams и registry lifecycle
  описаны явно;
- API examples используют placeholder secrets и не обещают DTO до M2;
- compatibility policy соответствует Semantic Versioning и deprecation period.

## Шаги выполнения M0

1. `docs/requirements.md` — сформировать normative scope, таблицу форматов и
   исключений, режимы/NFR и traceability matrix `AC-01`–`AC-33`. Проверка:
   review на полноту по выбранным разделам ТЗ и Markdown lint этого файла.
2. `docs/architecture.md` — зафиксировать границы SDK, pipeline, ports/adapters,
   lifecycle и решения `D-01`–`D-05`, `D-08`, `D-10`–`D-12`. Проверка:
   dependency/side-effect review и отсутствие обещаний реализации.
3. `docs/threat-model.md` — связать активы и trust boundaries с threats,
   controls, security outcomes и residual risks; закрыть `D-06`, `D-07`, `D-09`.
   Проверка: security review по каждой строке boundary matrix.
4. `docs/public-api.md` — описать canonical async API, convenience/sync flows,
   extension points, status/error taxonomy и side-effect matrix после решений
   `D-01`–`D-05`, `D-11`. Проверка: пройти каждый сценарий как consumer без
   обращения к внутренним типам будущей реализации.
5. Все четыре файла — провести consistency pass: термины, links, anchors,
   policy names, fingerprints, error codes и acceptance traceability. При
   реальном расхождении с ТЗ добавить минимальный ADR, а не скрыто менять смысл.
6. Запустить документальные проверки ниже, затем review diff. M0 готов только
   при выполнении всех критериев готовности; production quality gates остаются
   неприменимыми, пока соответствующие targets не появятся.

## Команды проверки Markdown и ссылок

До появления репозиторного docs target используются явные pinned команды:

```bash
npx --yes markdownlint-cli2@0.21.0 \
  docs/plans/M00_requirements.md \
  docs/requirements.md \
  docs/architecture.md \
  docs/threat-model.md \
  docs/public-api.md \
  docs/adr/0001-public-api-and-run-policies.md \
  docs/adr/0002-security-boundary-defaults.md

npx --yes markdown-link-check@3.15.0 \
  docs/plans/M00_requirements.md \
  docs/requirements.md \
  docs/architecture.md \
  docs/threat-model.md \
  docs/public-api.md \
  docs/adr/0001-public-api-and-run-policies.md \
  docs/adr/0002-security-boundary-defaults.md

for file in \
  docs/plans/M00_requirements.md \
  docs/requirements.md \
  docs/architecture.md \
  docs/threat-model.md \
  docs/public-api.md \
  docs/adr/0001-public-api-and-run-policies.md \
  docs/adr/0002-security-boundary-defaults.md
do
  check_output="$(git diff --no-index --check /dev/null "$file")"
  check_status=$?
  if [ "$check_status" -gt 1 ] || [ -n "$check_output" ]; then
    printf '%s\n' "$check_output"
    exit 1
  fi
done
```

После появления воспроизводимого dev toolchain эти же проверки должны быть
закреплены в `make docs`; до создания такого target команда `make docs` не
считается доступной. Проверка существующего Codex-набора выполняется отдельно:

```bash
python3 scripts/validate_codex_pack.py .
```

## Риски

- Дублирование требований между четырьмя файлами приведёт к drift; снижается
  назначением одного нормативного владельца и относительными ссылками.
- Раннее закрепление signatures может преждевременно ограничить M2; M0 фиксирует
  наблюдаемое поведение, а не внутреннюю форму DTO.
- Точные DTO M2 могут выявить новую несовместимость с решениями `D-01`–`D-12`;
  изменение contract требует ADR и повторной проверки всех четырёх документов.
- External link checks зависят от сети и могут быть flaky; внутренние links и
  anchors проверяются обязательно, внешние повторы/исключения документируются,
  но не скрываются глобальным ignore.
- Threat model может ошибочно описать planned controls как действующие; для
  каждого control требуется явная отметка `required`, пока реализации нет.
