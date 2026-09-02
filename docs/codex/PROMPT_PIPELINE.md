# StructuraGuard — полный конвейер запросов для Codex

Этот документ задаёт порядок разработки StructuraGuard SDK через Codex и проектные skills из `.agents/skills/`.

## 1. Как пользоваться документом

### 1.1. Базовое правило

Не отправляйте Codex весь документ одним сообщением. Выполняйте один milestone за раз и отправляйте запросы строго последовательно.

Для каждого milestone:

1. Создайте отдельную Git-ветку.
2. Запустите Codex из корня репозитория с профилем `quality`.
3. Отправьте запрос на планирование.
4. Просмотрите сохранённый план.
5. Отправляйте запросы реализации по одному.
6. После реализации запустите тестирование, security review при необходимости, документацию и review.
7. Не переходите дальше, пока текущий milestone не прошёл quality gates.
8. Просмотрите `git diff` и создайте commit вручную.
9. Для следующего milestone откройте новую сессию Codex, чтобы не раздувать контекст.

Внутри одного milestone лучше сохранять одну сессию: Codex помнит план и уже просмотренные файлы. После commit начинайте новую сессию.

### 1.2. Запуск Codex

Из корня проекта:

```bash
codex -C . --profile quality
```

Для небольшой локальной правки или документации:

```bash
codex -C . --profile economy
```

Перед первой работой в новой сессии можно проверить skills:

```text
/skills
```

### 1.3. Когда можно переходить к следующему запросу

Переходите дальше только когда Codex:

- закончил текущую операцию;
- перечислил изменённые файлы;
- показал фактически выполненные проверки;
- не оставил Critical/High findings;
- не сообщил о непройденных обязательных тестах.

Если Codex сообщил о блокере, используйте раздел «Исправление незавершённой задачи», а не переходите к следующему milestone.

---

# 2. Карта skills: что и когда применять

| Skill | Когда использовать | Когда не использовать |
|---|---|---|
| `$structuraguard-plan` | Перед milestone, новым модулем, миграцией, изменением публичного API или крупным рефакторингом | Для опечатки или очевидной локальной правки |
| `$structuraguard-python` | DTO, protocols, facade, pipeline, services, configuration, исключения, чистая Python-логика | Самостоятельно недостаточен для parser/DB/LLM: сочетайте с профильным skill |
| `$structuraguard-parser` | Detection, Parser Registry, TXT/LOG/CSV/JSON/XML/HTML/XLSX/PDF/DOCX/YAML/Tika, batching, provenance | Demo UI, чистая DB-логика |
| `$structuraguard-database` | Reflection, DatabaseCatalog, fingerprint, FK graph, MappingPlan validation, staging, loader, transaction, upsert | Разбор входного файла или LLM provider |
| `$structuraguard-llm` | LLMProvider, provider adapter, structured output, semantic mapper, router, retry/fallback, privacy policy | Детерминированный mapper без LLM |
| `$structuraguard-tests` | После реализации, перед завершением milestone, для contract/property/integration/security tests | Не заменяет исправление кода |
| `$structuraguard-security` | Любая trust boundary: файл, XML/YAML/HTML, LLM, БД, staging, PII, logs, plugins, resource limits | Обычная правка текста без изменения поведения |
| `$structuraguard-review` | После реализации и тестов, перед commit/merge | Не применять вместо первоначального проектирования |
| `$structuraguard-docs` | Изменение public API, архитектуры, CLI, примеров, ADR и материалов диплома | Не документировать ещё не реализованное поведение как готовое |
| `$structuraguard-debug` | Только для воспроизводимого дефекта, падения теста, неверных данных, race condition или деградации | Не применять для новой функции |

Допустимо явно упоминать несколько skills в одном запросе, например:

```text
$structuraguard-python $structuraguard-database
```

Первым указывайте профильный skill, затем общий Python-skill.

---

# 3. Одноразовая подготовка проекта

## P00. Проверка конфигурации Codex

Отправить один раз после установки Pro Pack. Код не должен изменяться.

```text
Проверь настройку Codex для этого репозитория. Ничего не изменяй.

Выполни:
- определи корень Git-репозитория и текущую ветку;
- перечисли активные AGENTS.md/AGENTS.override.md в порядке приоритета;
- проверь наличие десяти project skills в .agents/skills;
- выполни `python3 scripts/validate_codex_pack.py .`;
- выполни `python3 -m py_compile scripts/*.py`;
- проверь, что `docs/codex/PROJECT_CONTEXT.md`, `SPEC_INDEX.md`, `QUALITY_GATES.md` и техническое ТЗ доступны;
- не читай полное ТЗ целиком.

В ответе укажи только:
1. рабочий корень;
2. активные инструкции;
3. найденные skills;
4. результаты команд;
5. блокеры, если они есть.
```

## P01. Создание файла состояния проекта

Отправить один раз до M0.

```text
$structuraguard-docs

Создай компактный файл `docs/codex/PROJECT_STATE.md` для передачи состояния между сессиями Codex.

Структура файла:
- текущая версия и текущий milestone;
- завершённые milestones;
- активная ветка;
- реализованные публичные contracts;
- последние успешные quality gates;
- открытые блокеры;
- принятые архитектурные решения со ссылками на ADR;
- следующий рекомендуемый шаг.

Ограничения:
- не более 200 строк;
- не дублировать ТЗ;
- не включать secrets, DSN или временные рассуждения;
- пока отметить все M0–M14 как не начатые.

Проверь Markdown и покажи изменённый файл.
```

---

# 4. Универсальные завершающие запросы

Эти запросы повторяются после реализации каждого milestone. В таблицах milestone ниже указано, какие из них выполнять.

## G1. Тестирование milestone

```text
$structuraguard-tests

Проверь текущий milestone по его plan-файлу и критериям приёмки.

Сделай следующее:
1. Сопоставь каждый критерий приёмки с наблюдаемым тестом.
2. Добавь только недостающие unit/contract/integration/property/security tests.
3. Не ослабляй существующие тесты, lint или typecheck.
4. Не используй реальные платные LLM API.
5. Сначала запусти новые и узкие тесты, затем доступные quality gates для затронутых модулей.
6. Исправляй только дефекты, непосредственно выявленные этими тестами; крупный несвязанный рефакторинг не выполнять.

В конце выведи:
- матрицу «критерий → тест»;
- команды и фактический результат;
- непроверенные сценарии и причину.
```

## G2. Security review milestone

Выполнять после изменений на trust boundary.

```text
$structuraguard-security

Проведи security review только текущего diff и текущего milestone.

Обязательно проверь применимые угрозы:
- attacker-controlled input;
- resource exhaustion;
- parser exploit и unsafe deserialization;
- XXE/DTD/network access;
- prompt injection и excessive agency;
- PII/secrets leakage;
- SQL и identifier injection;
- обход DB allowlist/denylist;
- schema drift;
- небезопасные logs/audit events;
- path traversal и небезопасные временные файлы;
- supply-chain риск новых production dependencies.

Для каждой найденной проблемы:
1. severity;
2. `path:line`;
3. путь эксплуатации;
4. влияние;
5. минимальное исправление;
6. security regression test.

Если находишь Critical/High/Medium проблему, исправь её и запусти соответствующий regression test. Не исправляй несвязанный код.
```

## G3. Обновление документации

```text
$structuraguard-docs

Обнови документацию только для подтверждённого поведения текущего milestone.

Требуется:
- public docstring на русском для изменённого публичного API;
- минимальный копируемый пример, если появился новый пользовательский сценарий;
- ограничения, исключения и security semantics;
- ADR только для долгоживущего архитектурного решения;
- обновление `docs/codex/PROJECT_STATE.md`;
- ссылка на канонический раздел ТЗ вместо его копирования.

Не документируй незавершённые функции как готовые. Запусти доступную сборку документации и проверку примеров.
```

## G4. Финальный review diff

```text
$structuraguard-review

Проведи финальный review текущего uncommitted diff относительно задачи и plan-файла milestone.

Порядок проверки:
1. корректность и риск потери/искажения данных;
2. выполнение всех критериев приёмки;
3. архитектурные границы и направление зависимостей;
4. публичный API и обратная совместимость;
5. error paths, cancellation, timeout и resource lifecycle;
6. batch boundaries, concurrency и идемпотентность;
7. безопасность;
8. достаточность тестов;
9. производительность на заявленных объёмах;
10. отсутствие secrets, debug prints, временных файлов и закомментированного кода.

Findings выведи первыми по severity с `path:line`, доказательством последствий и минимальным исправлением.
Если существенных findings нет, сообщи это явно и перечисли только реальные residual risks и непроверенные suites.
Не изменяй код на этом шаге.
```

## G5. Исправление findings после review

Отправлять только если G4 нашёл проблемы.

```text
$structuraguard-python $structuraguard-tests

Исправь только findings из последнего review текущего milestone.

Правила:
- Critical и High обязательны;
- Medium исправь, если не требуется изменение утверждённого scope;
- Low исправляй только если правка локальна и не создаёт шум;
- на каждый behavioral/security дефект добавь regression test;
- не меняй публичный API без необходимости;
- не выполняй несвязанный рефакторинг.

После исправлений запусти узкие тесты, lint и typecheck. Затем повторно примени `$structuraguard-review` к новому diff.
```

## G6. Закрытие milestone

```text
$structuraguard-docs $structuraguard-review

Подготовь текущий milestone к ручному commit, не создавая commit самостоятельно.

Проверь:
- plan-файл и все критерии приёмки;
- `git diff --check`;
- отсутствие secrets, debug artifacts и случайных generated files;
- актуальность `docs/codex/PROJECT_STATE.md`;
- список фактически выполненных команд;
- наличие объяснения для каждой пропущенной проверки.

Обнови в plan-файле статус и checklist по фактическому состоянию.
В финальном ответе дай только:
1. результат milestone;
2. изменённые файлы;
3. успешные проверки;
4. residual risks;
5. рекомендуемый Conventional Commit message.
```

## G7. Полный quality gate перед merge или релизом

```text
$structuraguard-tests $structuraguard-security $structuraguard-review

Выполни полный quality gate репозитория перед merge/release.

Последовательно запусти доступные команды:
- `make format` или эквивалентную проверку форматирования;
- `make lint`;
- `make typecheck`;
- `make test`;
- `make test-integration`;
- `make test-security`;
- `make docs`;
- сборку wheel/sdist;
- установку wheel в чистое временное окружение;
- smoke test `import structuraguard` без побочных эффектов.

Не скрывай падения и не ослабляй конфигурацию. Если команда отсутствует, зафиксируй это как gap и предложи минимальное исправление. Код меняй только для устранения подтверждённого блокера.
```

---

# 5. M0 — требования, архитектура и модель угроз

## Ветка и профиль

```bash
git switch -c docs/m00-requirements
codex -C . --profile quality
```

## Последовательность

```text
M0-P → M0-I → G2 → G3 → G4 → при необходимости G5 → G6
```

## M0-P. План

```text
$structuraguard-plan

Цель: спланировать milestone M0 «Зафиксировать требования» без написания production-кода.

Сначала прочитай:
- `AGENTS.md`;
- `docs/codex/PROJECT_CONTEXT.md`;
- `docs/codex/SPEC_INDEX.md`;
- через `scripts/extract_spec_sections.py` только разделы `0. Паспорт проекта`, `1. Концепция проекта`, `2. Что является результатом`, `3. Цель и задачи`, `5. Границы`, `6. Режимы`, `7. Полный pipeline`, `20. Информационная безопасность`, `21. Нефункциональные требования`, `23. Публичный API`, `33. Критерии приемки`, `M0`.

Сформируй и сохрани план в `docs/plans/M00_requirements.md`.

Plan должен включать:
- перечень создаваемых документов;
- открытые архитектурные решения;
- форматы и явные исключения;
- trust boundaries и активы;
- публичные сценарии SDK;
- критерии готовности документов;
- команды проверки Markdown/ссылок.

Не повторяй всё ТЗ и не создавай код.
```

## M0-I. Реализация документов

```text
$structuraguard-docs $structuraguard-security

Реализуй утверждённый `docs/plans/M00_requirements.md`.

Создай или доведи до согласованного состояния:
- `docs/requirements.md`;
- `docs/architecture.md`;
- `docs/threat-model.md`;
- `docs/public-api.md`;
- при необходимости минимальные ADR в `docs/adr/`.

Обязательно зафиксируй:
- SDK является библиотекой, а не web-service;
- поддерживаемые форматы и исключения media/OCR;
- PostgreSQL как основной стенд, SQLite только для тестов;
- role of LLM и запрет executable SQL;
- staging, dry-run, transaction и rollback;
- DDL deny-by-default;
- separate inspection/writer DB users;
- состояния pipeline;
- PII/prompt-injection/parser threats;
- acceptance criteria и out-of-scope.

Пиши по-русски, identifiers оставляй на английском. Не дублируй техническое ТЗ целиком.
```

Рекомендуемый commit:

```text
docs: define requirements architecture and threat model
```

---

# 6. M1 — каркас Python-пакета

## Ветка и профиль

```bash
git switch -c feat/m01-sdk-scaffold
codex -C . --profile quality
```

## Последовательность

```text
M1-P → M1-I → G1 → G3 → G4 → при необходимости G5 → G6
```

## M1-P. План

```text
$structuraguard-plan

Цель: спланировать milestone M1 «Каркас Python-пакета».

Прочитай только релевантные разделы через `docs/codex/SPEC_INDEX.md` и `scripts/extract_spec_sections.py`: каркас SDK, NFR-001–NFR-012, стек, public API, структура репозитория, M1 и критерии приёмки.

Проверь текущее состояние репозитория, существующие build files и CI. Не проектируй параллельную структуру, если каркас уже частично создан.

Сохрани исполнимый план в `docs/plans/M01_sdk_scaffold.md`.

План должен покрывать:
- installable package с src-layout;
- `pyproject.toml` и optional extras без лишних dependencies;
- `AsyncStructuraGuard` и отдельный sync facade без реальной бизнес-логики;
- `SDKConfig` без чтения environment при import;
- публичную иерархию typed exceptions;
- Ruff, mypy, pytest, pytest-anyio;
- Makefile;
- CI;
- MkDocs;
- smoke test import-time side effects;
- wheel/sdist build test.

Не реализовывать parsers, DB или LLM.
```

## M1-I. Реализация

```text
$structuraguard-python

Реализуй утверждённый `docs/plans/M01_sdk_scaffold.md` непосредственно в репозитории.

Критерии:
- пакет устанавливается из `pyproject.toml`;
- `import structuraguard` не читает environment, не создаёт event loop, network connections, files, tables и не меняет logging;
- async facade является основным;
- sync facade — явная отдельная оболочка;
- public exceptions имеют стабильный `error_code` и не раскрывают secrets;
- публичный API типизирован;
- web frameworks отсутствуют в core dependencies;
- tests для import side effects, config и exceptions;
- Make targets и CI воспроизводимы.

Не создавать заглушки, которые молча возвращают фиктивный успех. Нереализованные операции должны завершаться явной typed error.
```

Рекомендуемый commit:

```text
feat: scaffold structuraguard sdk package
```

---

# 7. M2 — доменные модели и contracts

## Ветка и профиль

```bash
git switch -c feat/m02-domain-contracts
codex -C . --profile quality
```

## Последовательность

```text
M2-P → M2-I → G1 → G3 → G4 → при необходимости G5 → G6
```

## M2-P. План

```text
$structuraguard-plan

Цель: спланировать milestone M2 «Доменные модели и contracts».

Извлеки только разделы ТЗ для Contracts/DTO через `docs/codex/SPEC_INDEX.md`.
Изучи существующие public types и tests, не читай весь репозиторий.

Сохрани план в `docs/plans/M02_domain_contracts.md`.

План должен определить:
- границы `contracts` и `domain`;
- immutable/value-object semantics;
- сериализацию Pydantic;
- типы provenance;
- стабильные enums/error codes/statuses;
- protocol signatures;
- отсутствие infrastructure imports;
- forward compatibility и version fields;
- тесты invalid states, equality, serialization и protocol substitutability.
```

## M2-I. Реализация

```text
$structuraguard-python

Реализуй `docs/plans/M02_domain_contracts.md`.

Создай и экспортируй минимально необходимые contracts и DTO:
- `Parser`;
- `DatabaseAdapter`;
- `LLMProvider`;
- `SecurityScanner`;
- `StagingStore`;
- `AuditStore`;
- `SourceArtifact`;
- `SourceLocation`/provenance DTO;
- `NormalizedValue`;
- `NormalizedRecord`;
- `NormalizedBatch`;
- `DatabaseCatalog` и дочерние catalog types;
- `MappingCandidate`;
- `MappingPlan`;
- `ValidationReport`;
- `LoadReport`;
- `SecurityReport`;
- `AuditEvent`;
- pipeline statuses.

Требования:
- `domain` и `contracts` не импортируют infrastructure;
- денежные значения — `Decimal`;
- datetime — timezone-aware UTC;
- mutable defaults запрещены;
- недопустимые состояния отклоняются при создании модели;
- `Any` не выходит за адаптерную границу без обоснования;
- public DTO сериализуются детерминированно.

Сначала добавь tests контракта, затем реализацию.
```

Рекомендуемый commit:

```text
feat: add domain models and extension contracts
```

---

# 8. M3 — Parser Registry и плагины

## Ветка и профиль

```bash
git switch -c feat/m03-parser-registry
codex -C . --profile quality
```

## Последовательность

```text
M3-P → M3-I → G1 → G2 → G3 → G4 → при необходимости G5 → G6
```

## M3-P. План

```text
$structuraguard-plan $structuraguard-parser

Цель: спланировать M3 — Parser Registry и безопасное discovery parser plugins.

Прочитай через индекс только разделы 5, FR-001–FR-003, NFR-004, M3, parser contract и текущие tests.
Сохрани план в `docs/plans/M03_parser_registry.md`.

План должен включать:
- ручную регистрацию без global mutable state;
- `probe` score, priority и deterministic tie-breaking;
- MIME/signature/extension conflict semantics;
- duplicate registration policy;
- `FakeParser`;
- entry-point discovery группы `structuraguard.parsers`;
- явное включение plugin discovery, без import-time scanning;
- typed errors;
- contract tests;
- security controls для недоверенного plugin metadata.

Не реализовывать реальные format parsers.
```

## M3-I. Реализация

```text
$structuraguard-parser $structuraguard-python

Реализуй `docs/plans/M03_parser_registry.md`.

Требования:
- registry является экземпляром и передаётся через dependency injection;
- регистрация и selection детерминированы;
- выбор учитывает `probe`, MIME, extension и priority;
- конфликтующие сигналы возвращают предупреждение или typed ambiguity error согласно плану;
- duplicate names/classes обрабатываются явно;
- entry points загружаются только по явному вызову;
- ошибка одного стороннего plugin не ломает discovery остальных и не скрывается;
- `FakeParser` используется в tests;
- orchestrator пока не реализуется.

Добавь общий contract suite, который затем смогут использовать реальные parsers.
```

Рекомендуемый commit:

```text
feat: add parser registry and plugin discovery
```

---

# 9. M4 — встроенные parsers

M4 лучше выполнять несколькими небольшими вертикальными задачами в одной ветке. После каждой группы запускайте узкие tests. Полные G1/G2/G3/G4/G6 выполняются после завершения всей M4.

## Ветка и профиль

```bash
git switch -c feat/m04-built-in-parsers
codex -C . --profile quality
```

## Последовательность

```text
M4-P → M4-A → M4-B → M4-C → M4-D → M4-E → при необходимости M4-F → G1 → G2 → G3 → G4 → G5 при необходимости → G6
```

## M4-P. Общий план

```text
$structuraguard-plan $structuraguard-parser

Цель: спланировать milestone M4 как набор независимых parser adapters без изменения downstream pipeline.

Извлеки только FR-004–FR-012, NFR-006, parser security sections и M4.
Проверь существующий Parser contract и contract suite.
Сохрани план в `docs/plans/M04_built_in_parsers.md`.

Разбей план на группы:
A. TXT/LOG/MD;
B. CSV/TSV;
C. JSON/JSONL/NDJSON;
D. XML/HTML/YAML;
E. XLSX/PDF/DOCX;
F. Tika fallback как optional, только если core группы готовы.

Для каждой группы укажи:
- dependencies/extras;
- detection/probe signals;
- streaming/batching;
- provenance;
- configurable limits;
- typed errors;
- malicious/boundary fixtures;
- contract/property/security tests.

Не объединяй все форматы в один универсальный parser.
```

## M4-A. TXT, LOG и MD

```text
$structuraguard-parser $structuraguard-python

Реализуй группу A из `docs/plans/M04_built_in_parsers.md`: TXT, LOG и MD.

Требования:
- encoding detection с явным confidence/warning;
- line provenance;
- ограничение размера и числа строк до накопления в памяти;
- configurable batching;
- plain text blocks для TXT/MD без выполнения embedded content;
- LOG parser поддерживает минимум plain lines, common timestamp/level и key-value hints без «угадывания» неизвестного формата;
- malformed encoding даёт typed error или контролируемую policy, а не silent replacement;
- cancellation учитывается между batches.

Добавь contract и boundary tests. Не трогай DB/LLM/mapper.
```

## M4-B. CSV и TSV

```text
$structuraguard-parser $structuraguard-tests

Реализуй группу B: CSV/TSV.

Требования:
- dialect detection: delimiter, quote, escape, header;
- пользователь может переопределить detection options;
- streaming batches без полного чтения large source;
- provenance row/column;
- duplicate/empty headers обрабатываются детерминированно;
- max_records, max_columns, max_field_size и cancellation;
- malformed row возвращает typed error с номером строки;
- сохраняются raw values, parser не выполняет бизнес-нормализацию;
- property-based tests для Unicode, delimiters, quoting и batch boundaries.

Не добавляй Polars в обязательные dependencies без доказанной необходимости.
```

## M4-C. JSON, JSONL и NDJSON

```text
$structuraguard-parser $structuraguard-tests

Реализуй группу C: JSON, JSONL и NDJSON.

Требования:
- объект, массив объектов и nested structures;
- JSON Pointer provenance;
- JSONL/NDJSON читаются потоково по строкам;
- malformed line сообщает точный line number;
- лимиты nesting, records, key count и value size;
- batch boundary не создаёт дубли и пропуски;
- top-level scalar отклоняется или обрабатывается строго по утверждённому contract;
- не выполнять автоматический flatten, разрушающий связи; сохранить иерархию в normalized model.

Добавь contract/property tests и regression test на границе batches.
```

## M4-D. XML, HTML и YAML

```text
$structuraguard-parser $structuraguard-security $structuraguard-tests

Реализуй группу D: безопасные XML, HTML и YAML parsers.

XML:
- external entities и DTD отключены;
- никаких network fetch;
- depth/node/text limits;
- namespace support;
- XPath provenance;
- XXE/Billion Laughs regression tests.

HTML:
- JavaScript не выполняется;
- external resources не загружаются;
- извлекаются headings, text blocks, lists и tables;
- CSS selector provenance;
- script/style/iframe обрабатываются по deny-by-default policy;
- XSS payload остаётся данными и безопасно сериализуется.

YAML:
- только safe loader;
- запрет произвольных Python objects и aliases bombs;
- depth/node limits;
- provenance в рамках возможностей выбранной библиотеки документирован.

Dependencies добавляй через optional extras. Не добавляй browser engine.
```

## M4-E. XLSX, PDF и DOCX

```text
$structuraguard-parser $structuraguard-security $structuraguard-tests

Реализуй группу E: XLSX, PDF с текстовым слоем и DOCX.

XLSX:
- read-only mode, где возможно;
- sheets, tables, cells и merged cells;
- sheet/cell provenance;
- formulas и macros не исполняются;
- stored formula values/metadata обрабатываются согласно contract;
- лимиты sheets/rows/columns/cells.

PDF:
- только text-layer PDF;
- pages/blocks/tables в пределах возможностей адаптера;
- page/block provenance;
- timeout/page/text limits;
- сканированный PDF без text layer возвращает `PARSER_NO_TEXT_LAYER`;
- parser не выполняет embedded actions/files.

DOCX:
- paragraphs, headings, lists и tables в исходном порядке;
- block/table/cell provenance;
- macros/relationships/external resources не исполняются и не загружаются;
- лимиты распакованного контейнера.

Добавь fixture-based contract/security tests. Не добавляй OCR.
```

## M4-F. Optional Tika fallback

Выполнять только после готовности обязательных parsers.

```text
$structuraguard-parser $structuraguard-security

Добавь optional `TikaParserAdapter` как fallback, не как dependency ядра.

Требования:
- отдельный extra;
- явная конфигурация endpoint;
- выключен по умолчанию;
- timeout, response-size и content-type limits;
- запрет передачи secrets;
- typed unavailable/timeout/malformed errors;
- документировать, что network/container isolation обеспечивает вызывающий проект;
- default tests используют fake server, а не внешний Tika.

Не заменяй специализированные parsers Tika-адаптером.
```

Рекомендуемый commit:

```text
feat: add safe built-in data parsers
```

---

# 10. M5 — Database Inspector, каталог и граф БД

## Ветка и профиль

```bash
git switch -c feat/m05-database-inspector
codex -C . --profile quality
```

## Последовательность

```text
M5-P → M5-A → M5-B → M5-C → G1 → G2 → G3 → G4 → G5 при необходимости → G6
```

## M5-P. План

```text
$structuraguard-plan $structuraguard-database

Цель: спланировать M5 — безопасный Database Inspector для SQLite и PostgreSQL, stable catalog fingerprint и FK graph.

Извлеки через индекс только разделы 9, 10, 13, M5 и DB security requirements.
Изучи DatabaseAdapter contract и catalog DTO.
Сохрани план в `docs/plans/M05_database_inspector.md`.

Разбей работу:
A. dialect-neutral catalog normalization и SQLite adapter tests;
B. PostgreSQL reflection + Testcontainers;
C. stable fingerprint + FK dependency graph/cycles.

Зафиксируй:
- read-only inspection connection;
- schema/table allowlist до reflection;
- отсутствие DDL/data mutation;
- handling views/generated columns/comments/checks/indexes;
- deterministic ordering/fingerprint;
- metadata-only default;
- resource/time limits.
```

## M5-A. Каталог и SQLite

```text
$structuraguard-database $structuraguard-python

Реализуй часть A `docs/plans/M05_database_inspector.md`.

Требования:
- dialect-neutral normalization DB types;
- SQLite adapter для unit/contract tests;
- извлечение tables, columns, nullable/default, PK, FK, unique, checks, indexes;
- generated/non-writable columns отмечаются явно;
- stable sorting;
- read-only semantics подтверждены tests;
- allowlist/denylist применяется до публикации каталога.

Не использовать SQLite для имитации PostgreSQL-specific behavior.
```

## M5-B. PostgreSQL reflection

```text
$structuraguard-database $structuraguard-tests $structuraguard-security

Реализуй PostgreSQL inspection adapter.

Требования:
- SQLAlchemy 2.x;
- отдельный inspection connection;
- schemas, tables/views, columns/native types, nullable/default, PK, FK, unique, checks, indexes, comments;
- statement timeout и корректное закрытие ресурсов;
- пароль/DSN не попадает в exceptions/logs;
- никаких DDL/DML;
- integration tests через Testcontainers с реальными constraints/comments/generated columns;
- проверка недостаточных прав возвращает typed error.
```

## M5-C. Fingerprint и FK graph

```text
$structuraguard-database $structuraguard-tests

Реализуй stable database fingerprint и dependency graph.

Требования:
- fingerprint строится из canonical catalog без volatile metadata;
- одинаковая логическая схема даёт одинаковый SHA-256 независимо от порядка reflection;
- значимое изменение схемы меняет fingerprint;
- graph строится по FK;
- topological load order;
- cycles и self-references обнаруживаются и описываются;
- join tables не угадываются без достаточных признаков;
- unit/property tests для DAG, cycles, multiple schemas и composite FK.
```

Рекомендуемый commit:

```text
feat: add database inspection catalog and dependency graph
```

---

# 11. M6 — Source Profiler

## Ветка и профиль

```bash
git switch -c feat/m06-source-profiler
codex -C . --profile quality
```

## Последовательность

```text
M6-P → M6-I → G1 → G2 → G3 → G4 → G5 при необходимости → G6
```

## M6-P. План

```text
$structuraguard-plan $structuraguard-python

Цель: спланировать bounded Source Profiler M6.

Извлеки только FR-013, 11.4–11.6, 12 и M6.
Изучи normalized model и parser batching contract.
Сохрани план в `docs/plans/M06_source_profiler.md`.

План должен определить:
- online/bounded statistics;
- sampling strategy;
- type inference и ambiguity;
- null/unique ratios;
- min/max/string lengths;
- patterns email/phone/UUID/URL/date/money/INN;
- identity hints;
- PII classification interfaces;
- stable source fingerprint;
- memory/performance limits;
- property-based tests.
```

## M6-I. Реализация

```text
$structuraguard-python $structuraguard-tests $structuraguard-security

Реализуй `docs/plans/M06_source_profiler.md`.

Требования:
- profiler принимает normalized batches и не зависит от исходного формата;
- samples строго ограничены и не сохраняют весь dataset;
- статистики обновляются инкрементально;
- типы не «угадываются» при конфликте: возвращается ranked/ambiguous result;
- raw PII examples не попадают в безопасные summaries/logs;
- Russian/English number/date formats различаются по locale policy;
- `Decimal` для денег;
- source fingerprint детерминирован и документирован;
- tests для пустых, смешанных, Unicode и очень больших логических потоков.
```

Рекомендуемый commit:

```text
feat: add bounded source profiler
```

---

# 12. M7 — Deterministic Mapper

## Ветка и профиль

```bash
git switch -c feat/m07-deterministic-mapper
codex -C . --profile quality
```

## Последовательность

```text
M7-P → M7-I → G1 → G3 → G4 → G5 при необходимости → G6
```

## M7-P. План

```text
$structuraguard-plan $structuraguard-python

Цель: спланировать deterministic mapping M7 без вызова LLM.

Извлеки через индекс только разделы 10–14 и M7.
Изучи SourceProfile, DatabaseCatalog, FK graph и MappingCandidate DTO.
Сохрани план в `docs/plans/M07_deterministic_mapper.md`.

Определи:
- candidate generation и top-k pruning;
- normalized names/transliteration/tokenization;
- aliases/Semantic Catalog;
- type/value-pattern compatibility;
- source structural context;
- DB graph context;
- signal breakdown;
- calibration/weights config;
- deterministic tie-breaking;
- ambiguity thresholds;
- tests на русском и английском.

Не использовать embeddings или LLM в этом milestone.
```

## M7-I. Реализация

```text
$structuraguard-python $structuraguard-tests

Реализуй `docs/plans/M07_deterministic_mapper.md`.

Требования:
- чистые функции scoring отделены от I/O;
- exact/normalized/alias/type/pattern/structure/graph signals видимы в результате;
- final score вычисляет SDK, а не внешний provider;
- веса валидируются и конфигурируются;
- tie-breaking стабилен;
- несовместимый DB type не может победить только из-за сходства имени;
- candidate list ограничен;
- unmapped и ambiguous состояния явные;
- tests покрывают ложные совпадения, одинаковые column names в разных tables, Russian aliases и composite context.

Не генерировать MappingPlan/SQL сверх утверждённого scope, если это отложено планом.
```

Рекомендуемый commit:

```text
feat: add deterministic mapping candidate engine
```

---

# 13. M8 — LLM layer и semantic mapper

## Ветка и профиль

```bash
git switch -c feat/m08-llm-layer
codex -C . --profile quality
```

## Последовательность

```text
M8-P → M8-A → M8-B → M8-C → M8-D → G1 → G2 → G3 → G4 → G5 при необходимости → G6
```

## M8-P. План

```text
$structuraguard-plan $structuraguard-llm

Цель: спланировать provider-neutral LLM layer, semantic mapping и policy-aware routing M8.

Извлеки только 11.8–11.9, 12, 14, 19, 20.2, 20.10–20.11 и M8.
Изучи существующий LLMProvider contract и deterministic candidates.
Сохрани план в `docs/plans/M08_llm_layer.md`.

Разбей на части:
A. `FakeLLMProvider` и `NoLLMProvider` + common contract suite;
B. `OpenAICompatibleProvider`;
C. router/retry/fallback/privacy policy;
D. semantic mapper поверх top-k candidates.

Зафиксируй strict structured response, normalized errors, metadata/usage, no credentials/tools/SQL, prompt-injection boundaries и отсутствие paid calls в default tests.
```

## M8-A. Fake и NoLLM providers

```text
$structuraguard-llm $structuraguard-python $structuraguard-tests

Реализуй часть A плана M8.

Требования:
- общий provider contract suite;
- `FakeLLMProvider` поддерживает scripted success, malformed response, timeout, rate limit и unavailable;
- `NoLLMProvider` предоставляет явную deterministic-only policy, а не фиктивный ответ;
- provider capabilities типизированы;
- request/response metadata не содержит secrets;
- tests полностью deterministic с controlled clocks/IDs.
```

## M8-B. OpenAI-compatible provider

```text
$structuraguard-llm $structuraguard-security $structuraguard-tests

Реализуй `OpenAICompatibleProvider` как infrastructure adapter.

Требования:
- core/domain не импортирует provider SDK;
- async HTTP client внедряется или управляется явным lifecycle;
- supported capabilities проверяются до запроса;
- strict structured output/JSON Schema там, где поддерживается;
- timeout, cancellation, rate limit, unavailable и malformed response превращаются в typed errors;
- API key/headers/base URL credentials не логируются;
- retry только для безопасных transient failures;
- fake HTTP transport в default tests;
- никакого реального API key и внешних вызовов в CI.
```

## M8-C. Router, retry, fallback и privacy

```text
$structuraguard-llm $structuraguard-security $structuraguard-tests

Реализуй policy-aware LLM router.

Минимальные режимы:
- `fixed`;
- `no_llm`;
- `local_only`;
- `privacy_first`;
- `fallback`.

Требования:
- fallback не может понизить допустимый data classification;
- restricted data не отправляется cloud provider;
- все попытки, latency, model/provider и fallback reason видимы в metadata;
- budget по calls/tokens/time;
- circuit/retry semantics детерминированы и тестируемы;
- при отсутствии разрешённого provider возвращается явная policy error;
- raw sensitive values не попадают в router logs.
```

## M8-D. Semantic mapper

```text
$structuraguard-llm $structuraguard-python $structuraguard-security

Реализуй semantic mapper поверх deterministic top-k candidates.

Требования:
- LLM получает только candidate identifiers, безопасные descriptions и bounded examples;
- document/source content маркируется как недоверенные данные;
- модель не получает credentials, tools, DB connection и arbitrary SQL capability;
- response строго валидируется Pydantic/JSON Schema;
- любой table/column identifier повторно проверяется по DatabaseCatalog и candidate list;
- LLM score является только одним signal; итоговый confidence считает SDK;
- неизвестные identifiers, SQL fragments и prompt-injection attempts отклоняются;
- tests: valid, malformed, unknown target, injected instruction, timeout, rate limit, capability mismatch.
```

Рекомендуемый commit:

```text
feat: add provider-neutral llm semantic mapping
```

---

# 14. M9 — MappingPlan Validator

## Ветка и профиль

```bash
git switch -c feat/m09-mapping-plan-validator
codex -C . --profile quality
```

## Последовательность

```text
M9-P → M9-I → G1 → G2 → G3 → G4 → G5 при необходимости → G6
```

## M9-P. План

```text
$structuraguard-plan $structuraguard-database

Цель: спланировать строгую валидацию декларативного MappingPlan M9.

Извлеки только 14.1, 16, 18, 20.3–20.4 и M9.
Изучи MappingPlan DTO, DatabaseCatalog, fingerprints и policy types.
Сохрани план в `docs/plans/M09_mapping_plan_validator.md`.

План должен покрыть:
- existence/writability;
- schema/table/column allowlist и denylist;
- type compatibility;
- identity/upsert keys;
- FK/relation resolution;
- confidence thresholds;
- schema drift;
- forbidden operations/SQL fragments;
- all-issues report с machine-readable codes;
- deterministic validation order;
- property/security tests.
```

## M9-I. Реализация

```text
$structuraguard-database $structuraguard-python $structuraguard-security $structuraguard-tests

Реализуй `docs/plans/M09_mapping_plan_validator.md`.

Отклоняй планы с:
- неизвестными schemas/tables/columns;
- denylisted или неразрешёнными targets;
- generated/non-writable columns;
- несовместимыми types;
- invalid/неподтверждённым identity key;
- unresolved FK relations;
- fingerprint mismatch;
- confidence ниже policy threshold;
- SQL fragments или forbidden load operation;
- попыткой DDL/system-table access.

Требования:
- собрать все независимые issues, не только первое;
- error codes стабильны;
- пользовательские identifiers не интерполируются в SQL;
- validator является чистой логикой и не меняет БД;
- tests включают malicious identifiers и schema drift.
```

Рекомендуемый commit:

```text
feat: validate declarative mapping plans
```

---

# 15. M10 — Validation Engine и нормализация

## Ветка и профиль

```bash
git switch -c feat/m10-validation-engine
codex -C . --profile quality
```

## Последовательность

```text
M10-P → M10-A → M10-B → M10-C → M10-D → G1 → G2 → G3 → G4 → G5 при необходимости → G6
```

## M10-P. План

```text
$structuraguard-plan $structuraguard-python

Цель: спланировать M10 — conservative normalization и многоуровневый Validation Engine.

Извлеки только разделы 15–16 и M10.
Изучи DTO validation issues, MappingPlan и provenance.
Сохрани план в `docs/plans/M10_validation_engine.md`.

Разбей:
A. normalizer registry и locale-aware built-ins;
B. JSON Schema Draft 2020-12;
C. DB constraints и safe business-rule DSL;
D. provenance validation и all-errors report.

Определи порядок уровней, immutability/raw value preservation, error codes и repair boundaries. Никакого `eval`.
```

## M10-A. Normalizers

```text
$structuraguard-python $structuraguard-tests

Реализуй conservative normalizer registry.

Минимум:
- trim/empty-to-null;
- boolean;
- integer/Decimal;
- date/datetime с явной locale policy;
- phone/email canonical checks;
- UUID;
- configurable custom normalizer protocol.

Требования:
- исходное raw value сохраняется;
- каждая transformation записывается;
- неоднозначная дата не угадывается;
- money только `Decimal`;
- normalizers не выполняют I/O;
- property-based tests на Unicode, separators, round-trip и ambiguous cases.
```

## M10-B. JSON Schema

```text
$structuraguard-python $structuraguard-tests $structuraguard-security

Реализуй validation по JSON Schema Draft 2020-12.

Требования:
- meta-validation схемы;
- локальные refs по утверждённой policy;
- запрет remote refs/network;
- лимиты depth/properties/regex complexity в пределах доступных controls;
- сбор всех issues с JSON path и code;
- schema cache bounded и без global mutable state;
- tests для malformed schema, recursive/oversized schema и multiple errors.
```

## M10-C. DB constraints и business rules

```text
$structuraguard-python $structuraguard-database $structuraguard-security $structuraguard-tests

Реализуй deterministic validation DB constraints и безопасный business-rule DSL.

Требования:
- NOT NULL, length, numeric bounds, enum/check representations, unique/FK prechecks в границах catalog/adapter;
- rules только из allowlisted операций;
- никакого `eval`, `exec`, dynamic import или SQL expressions от пользователя;
- typed operands и явная обработка null;
- все issues собираются;
- tests для rule injection, wrong types, composite uniqueness и date/sum invariants.
```

## M10-D. Provenance validation и отчёт

```text
$structuraguard-python $structuraguard-tests

Реализуй provenance validation и итоговый ValidationReport.

Требования:
- source reference существует и принадлежит текущему source fingerprint;
- значения/нормализации связываются с исходными locations;
- ненулевые extracted values без required provenance получают issue;
- report содержит raw/normalized references без утечки restricted values в safe summary;
- deterministic issue ordering;
- агрегаты valid/invalid/warnings;
- tests для поддельных pointers, чужого source ID и missing evidence.
```

Рекомендуемый commit:

```text
feat: add normalization and validation engine
```

---

# 16. M11 — Staging и transactional Loader

## Ветка и профиль

```bash
git switch -c feat/m11-staging-loader
codex -C . --profile quality
```

## Последовательность

```text
M11-P → M11-A → M11-B → M11-C → M11-D → G1 → G2 → G3 → G4 → G5 при необходимости → G6
```

## M11-P. План

```text
$structuraguard-plan $structuraguard-database

Цель: спланировать безопасные staging и loader M11 для PostgreSQL.

Извлеки только разделы 17–18, 20.3–20.4 и M11.
Изучи MappingPlan validator, DatabaseCatalog graph и ValidationReport.
Сохрани план в `docs/plans/M11_staging_loader.md`.

Разбей:
A. StagingStore contract/implementation и run lifecycle;
B. dry-run и execution plan;
C. insert_only/upsert/FK resolution/load order;
D. rollback/quarantine/idempotency/schema recheck.

Зафиксируй separate writer user, parameterized values, validated identifiers, transaction boundaries, cancellation semantics и integration test matrix.
```

## M11-A. Staging

```text
$structuraguard-database $structuraguard-python $structuraguard-tests

Реализуй PostgreSQL staging layer.

Требования:
- run/batch/record metadata;
- raw/normalized/mapping/validation/provenance references по data-retention policy;
- writer user не получает DDL production schema;
- staging schema/table creation не выполняется неожиданно при import/ingest; bootstrap отделён и явен;
- batch writes параметризованы;
- lifecycle statuses и cleanup policy;
- contract tests + PostgreSQL integration tests.
```

## M11-B. Dry run

```text
$structuraguard-database $structuraguard-tests

Реализуй настоящий `dry_run`.

Требования:
- не меняет target tables и существующие staging records сверх явно разрешённого audit режима;
- формирует ordered execution plan;
- сообщает planned inserts/updates/skips/quarantine;
- проверяет MappingPlan, permissions, schema fingerprint и resolvability;
- SQL/parameters не раскрывают secrets;
- integration test доказывает нулевое изменение target DB до/после.
```

## M11-C. Insert, upsert и FK resolution

```text
$structuraguard-database $structuraguard-python $structuraguard-tests

Реализуй `insert_only` и `upsert`.

Требования:
- upsert только по подтверждённому PK/unique/natural key;
- dependency graph определяет load order;
- FK lookup/resolution явный и bounded;
- composite keys поддерживаются согласно plan;
- values параметризованы;
- identifiers берутся только из validated catalog/plan;
- generated columns не записываются;
- bulk batches не создают дубли/пропуски;
- PostgreSQL integration tests для parent/child, duplicate key и update path.
```

## M11-D. Atomicity, quarantine и idempotency

```text
$structuraguard-database $structuraguard-security $structuraguard-tests

Реализуй:
- atomic transaction по умолчанию;
- rollback при critical failure/cancellation;
- `quarantine_invalid` как отдельный явный mode;
- idempotency key + source/plan fingerprints;
- schema fingerprint recheck непосредственно перед load;
- обработку concurrency/race на повторном ingest;
- audit metadata без secrets.

Добавь PostgreSQL integration tests:
- полный success;
- validation failure;
- FK failure;
- exception в середине batch;
- cancellation;
- duplicate request;
- concurrent same idempotency key;
- schema drift перед commit.
```

Рекомендуемый commit:

```text
feat: add staging and transactional database loader
```

---

# 17. M12 — Security layer

## Ветка и профиль

```bash
git switch -c feat/m12-security-layer
codex -C . --profile quality
```

## Последовательность

```text
M12-P → M12-A → M12-B → M12-C → M12-D → G1 → G2 → G3 → G4 → G5 при необходимости → G6
```

## M12-P. План

```text
$structuraguard-plan $structuraguard-security

Цель: спланировать M12 — единый Security Policy layer и audit chain, не дублируя controls уже встроенные в parsers/DB/LLM.

Извлеки только раздел 20, 30.4, 32.5 и M12.
Прочитай `docs/threat-model.md` и текущие security tests.
Сохрани план в `docs/plans/M12_security_layer.md`.

Разбей:
A. resource/file/source policy;
B. PII/secrets classification и redaction;
C. prompt-injection signals и LLM routing restrictions;
D. database policy, audit events/HMAC chain и sandbox runner interface.

Для каждого control укажи trust boundary, deny-by-default behavior, regression tests и residual risk.
```

## M12-A. Resource и source policy

```text
$structuraguard-security $structuraguard-python $structuraguard-tests

Реализуй централизованную configurable policy лимитов.

Минимум:
- file/stream bytes;
- records/columns/nesting/text/chunks;
- parser duration;
- LLM calls/tokens/time;
- DB batch/query limits;
- cancellation propagation.

Требования:
- лимиты проверяются до опасного накопления;
- deny-by-default для неподдерживаемого/опасного input;
- typed security errors;
- audit event без raw restricted data;
- tests на boundary и превышение на один элемент.
```

## M12-B. PII, secrets и redaction

```text
$structuraguard-security $structuraguard-python $structuraguard-tests

Реализуй configurable detection/classification/redaction.

Категории минимум:
- email/phone;
- Russian INN/SNILS/passport patterns с документированными false positives;
- card-like values с безопасной проверкой;
- API keys/tokens/private-key blocks/password-like fields;
- custom patterns.

Требования:
- classifications PUBLIC/INTERNAL/CONFIDENTIAL/RESTRICTED;
- bounded scans;
- reversible placeholder map хранится отдельно и защищён policy;
- logs/reports используют safe summaries;
- raw secrets никогда не попадают в exception message;
- false-positive/false-negative fixtures и property tests.
```

## M12-C. Prompt injection и LLM routing

```text
$structuraguard-security $structuraguard-llm $structuraguard-tests

Реализуй prompt-injection signal detection как дополнительный control, не как единственную защиту.

Требования:
- source text всегда остаётся untrusted data;
- detector возвращает signals/severity/evidence location;
- high-risk policy может запретить cloud LLM или перевести run в NEEDS_REVIEW;
- модель по-прежнему не имеет tools/DB/SQL capability;
- detector не утверждает абсолютную безопасность;
- multilingual attack fixtures;
- tests на direct/indirect injection и benign false positives;
- routing regression tests для restricted/confidential data.
```

## M12-D. DB policy, audit chain и sandbox runner

```text
$structuraguard-security $structuraguard-database $structuraguard-parser $structuraguard-tests

Реализуй оставшиеся controls M12.

DB policy:
- schemas/tables/columns allowlist и denylist;
- separate roles;
- DDL forbidden;
- system catalogs/unsafe identifiers denied.

Audit:
- typed events;
- canonical serialization;
- HMAC hash chain с key abstraction;
- tamper detection;
- secret-safe payloads;
- documented limitation при компрометации key/store.

Sandbox:
- `InProcessParserRunner` и `SandboxParserRunner` protocols/implementations согласно architecture;
- strict policy требует sandbox для configured risky parsers;
- core SDK не обещает container isolation, которую не может обеспечить;
- timeout/cancellation/result-size limits;
- no shell interpolation.

Добавь security regression tests и threat-model traceability.
```

Рекомендуемый commit:

```text
feat: add security policy and tamper-evident audit
```

---

# 18. Интеграционный pipeline SDK facade

После M12 нужен отдельный вертикальный этап, который соединяет готовые компоненты через публичный SDK API. Он следует ТЗ, даже если не выделен отдельным номером.

## Ветка и профиль

```bash
git switch -c feat/sdk-ingest-orchestrator
codex -C . --profile quality
```

## Последовательность

```text
ORCH-P → ORCH-I → G1 → G2 → G3 → G4 → G5 при необходимости → G6
```

## ORCH-P. План

```text
$structuraguard-plan $structuraguard-python

Цель: спланировать end-to-end SDK orchestrator от source до report, используя только уже реализованные contracts/adapters.

Извлеки разделы 6, 7, 23–26 и критерии приёмки 1–30.
Изучи текущие facades и component APIs.
Сохрани план в `docs/plans/SDK_ingest_orchestrator.md`.

План должен покрыть методы:
- `inspect_source`;
- `inspect_database`;
- `create_plan`;
- `validate_plan`;
- `execute`;
- `ingest`;
- sync wrappers.

Зафиксируй state transitions, event hooks, cancellation, retries boundaries, no hidden fallback, dry-run, security gates и composition root.
```

## ORCH-I. Реализация

```text
$structuraguard-python $structuraguard-security $structuraguard-tests

Реализуй `docs/plans/SDK_ingest_orchestrator.md`.

Требования:
- pipeline следует утверждённым states;
- зависимости внедряются через constructor/configuration, global registry нет;
- каждый stage принимает/возвращает typed DTO;
- ошибки нормализуются, но первопричина не скрывается;
- cancellation и timeout проходят через stages;
- no_llm flow работает;
- dry_run не изменяет target DB;
- security policy выполняется до внешнего LLM и перед load;
- final IngestResult содержит source/DB/plan fingerprints, provider metadata, validation/load/security reports и audit references;
- sync facade корректно обрабатывает ситуацию с уже запущенным event loop согласно documented contract;
- end-to-end tests с fake parser/LLM/DB плюс PostgreSQL integration happy/error paths.
```

Рекомендуемый commit:

```text
feat: connect sdk ingestion pipeline
```

---

# 19. M13 — демонстрационный проект

## Ветка и профиль

```bash
git switch -c feat/m13-demo-application
codex -C . --profile quality
```

## Последовательность

```text
M13-P → M13-A → M13-B → M13-C → M13-D → G1 → G2 → G3 → G4 → G5 при необходимости → G6
```

## M13-P. План

```text
$structuraguard-plan

Цель: спланировать отдельный demo project M13, доказывающий встраиваемость SDK и не загрязняющий core framework dependencies.

Извлеки только разделы 2, 23, 28 и M13.
Изучи public SDK API, examples и repository layout.
Сохрани план в `docs/plans/M13_demo_application.md`.

Разбей:
A. FastAPI API;
B. worker/job execution abstraction;
C. minimal UI;
D. Docker Compose/PostgreSQL/demo data.

Определи API contracts, auth/demo boundaries, file limits, secret handling, progress events и e2e scenario. Core package не импортирует FastAPI/Celery/UI.
```

## M13-A. FastAPI demo API

```text
$structuraguard-python $structuraguard-security $structuraguard-tests

Создай отдельное приложение `apps/demo-api`, которое использует только public StructuraGuard SDK.

Маршруты минимум:
- `POST /documents/analyze`;
- `POST /documents/plan`;
- `POST /documents/import`;
- `GET /runs/{run_id}`;
- `GET /runs/{run_id}/mapping`;
- `GET /runs/{run_id}/validation`;
- `GET /runs/{run_id}/security`.

Требования:
- SDK не импортирует FastAPI;
- upload streaming и size limits;
- DSN/API keys только из demo app config/secrets;
- безопасные response DTO без raw secrets;
- корректные HTTP status/error mappings;
- dependency injection и lifecycle cleanup;
- API tests с fake SDK и минимум один integration path.
```

## M13-B. Worker

```text
$structuraguard-python $structuraguard-tests

Создай worker integration для длительных ingest runs в `apps/demo-worker`.

Требования:
- SDK не зависит от выбранной queue technology;
- idempotent job dispatch;
- cancellation/status/progress;
- retries только на безопасном уровне и без двойной загрузки;
- serialization только typed task data, без DB connection objects;
- worker secrets не попадают в task payload/logs;
- tests на duplicate delivery и failed/retried job.

Используй минимальную технологию, утверждённую в plan; не добавляй инфраструктуру без необходимости.
```

## M13-C. Минимальный UI

```text
$structuraguard-docs $structuraguard-security

Создай минимальный demo UI, достаточный для защиты диплома.

Пользователь должен уметь:
- загрузить поддерживаемый файл;
- выбрать target DB connection из заранее настроенного списка, не вводя DSN в браузере;
- увидеть detected format/source profile;
- увидеть mapping candidates/plan/confidence;
- подтвердить или отклонить ambiguous mappings;
- выполнить dry run;
- запустить import;
- увидеть validation/load/security reports и provenance.

Требования:
- не отображать raw secrets;
- не рендерить raw HTML/LLM output;
- обрабатывать loading/error/empty states;
- UI не становится обязательной dependency SDK.
```

## M13-D. Docker Compose и demo scenario

```text
$structuraguard-database $structuraguard-security $structuraguard-tests $structuraguard-docs

Создай воспроизводимое локальное demo-окружение.

Минимум:
- PostgreSQL;
- demo-api;
- worker, если он выбран;
- UI;
- отдельные inspection/writer users;
- migrations/bootstrap только явной командой;
- health checks;
- sample target schema и sample input files;
- `.env.example` без secrets;
- no privileged containers, Docker socket или host network.

Добавь smoke/e2e инструкцию и автоматизированный happy-path test, насколько это практически возможно.
```

Рекомендуемый commit:

```text
feat: add demo application for structuraguard sdk
```

---

# 20. M14 — Evaluation и экспериментальная часть

## Ветка и профиль

```bash
git switch -c feat/m14-evaluation
codex -C . --profile quality
```

## Последовательность

```text
M14-P → M14-A → M14-B → M14-C → G1 → G3 → G4 → G5 при необходимости → G6
```

## M14-P. План

```text
$structuraguard-plan $structuraguard-tests

Цель: спланировать воспроизводимую evaluation framework M14 для сравнения rules-only, LLM-only и hybrid pipeline.

Извлеки только разделы 30–33 и M14.
Изучи текущие adapters, reports и sample data.
Сохрани план в `docs/plans/M14_evaluation.md`.

План должен определить:
- dataset manifest и лицензирование/синтетическое происхождение;
- target DB schemas;
- gold MappingPlan/expected values;
- experiment configurations;
- fixed seeds/provider snapshots;
- метрики mapping/load/LLM/performance/security;
- separation train/calibration/test, если веса настраиваются;
- output CSV/JSON/Markdown;
- способ не коммитить sensitive data и огромные generated artifacts.
```

## M14-A. Dataset и fixtures

```text
$structuraguard-tests $structuraguard-security $structuraguard-docs

Создай структуру `evals/` и минимальный репрезентативный dataset manifest.

Требования:
- форматы CSV/TSV, JSON/JSONL, XML, XLSX, HTML, text-layer PDF, DOCX, LOG/TXT;
- несколько target DB domains;
- gold mapping plans и expected normalized values;
- benign, malformed, ambiguous и security attack variants;
- синтетические/обезличенные данные;
- source/license metadata;
- deterministic generators для расширения набора;
- никаких реальных персональных данных или secrets.

Не генерируй сразу сотни тяжёлых бинарных файлов, если достаточно manifest + generator + минимальных fixtures.
```

## M14-B. Evaluation runner и метрики

```text
$structuraguard-python $structuraguard-tests

Реализуй evaluation runner.

Конфигурации минимум:
- rules-only;
- LLM-only с Fake/recorded provider для CI;
- hybrid rules + LLM + validation.

Метрики:
- table/column/entity/relation accuracy;
- precision/recall/F1/top-k recall/ambiguity;
- valid load/incorrect insert/rollback/duplicate/FK/idempotency;
- response schema validity/fallback/calls/tokens;
- p50/p95/throughput/peak memory, если измеримо воспроизводимо;
- prompt-injection ASR/PII leakage/unauthorized target attempts.

Требования:
- fixed seeds;
- machine-readable outputs;
- отсутствие paid calls в default CI;
- корректная обработка partial failures;
- tests формул метрик и aggregation.
```

## M14-C. Отчёт эксперимента

```text
$structuraguard-docs $structuraguard-tests

Создай воспроизводимый evaluation report на основании фактически полученных результатов.

Требования:
- методика;
- версии SDK/config/dataset/provider;
- baseline и hybrid comparison;
- таблицы метрик;
- ограничения и threats to validity;
- случаи ошибок;
- security results;
- команды воспроизведения;
- не делать выводов, не подтверждённых данными.

Автоматизируй построение Markdown/CSV/JSON отчёта. Generated charts должны быть воспроизводимы и не зависеть от ручного редактирования.
```

Рекомендуемый commit:

```text
feat: add reproducible evaluation framework
```

---

# 21. Финальная приёмка SDK

## Ветка и профиль

```bash
git switch -c chore/final-acceptance
codex -C . --profile quality
```

## Последовательность

```text
FINAL-A → G7 → FINAL-B → G4 → при необходимости G5 → G6
```

## FINAL-A. Матрица критериев приёмки

```text
$structuraguard-review $structuraguard-tests $structuraguard-security

Проведи полную приёмку StructuraGuard по разделу 33 ТЗ.

Создай `docs/acceptance/FINAL_ACCEPTANCE.md` с таблицей для каждого из 33 критериев:
- идентификатор;
- статус PASS/PARTIAL/FAIL;
- реализация `path:line`;
- подтверждающий test;
- команда проверки;
- residual risk или gap.

Не отмечай PASS без проверяемого доказательства.
Отдельно проверь архитектурные инварианты раздела 40, public API, no import-time side effects, no executable SQL from LLM, dry-run, staging, rollback, idempotency, security routing и reproducible evaluation.

Код пока не меняй. Сначала сформируй честный gap report.
```

## FINAL-B. Закрытие gaps

```text
$structuraguard-plan $structuraguard-python $structuraguard-tests

На основании `docs/acceptance/FINAL_ACCEPTANCE.md` сформируй минимальный план закрытия только критериев со статусом PARTIAL/FAIL.

Сохрани его в `docs/plans/FINAL_gap_closure.md`.
Сгруппируй gaps по severity и зависимости.
Не добавляй новые функции вне ТЗ.
После плана реализуй только блокирующие gaps по одному, добавляя tests и обновляя acceptance matrix после каждого исправления.
Для security/DB/parser/LLM gap явно подключай соответствующий профильный skill.
```

Рекомендуемый commit:

```text
chore: complete final sdk acceptance checks
```

---

# 22. Специальные пайплайны после основной разработки

## 22.1. Исправление воспроизводимого бага

### Когда применять

- падает test;
- данные дублируются/теряются;
- неверное сопоставление;
- race condition;
- regression;
- unexpected exception.

### Последовательность

```text
BUG-1 → BUG-2 → G4 → G6
```

### BUG-1. Диагностика и исправление

```text
$structuraguard-debug $structuraguard-tests

Дефект: <вставить краткое фактическое описание>.
Команда воспроизведения: `<команда>`.
Ожидаемое поведение: <одно проверяемое предложение>.
Фактическое поведение: <ошибка/расхождение>.

Сначала воспроизведи дефект без изменения кода.
Затем:
1. найди первое неверное состояние в pipeline;
2. сформулируй не более трёх проверяемых гипотез;
3. добавь regression test, который падает на старом поведении;
4. исправь первопричину минимально;
5. запусти regression test, соседний suite, lint и typecheck;
6. не маскируй ошибку retry/default/broad exception;
7. не выполняй несвязанный рефакторинг.

В финале укажи причину, доказательство, исправление и команды проверки.
```

### BUG-2. Security review бага

Отправлять, если баг касается файлов, БД, LLM, auth, PII, logs, paths или ресурсов.

```text
$structuraguard-security

Проверь исправленный дефект как потенциальную security regression.
Определи, можно ли было использовать его для утечки, обхода policy, corruption, DoS или unauthorized DB operation.
Если да — добавь отдельный минимальный exploit regression test и проверь соседние trust boundaries.
```

---

## 22.2. Новый parser plugin

```text
$structuraguard-plan $structuraguard-parser

Цель: добавить parser plugin для формата `<FORMAT>` без изменения orchestrator.

Контекст:
- parser contract: <path>;
- пример входа: <path>;
- MIME/signature: <значения>;
- обязательный provenance: <тип>;
- ожидаемые limits: <limits>.

Сначала сохрани краткий plan в `docs/plans/parser_<format>.md`.
Затем реализуй:
- probe/detection;
- normalized batches;
- exact provenance;
- streaming, если формат потенциально большой;
- typed parse/unsupported/limit/security errors;
- registration через registry/entry point;
- contract, malformed, boundary, cancellation и malicious tests;
- optional extra, если нужна тяжёлая dependency.

Не менять MappingPlan, DB loader и core pipeline.
После реализации: `$structuraguard-tests`, `$structuraguard-security`, `$structuraguard-review`.
```

---

## 22.3. Новый database dialect

```text
$structuraguard-plan $structuraguard-database

Цель: добавить DatabaseAdapter для `<DIALECT>` без изменения domain contracts.

Зафиксируй:
- поддерживаемую версию СУБД;
- driver и license;
- reflection capabilities;
- dialect-specific types/constraints;
- read-only inspection semantics;
- writer least privilege;
- upsert syntax и ограничения;
- integration test environment.

Сохрани plan в `docs/plans/database_<dialect>.md`, затем реализуй adapter, common contract suite и реальные integration tests.
SQLite imitation не считается доказательством dialect-specific behavior.
После реализации: `$structuraguard-security`, `$structuraguard-review`.
```

---

## 22.4. Новый LLM provider

```text
$structuraguard-plan $structuraguard-llm

Цель: добавить provider adapter `<PROVIDER>` без изменения core/domain.

Определи capabilities:
- structured output;
- JSON Schema;
- context/output limits;
- local/cloud classification;
- usage metadata;
- timeout/rate-limit/error model.

Реализуй только infrastructure adapter через существующий `LLMProvider`.
Требования:
- strict response validation;
- normalized typed errors;
- injectable/mockable client;
- credentials не логируются;
- no tools/DB/SQL capability;
- privacy routing compatibility;
- common provider contract tests;
- default CI без реального network/API key.

После реализации: `$structuraguard-tests`, `$structuraguard-security`, `$structuraguard-review`, `$structuraguard-docs`.
```

---

## 22.5. Изменение публичного API

```text
$structuraguard-plan $structuraguard-python $structuraguard-docs

Цель: изменить public API `<API>` так, чтобы <наблюдаемое поведение>.

Перед реализацией:
- найди все public exports, tests, examples и integrations;
- оцени backward compatibility;
- предложи non-breaking вариант;
- если breaking change неизбежен, подготовь migration note и versioning impact;
- сохрани plan/ADR, если решение долгоживущее.

После утверждения добавь contract tests, реализацию, обнови docs/examples и выполни full review.
Не меняй unrelated API.
```

---

## 22.6. Добавление production dependency

```text
$structuraguard-plan $structuraguard-security

Оцени необходимость production dependency `<PACKAGE>` для задачи `<TASK>` до её добавления.

Сравни минимум:
- стандартную библиотеку/уже имеющиеся dependencies;
- `<PACKAGE>`;
- один реалистичный альтернативный пакет.

Проверь:
- license;
- release/maintenance status;
- Python 3.12 compatibility;
- transitive dependencies;
- known security risks;
- wheel/platform support;
- размер и import-time cost;
- возможность optional extra;
- тестируемость и exit strategy.

Сохрани короткий ADR только если dependency действительно нужна. Не устанавливай пакет до принятого решения.
```

---

## 22.7. Оптимизация производительности

```text
$structuraguard-plan $structuraguard-debug $structuraguard-tests

Цель: улучшить `<METRIC>` для `<SCENARIO>` без изменения observable behavior.

Сначала:
- воспроизведи baseline;
- добавь benchmark/performance test с фиксированными данными;
- профилируй и найди доказанный bottleneck;
- зафиксируй p50/p95/throughput/peak memory, применимые к задаче.

Затем внеси минимальную оптимизацию, повтори измерения и запусти correctness suites.
Не выполнять speculative optimization и не ухудшать limits/security/readability без измеримого выигрыша.
```

---

## 22.8. Рефакторинг без изменения поведения

```text
$structuraguard-plan $structuraguard-python $structuraguard-tests

Цель: рефакторинг `<AREA>` без изменения публичного и наблюдаемого поведения.

Сначала добавь/проверь characterization tests.
Опиши конкретную проблему: duplication/coupling/complexity/lifecycle.
Разбей refactor на небольшие steps, после каждого запускай узкие tests.
Не совмещай refactor с новой функцией, изменением API или dependency upgrade.
В конце покажи доказательство неизменности behavior и выполни `$structuraguard-review`.
```

---

## 22.9. Обновление документации без кода

```text
$structuraguard-docs

Обнови `<DOCUMENT>` для подтверждённого поведения `<FEATURE>`.

Требования:
- русский язык;
- identifiers/commands/API без перевода;
- сначала минимальный рабочий пример;
- описать exceptions, side effects, limits и security;
- не копировать ТЗ целиком;
- примеры без secrets и по возможности исполняемые;
- проверить ссылки и docs build.

Код и публичное поведение не менять.
```

---

## 22.10. Проверка чужого или большого diff

```text
$structuraguard-review

Проведи review diff `<BASE>...<HEAD>` относительно задачи `<TASK>`.

Приоритет:
1. data corruption/loss;
2. security;
3. correctness/error paths;
4. architecture/API compatibility;
5. concurrency/resource lifecycle;
6. tests;
7. performance.

Не перечисляй форматирование, которое ловят Ruff/mypy.
Каждый finding: severity, `path:line`, доказательство, impact и минимальное исправление.
Не изменяй код.
```

---

# 23. Исправление незавершённой задачи

## 23.1. Codex не выполнил tests

```text
$structuraguard-tests

Ты завершил реализацию, но не подтвердил её обязательными проверками.
Не добавляй новую функциональность.

Определи доступные quality gates из Makefile/pyproject/AGENTS.md и последовательно выполни минимально необходимые проверки текущего diff.
Если окружение блокирует команду, покажи точную команду, полный существенный фрагмент ошибки и минимальный способ разблокировки.
Не называй задачу готовой без фактического результата.
```

## 23.2. Codex вышел за scope

```text
$structuraguard-review

Сравни текущий diff с plan-файлом текущей задачи и найди изменения вне scope.
Ничего не меняй.

Раздели изменения на:
- необходимые;
- допустимые сопутствующие;
- несвязанные/спекулятивные.

Для каждой несвязанной правки укажи файлы и предложи безопасно удалить её из текущего diff, не затрагивая необходимую работу.
```

После отчёта:

```text
$structuraguard-python

Удалить только изменения, отмеченные последним review как несвязанные со scope. Сохрани необходимые изменения и tests. Затем запусти узкие проверки и покажи новый diff stat.
```

## 23.3. Codex хочет ослабить test/lint/typecheck

```text
$structuraguard-debug $structuraguard-tests

Не ослабляй и не отключай failing test/lint/typecheck.
Найди первопричину падения.
Запрещено использовать skip, xfail, noqa, broad `type: ignore`, исключение файла из checks или понижение strictness без отдельного доказанного требования.
Исправь код или узкую ошибочную конфигурацию, затем повтори проверку.
```

## 23.4. Остались Critical/High findings

```text
$structuraguard-security $structuraguard-debug $structuraguard-tests

Останови завершение milestone. Исправь все Critical/High findings последнего review по одному.
Для каждого:
- воспроизведение/exploit test;
- минимальное исправление первопричины;
- regression test;
- соседние security/correctness checks.

После этого заново выполни `$structuraguard-review`. Не переходи к документации или commit, пока Critical/High не закрыты.
```

## 23.5. Контекст сессии стал слишком большим

Перед завершением старой сессии:

```text
$structuraguard-docs

Обнови `docs/codex/PROJECT_STATE.md` и plan-файл текущего milestone перед переносом работы в новую сессию.
Зафиксируй только:
- завершённые шаги;
- изменённые contracts;
- команды и результаты;
- текущий failing test/blocker;
- следующий конкретный шаг;
- residual risks.

Не вставляй большие code snippets и не пересказывай историю обсуждения.
```

В новой сессии:

```text
Продолжи текущий milestone.
Сначала прочитай `AGENTS.md`, `docs/codex/PROJECT_STATE.md` и plan-файл текущего milestone, затем только непосредственно затронутые файлы/tests.
Не перечитывай полное ТЗ.
Проверь `git status` и продолжи с указанного в PROJECT_STATE следующего шага.
```

---

# 24. Команды Git между milestone

Codex лучше не просить автоматически коммитить, пока вы сами не просмотрели diff.

Перед milestone:

```bash
git status --short
git switch main
git pull --ff-only
git switch -c <branch-name>
```

После G6:

```bash
git status --short
git diff --check
git diff --stat
git diff
```

Если всё корректно:

```bash
git add <конкретные-файлы-и-каталоги>
git diff --cached --stat
git diff --cached
git commit -m "<recommended message>"
```

После merge/commit переходите к следующему milestone в новой сессии Codex.

---

# 25. Краткая шпаргалка последовательностей

## Обычный milestone ядра

```text
$structuraguard-plan
→ профильный implementation skill + $structuraguard-python
→ $structuraguard-tests
→ $structuraguard-security при trust boundary
→ $structuraguard-docs
→ $structuraguard-review
→ исправление findings
→ закрытие milestone
```

## Parser

```text
$structuraguard-plan
→ $structuraguard-parser + $structuraguard-python
→ $structuraguard-tests
→ $structuraguard-security
→ $structuraguard-docs
→ $structuraguard-review
```

## Database

```text
$structuraguard-plan
→ $structuraguard-database + $structuraguard-python
→ $structuraguard-tests
→ $structuraguard-security
→ $structuraguard-docs
→ $structuraguard-review
```

## LLM

```text
$structuraguard-plan
→ $structuraguard-llm + $structuraguard-python
→ $structuraguard-tests
→ $structuraguard-security
→ $structuraguard-docs
→ $structuraguard-review
```

## Баг

```text
$structuraguard-debug
→ failing regression test
→ minimal fix
→ $structuraguard-tests
→ $structuraguard-security при необходимости
→ $structuraguard-review
```

## Финальный релиз

```text
Acceptance matrix
→ gap closure
→ full quality gate
→ security review
→ wheel install smoke test
→ docs/evaluation verification
→ final review
```
