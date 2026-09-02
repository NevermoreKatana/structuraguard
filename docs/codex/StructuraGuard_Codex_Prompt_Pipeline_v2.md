# StructuraGuard — полный конвейер запросов для Codex

Версия документа: **2.0 — гибридный технический и LLM-семантический парсинг**.

Этот документ задаёт порядок разработки StructuraGuard SDK через Codex и проектные skills из `.agents/skills/`.

Главное уточнение версии 2.0:

```text
Технический parser
    читает контейнер/формат и сохраняет физическую структуру
        ↓
Structure Analyzer
    определяет границы записей, заголовки и возможную схему
        ↓
LLM Semantic Parser
    понимает неизвестную структуру и смысл данных
        ↓
ParsePlan
    описывает, как преобразовать источник в нормализованные сущности
        ↓
MappingPlan
    описывает, куда записать сущности в целевой БД
```

`ParsePlan` и `MappingPlan` являются разными декларативными объектами. LLM участвует и в понимании структуры источника, и в смысловом сопоставлении с БД, но не читает бинарный формат самостоятельно, не выполняет SQL и не определяет корректность собственного ответа.

## 1. Как пользоваться документом

### 1.1. Базовое правило

Не отправляйте Codex весь документ одним сообщением. Выполняйте один milestone за раз и отправляйте запросы строго последовательно.

Для каждого milestone:

1. Обновите локальный `main`.
2. Создайте отдельную Git-ветку.
3. Запустите Codex из корня репозитория с профилем `quality`.
4. Отправьте запрос на планирование.
5. Просмотрите сохранённый план.
6. Отправляйте запросы реализации по одному.
7. После реализации запустите тестирование, security review при необходимости, документацию и review.
8. Не переходите дальше, пока текущий milestone не прошёл quality gates.
9. Просмотрите `git diff`, создайте commit и отправьте ветку в remote.
10. Создайте Pull Request в `main`.
11. После успешных CI/review выполните merge.
12. Следующий milestone начинайте от обновлённого `main` в новой сессии Codex.

Внутри одного milestone лучше сохранять одну сессию: Codex помнит план и уже просмотренные файлы. После merge начинайте новую сессию, чтобы не раздувать контекст.

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

### 1.4. Текущее состояние проекта после готового M1

Для проекта, где **M1 уже завершён**, повторять его не требуется.

Порядок продолжения:

```text
проверить, что PR M1 merged в main
        ↓
создать ветку feat/m02-domain-contracts
        ↓
начать обновлённый M2
```

M1 остаётся совместимым, потому что он содержит только каркас пакета, конфигурацию, фасады, исключения и quality tooling. Новые типы `ExtractedSource`, `ParsePlan` и `SemanticStructureAnalyzer` добавляются в M2.

Если M1 ещё не merged:

```bash
git push -u origin feat/m01-sdk-scaffold
# Создать PR feat/m01-sdk-scaffold → main, дождаться CI/review и выполнить merge.

git switch main
git pull --ff-only
git switch -c feat/m02-domain-contracts
```

Не вносите semantic parsing обратно в ветку M1: это отдельный scope M2–M6.

# 2. Карта skills: что и когда применять

| Skill | Когда использовать | Когда не использовать |
|---|---|---|
| `$structuraguard-plan` | Перед milestone, новым модулем, миграцией, изменением публичного API или крупным рефакторингом | Для опечатки или очевидной локальной правки |
| `$structuraguard-python` | DTO, protocols, facade, pipeline, services, configuration, исключения, чистая Python-логика | Самостоятельно недостаточен для parser/DB/LLM: сочетайте с профильным skill |
| `$structuraguard-parser` | Detection, техническое извлечение, Parser Registry, TXT/LOG/CSV/JSON/XML/HTML/XLSX/PDF/DOCX/YAML/Tika, batching, provenance, ParsePlan executor | Demo UI и чистая DB-логика |
| `$structuraguard-database` | Reflection, DatabaseCatalog, fingerprint, FK graph, MappingPlan validation, staging, loader, transaction, upsert | Разбор исходного файла и provider adapter |
| `$structuraguard-llm` | `LLMProvider`, LLM semantic parsing, `ParsePlan`, semantic DB mapping, router, structured output, retry/fallback, privacy policy | Чистое техническое чтение бинарного формата |
| `$structuraguard-tests` | После реализации, перед завершением milestone, для contract/property/integration/security tests | Не заменяет исправление кода |
| `$structuraguard-security` | Любая trust boundary: файл, XML/YAML/HTML, LLM, БД, staging, PII, logs, plugins, resource limits | Обычная правка текста без изменения поведения |
| `$structuraguard-review` | После реализации и тестов, перед PR/merge | Не применять вместо первоначального проектирования |
| `$structuraguard-docs` | Изменение public API, архитектуры, CLI, примеров, ADR и материалов диплома | Не документировать ещё не реализованное поведение как готовое |
| `$structuraguard-debug` | Только для воспроизводимого дефекта, падения теста, неверных данных, race condition или деградации | Не применять для новой функции |

Допустимо явно упоминать несколько skills в одном запросе:

```text
$structuraguard-parser $structuraguard-llm $structuraguard-python
```

Для semantic parsing используйте следующую связку:

```text
$structuraguard-plan
→ $structuraguard-parser + $structuraguard-llm + $structuraguard-python
→ $structuraguard-tests
→ $structuraguard-security
→ $structuraguard-docs
→ $structuraguard-review
```

Первым указывайте наиболее профильный skill, затем общий Python-skill.

# 3. Одноразовая подготовка проекта

## P00. Проверка конфигурации Codex

Отправить один раз после установки обновлённого Pro Pack. Код не должен изменяться.

```text
Проверь настройку Codex для этого репозитория. Ничего не изменяй.

Выполни:
- определи корень Git-репозитория и текущую ветку;
- перечисли активные AGENTS.md/AGENTS.override.md в порядке приоритета;
- проверь project skills в .agents/skills;
- выполни `python3 scripts/validate_codex_pack.py .`;
- выполни `python3 -m py_compile scripts/*.py`;
- проверь, что `docs/codex/PROJECT_CONTEXT.md`, `SPEC_INDEX.md`, `QUALITY_GATES.md`,
  `PROMPT_PIPELINE.md` и техническое ТЗ доступны;
- проверь, что в ТЗ присутствуют `ParsePlan`, `SemanticStructureAnalyzer`
  и раздел LLM-assisted semantic parsing;
- не читай полное ТЗ целиком.

В ответе укажи только:
1. рабочий корень;
2. активные инструкции;
3. найденные skills;
4. результаты команд;
5. блокеры, если они есть.
```

## P01. Актуализация файла состояния проекта

Отправить перед продолжением после M1.

```text
$structuraguard-docs

Создай или обнови компактный файл `docs/codex/PROJECT_STATE.md`
для передачи состояния между сессиями Codex.

Сначала проверь Git history, merged branches и фактический код.
Не отмечай milestone завершённым только со слов пользователя.

Структура файла:
- версия pipeline: 2.0;
- текущая версия SDK и текущий milestone;
- завершённые milestones M0–M17;
- активная ветка;
- реализованные публичные contracts;
- последние успешные quality gates;
- открытые блокеры;
- принятые архитектурные решения со ссылками на ADR;
- следующий рекомендуемый шаг.

Для текущего проекта:
- M1 отметить завершённым только при наличии подтверждения в main;
- следующий рекомендуемый шаг — M2;
- зафиксировать, что semantic parsing добавляется в M2–M6 и не требует переделки M1.

Ограничения:
- не более 220 строк;
- не дублировать ТЗ;
- не включать secrets, DSN или временные рассуждения.

Проверь Markdown и покажи изменённый файл.
```

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

Подготовь текущий milestone к ручному commit и последующему Pull Request в `main`, не создавая commit/PR самостоятельно.

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
5. рекомендуемый Conventional Commit message;
6. рекомендуемые title и краткое body для Pull Request.
```

## G7. Полный quality gate перед merge или релизом

```text
$structuraguard-tests $structuraguard-security $structuraguard-review

Выполни полный quality gate ветки перед merge Pull Request в `main` или перед release.

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
git switch main
git pull --ff-only
git switch -c docs/m00-requirements
codex -C . --profile quality
```

## Последовательность

```text
M0-P → M0-I → G2 → G3 → G4 → при необходимости G5 → G6
→ commit → push → PR в main → merge
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

# 6. M1 — каркас Python-пакета (уже завершён)

> Для текущего проекта этот milestone уже готов. Раздел оставлен для истории и проверки критериев. Повторно выполнять его не нужно.

## Ветка и профиль

```bash
git switch main
git pull --ff-only
git switch -c feat/m01-sdk-scaffold
codex -C . --profile quality
```

## Последовательность

```text
M1-P → M1-I → G1 → G3 → G4 → при необходимости G5 → G6
→ commit → push → PR в main → merge
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

# 7. M2 — доменные модели и contracts с поддержкой semantic parsing

## Ветка и профиль

```bash
git switch main
git pull --ff-only
git switch -c feat/m02-domain-contracts
codex -C . --profile quality
```

## Последовательность

```text
M2-P → M2-I → G1 → G3 → G4 → при необходимости G5 → G6
→ commit → push → PR в main → CI/review → merge
```

## M2-P. План

```text
$structuraguard-plan $structuraguard-python

Цель: спланировать milestone M2 «Доменные модели и contracts» с двухэтапной моделью parsing.

Сначала прочитай:
- AGENTS.md;
- docs/codex/PROJECT_CONTEXT.md;
- docs/codex/SPEC_INDEX.md;
- только разделы ТЗ для Contracts/DTO, Extracted Source Model, ParsePlan,
  SemanticStructureAnalyzer, Normalized Model, MappingPlan и reports.

Не читай полное ТЗ целиком.
Проверь фактический публичный API готового M1.
Сохрани план в `docs/plans/M02_domain_contracts.md`.

Архитектурная граница:

1. `Parser` технически извлекает содержимое и возвращает `ExtractedBatch`.
2. `SemanticStructureAnalyzer` анализирует физическую структуру и формирует `ParsePlan`.
3. `ParsePlanExecutor` применяет проверенный план и создаёт `NormalizedBatch`.
4. `DatabaseAdapter` анализирует БД и исполняет только проверенный `MappingPlan`.
5. `LLMProvider` является сменным адаптером для semantic parsing и semantic mapping.

План должен определить:
- границы `contracts` и `domain`;
- immutable/value-object semantics;
- физическую модель источника;
- семантическую нормализованную модель;
- discriminated union вариантов `ParsePlan`;
- различие `ParsePlan` и `MappingPlan`;
- provenance на всех переходах;
- стабильные enums/error codes/statuses;
- protocol signatures;
- отсутствие infrastructure imports;
- version/fingerprint fields;
- invalid-state, equality, serialization и substitutability tests.

Не реализовывай format parsers, LLM adapters, DB reflection или orchestrator.
```

## M2-I. Реализация

```text
$structuraguard-python $structuraguard-tests

Реализуй `docs/plans/M02_domain_contracts.md`.

Создай и экспортируй минимально необходимые contracts:

- `Parser`;
- `SemanticStructureAnalyzer`;
- `ParsePlanValidator`;
- `ParsePlanExecutor`;
- `DatabaseAdapter`;
- `LLMProvider`;
- `SecurityScanner`;
- `StagingStore`;
- `AuditStore`.

Создай доменные DTO:

- `SourceArtifact`;
- `ProbeResult`;
- `SourceLocation`;
- `ExtractedValue`;
- `ExtractedLine`;
- `ExtractedBlock`;
- `ExtractedCell`;
- `ExtractedTable`;
- `ExtractedTreeNode`;
- `ExtractedBatch`;
- `StructureProfile`;
- `StructureCandidate`;
- `ParsePlan` и его минимальные варианты:
  `TabularParsePlan`, `TreeParsePlan`, `LogParsePlan`, `DocumentParsePlan`;
- `ParseField`;
- `ParseRule`;
- `SemanticEntity`;
- `NormalizedValue`;
- `NormalizedRecord`;
- `NormalizedBatch`;
- `DatabaseCatalog` и дочерние catalog types;
- `MappingCandidate`;
- `MappingPlan`;
- `ValidationReport`;
- `LoadReport`;
- `SemanticParseReport`;
- `SecurityReport`;
- `AuditEvent`;
- pipeline statuses.

Обязательные инварианты:
- technical parser не присваивает окончательный бизнес-смысл полям;
- `Extracted*` сохраняет физическую структуру и raw values;
- `Normalized*` появляется только после применения `ParsePlan`;
- `ParsePlan` не содержит Python-код, shell, SQL или исполняемые callbacks;
- `MappingPlan` не содержит SQL;
- все source references проверяемы;
- `domain` и `contracts` не импортируют infrastructure;
- денежные значения — `Decimal`;
- datetime — timezone-aware UTC;
- mutable defaults запрещены;
- недопустимые состояния отклоняются при создании модели;
- `Any` не выходит за адаптерную границу без обоснования;
- public DTO сериализуются детерминированно.

Сначала добавь contract/serialization/invalid-state tests, затем реализацию.
Не расширяй M1 сверх минимально необходимых exports.
```

Рекомендуемый commit:

```text
feat: add semantic parsing domain contracts
```

После commit:

```bash
git push -u origin feat/m02-domain-contracts
# Создать PR feat/m02-domain-contracts → main.
```

# 8. M3 — Parser Registry и плагины

## Ветка и профиль

```bash
git switch main
git pull --ff-only
git switch -c feat/m03-parser-registry
codex -C . --profile quality
```

## Последовательность

```text
M3-P → M3-I → G1 → G2 → G3 → G4 → при необходимости G5 → G6
→ commit → push → PR в main → merge
```

## M3-P. План

```text
$structuraguard-plan $structuraguard-parser

Цель: спланировать M3 — Parser Registry и безопасное discovery parser plugins.

Прочитай через индекс только разделы 5, FR-001–FR-003, NFR-004, M3,
parser contract и текущие tests.
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
- security controls для недоверенного plugin metadata;
- контракт, по которому parser возвращает `ExtractedBatch`, а не готовые
  бизнес-сущности или `MappingPlan`.

Не реализовывай реальные format parsers и semantic analyzers.
```

## M3-I. Реализация

```text
$structuraguard-parser $structuraguard-python $structuraguard-tests

Реализуй `docs/plans/M03_parser_registry.md`.

Требования:
- registry является экземпляром и передаётся через dependency injection;
- регистрация и selection детерминированы;
- выбор учитывает `probe`, MIME, extension и priority;
- конфликтующие сигналы возвращают warning или typed ambiguity error;
- duplicate names/classes обрабатываются явно;
- entry points загружаются только по явному вызову;
- ошибка одного стороннего plugin не ломает discovery остальных и не скрывается;
- `FakeParser` используется в tests;
- общий parser contract подтверждает возврат физической `ExtractedBatch`;
- parser не вызывает LLM и не определяет таблицу БД;
- orchestrator пока не реализуется.

Добавь общий contract suite для будущих технических parsers.
```

Рекомендуемый commit:

```text
feat: add parser registry and plugin discovery
```

# 9. M4 — технические parsers форматов

M4 выполняет **техническое извлечение**, а не окончательное понимание данных.

```text
Файл/поток → безопасное чтение формата → ExtractedBatch + provenance
```

Определение неизвестных границ записей, смысла полей и сущностей выполняется в M5–M6.

M4 лучше выполнять несколькими небольшими вертикальными задачами в одной ветке. После каждой группы запускайте узкие tests. Полные G1/G2/G3/G4/G6 выполняются после завершения всей M4.

## Ветка и профиль

```bash
git switch main
git pull --ff-only
git switch -c feat/m04-technical-parsers
codex -C . --profile quality
```

## Последовательность

```text
M4-P → M4-A → M4-B → M4-C → M4-D → M4-E
→ при необходимости M4-F
→ G1 → G2 → G3 → G4 → G5 при необходимости → G6
→ commit → push → PR в main → merge
```

## M4-P. Общий план

```text
$structuraguard-plan $structuraguard-parser

Цель: спланировать milestone M4 как набор независимых technical parser adapters.

Извлеки только FR-004–FR-012, NFR-006, parser security sections и M4.
Проверь существующий Parser contract, Extracted Source Model и contract suite.
Сохрани план в `docs/plans/M04_technical_parsers.md`.

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
- physical extracted representation;
- streaming/batching;
- exact provenance;
- configurable limits;
- typed errors;
- malformed/malicious/boundary fixtures;
- contract/property/security tests.

Архитектурные ограничения:
- parser не формирует окончательный `ParsePlan`;
- parser не присваивает бизнес-сущности и целевые имена полей;
- parser не вызывает LLM;
- parser не анализирует БД;
- parser не выполняет destructive flatten;
- parser сохраняет raw values и физическую структуру.

Не объединяй все форматы в один универсальный parser.
```

## M4-A. TXT, LOG и MD

```text
$structuraguard-parser $structuraguard-python $structuraguard-tests

Реализуй группу A из `docs/plans/M04_technical_parsers.md`: TXT, LOG и MD.

Требования:
- encoding detection с явным confidence/warning;
- line/block provenance;
- ограничение размера, длины строки и числа строк до накопления в памяти;
- configurable batching;
- TXT/MD возвращают физические text blocks/lines;
- LOG возвращает строки и безопасные deterministic hints
  (например, распознанный timestamp/level), но не объявляет окончательную схему;
- неизвестный формат LOG не «угадывается» регулярным выражением;
- malformed encoding даёт typed error или контролируемую policy;
- cancellation учитывается между batches;
- embedded content не выполняется.

Добавь contract и boundary tests. Не трогай DB/LLM/semantic analyzer.
```

## M4-B. CSV и TSV

```text
$structuraguard-parser $structuraguard-tests

Реализуй группу B: CSV/TSV.

Требования:
- dialect detection: delimiter, quote, escape;
- header rows определяются только как кандидаты, а не окончательное решение;
- пользователь может переопределить detection options;
- streaming batches без полного чтения large source;
- `ExtractedTable`/rows/cells с provenance row/column;
- duplicate/empty cells и ragged rows обрабатываются детерминированно;
- max_records, max_columns, max_field_size и cancellation;
- malformed row возвращает typed error с номером строки;
- raw values сохраняются;
- business normalization и semantic naming не выполняются;
- property-based tests для Unicode, delimiters, quoting и batch boundaries.

Не добавляй Polars в обязательные dependencies без доказанной необходимости.
```

## M4-C. JSON, JSONL и NDJSON

```text
$structuraguard-parser $structuraguard-tests

Реализуй группу C: JSON, JSONL и NDJSON.

Требования:
- объект, массив, nested structures и повторяющиеся коллекции;
- `ExtractedTreeNode`/raw values;
- JSON Pointer provenance;
- JSONL/NDJSON читаются потоково по строкам;
- malformed line сообщает точный line number;
- лимиты nesting, records, key count и value size;
- batch boundary не создаёт дубли и пропуски;
- top-level scalar обрабатывается строго по contract;
- не выполнять destructive flatten и не назначать бизнес-сущности.

Добавь contract/property tests и regression test на границе batches.
```

## M4-D. XML, HTML и YAML

```text
$structuraguard-parser $structuraguard-security $structuraguard-tests

Реализуй группу D: безопасные XML, HTML и YAML technical parsers.

XML:
- external entities и DTD отключены;
- никаких network fetch;
- depth/node/text limits;
- namespace support;
- tree representation и XPath provenance;
- XXE/Billion Laughs regression tests.

HTML:
- JavaScript не выполняется;
- external resources не загружаются;
- физически извлекаются headings, text blocks, lists, tables и DOM relations;
- CSS selector provenance;
- script/style/iframe обрабатываются по deny-by-default policy;
- XSS payload остаётся данными и безопасно сериализуется.

YAML:
- только safe loader;
- запрет произвольных Python objects и alias bombs;
- depth/node limits;
- иерархия сохраняется;
- provenance в рамках возможностей библиотеки документирован.

Не определяй окончательные сущности и поля на этом этапе.
Dependencies добавляй через optional extras. Не добавляй browser engine.
```

## M4-E. XLSX, PDF и DOCX

```text
$structuraguard-parser $structuraguard-security $structuraguard-tests

Реализуй группу E: XLSX, PDF с текстовым слоем и DOCX.

XLSX:
- read-only mode, где возможно;
- sheets, tables, rows, cells и merged cells;
- sheet/cell provenance;
- candidate header metadata допустима, окончательное решение запрещено;
- formulas и macros не исполняются;
- stored formula values/metadata обрабатываются согласно contract;
- лимиты sheets/rows/columns/cells и ZIP container limits.

PDF:
- только text-layer PDF;
- pages/blocks/lines/tables в пределах возможностей адаптера;
- page/block/bounding-box provenance;
- timeout/page/text limits;
- сканированный PDF без text layer возвращает `PARSER_NO_TEXT_LAYER`;
- embedded actions/files не выполняются.

DOCX:
- paragraphs, headings, lists и tables в исходном порядке;
- block/table/cell provenance;
- macros/relationships/external resources не исполняются и не загружаются;
- лимиты распакованного контейнера.

Parser сохраняет физическую структуру, но не извлекает окончательные
`contract_number`, `customer`, `amount` и другие semantic fields.
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
- результат преобразуется в `ExtractedBatch`;
- typed unavailable/timeout/malformed errors;
- документировать, что network/container isolation обеспечивает вызывающий проект;
- default tests используют fake server, а не внешний Tika.

Не заменяй специализированные parsers Tika-адаптером.
```

Рекомендуемый commit:

```text
feat: add safe technical data parsers
```

# 10. M5 — Structural Profiler, ParsePlan и deterministic structure analyzer

M5 отвечает на вопрос:

> Как программно определить вероятную внутреннюю структуру уже технически прочитанного источника без вызова LLM?

## Ветка и профиль

```bash
git switch main
git pull --ff-only
git switch -c feat/m05-parse-plan
codex -C . --profile quality
```

## Последовательность

```text
M5-P → M5-A → M5-B → M5-C
→ G1 → G2 → G3 → G4 → G5 при необходимости → G6
→ commit → push → PR в main → merge
```

## M5-P. План

```text
$structuraguard-plan $structuraguard-parser $structuraguard-python

Цель: спланировать M5 — bounded structural profiling, deterministic
structure analysis, ParsePlan validation и execution.

Прочитай только:
- Extracted Source Model;
- FR для ParsePlan/Structure Analyzer;
- текущие technical parsers;
- security limits;
- соответствующие tests.

Сохрани план в `docs/plans/M05_parse_plan.md`.

Разбей план:
A. `StructuralProfiler`;
B. `DeterministicStructureAnalyzer`;
C. `ParsePlanValidator` и `ParsePlanExecutor`.

План должен покрыть:
- candidate header rows;
- data regions и footer/summary rows;
- record boundaries;
- repeated key-value groups;
- nested/repeated tree collections;
- log template clustering;
- document sections и block groups;
- candidate fields и semantic type hints;
- confidence/evidence breakdown;
- multiple structure candidates;
- bounded samples и streaming;
- declarative plans без кода/SQL;
- safe pattern/regex policy;
- provenance;
- deterministic behavior и property tests.

LLM в M5 не использовать.
```

## M5-A. Structural Profiler

```text
$structuraguard-parser $structuraguard-python $structuraguard-tests

Реализуй `StructuralProfiler`, который принимает `ExtractedBatch`
и создаёт bounded `StructureProfile`.

Поддержи минимум:

Tabular:
- candidate header rows;
- data start/end regions;
- repeated header detection;
- empty/meta/footer rows;
- stable column counts и raggedness;
- merged-cell context;
- candidate field names и primitive type hints.

Tree:
- repeated object/array paths;
- candidate record roots;
- key/value distributions;
- parent/child collection relationships.

Text/LOG:
- line/block boundaries;
- repeated line shapes;
- key-value patterns;
- timestamp/level hints;
- multiline record candidates;
- template clusters без создания исполняемого regex.

Document:
- headings/sections;
- nearby key-value blocks;
- table candidates;
- repeated block groups.

Требования:
- bounded memory и configurable sampling;
- не сохранять весь источник;
- каждое предположение содержит evidence и confidence;
- ambiguous candidates не сворачиваются в один «угаданный» результат;
- no LLM/network;
- tests для пустого, смешанного, большого и намеренно неоднозначного источника.
```

## M5-B. Deterministic Structure Analyzer

```text
$structuraguard-parser $structuraguard-python $structuraguard-tests

Реализуй `DeterministicStructureAnalyzer`.

Он получает `ExtractedSource/StructureProfile` и возвращает:
- один проверяемый `ParsePlan`, если confidence выше порога;
- ranked `StructureCandidate` list, если вариантов несколько;
- `NEEDS_SEMANTIC_ANALYSIS`, если правил недостаточно.

Минимальные варианты plan:
- `TabularParsePlan`;
- `TreeParsePlan`;
- `LogParsePlan`;
- `DocumentParsePlan`.

Требования:
- планы декларативны;
- plan содержит source fingerprint и версию;
- никакого Python-кода, SQL, shell и произвольных callbacks;
- безопасные операции выбираются из allowlisted enum;
- source paths/row ranges/block IDs существуют;
- невыводимая semantic meaning остаётся unresolved;
- deterministic tie-breaking;
- configurable confidence threshold;
- no hidden fallback;
- tests на нестандартный CSV с meta/header/footer, nested JSON,
  смешанный LOG и PDF/DOCX blocks.
```

## M5-C. ParsePlan Validator и Executor

```text
$structuraguard-parser $structuraguard-security $structuraguard-tests

Реализуй строгие `ParsePlanValidator` и `ParsePlanExecutor`.

Validator проверяет:
- соответствие plan его discriminated schema;
- source fingerprint/version;
- существование row/path/block/cell references;
- ranges, limits и отсутствие бесконечных/перекрывающихся правил;
- allowlist transformations/operators;
- отсутствие SQL, code, commands и unsafe regex;
- отсутствие неизвестных source identifiers;
- совместимость plan с типом `ExtractedSource`.

Executor:
- применяет только валидный plan;
- создаёт `NormalizedBatch` и `SemanticEntity`;
- сохраняет raw values и provenance;
- работает streaming/batch-aware;
- не вызывает LLM на каждой записи;
- корректно обрабатывает cancellation и partial source errors;
- выдаёт typed execution issues.

Добавь property/security tests, включая malicious plan payload.
```

Рекомендуемый commit:

```text
feat: add deterministic structure analysis and parse plans
```

# 11. M6 — LLM-assisted semantic parsing

M6 добавляет интеллектуальное понимание неизвестной структуры. LLM анализирует
не байты файла, а ограниченное технически извлечённое представление.

## Ветка и профиль

```bash
git switch main
git pull --ff-only
git switch -c feat/m06-llm-semantic-parsing
codex -C . --profile quality
```

## Последовательность

```text
M6-P → M6-A → M6-B → M6-C → M6-D
→ G1 → G2 → G3 → G4 → G5 при необходимости → G6
→ commit → push → PR в main → merge
```

## M6-P. План

```text
$structuraguard-plan $structuraguard-llm $structuraguard-parser

Цель: спланировать provider-neutral LLM-assisted semantic parsing M6.

Прочитай только разделы ТЗ:
- ParsePlan и SemanticStructureAnalyzer;
- LLM-агностичность;
- prompt injection, PII и resource limits;
- public parsing API;
- M6.

Изучи contracts M2, technical parsers M4 и deterministic analyzer M5.
Сохрани план в `docs/plans/M06_llm_semantic_parsing.md`.

Разбей план:
A. provider contract suite, `FakeLLMProvider` и `NoLLMProvider`;
B. `OpenAICompatibleProvider` и policy-aware router;
C. `LLMStructureAnalyzer` и strict ParsePlan generation;
D. `HybridStructureAnalyzer`, document entity extraction и semantic parse report.

Зафиксируй три режима:
- `deterministic`;
- `llm_assisted`;
- `llm_first`.

Определи:
- когда LLM вызывается;
- как выбираются bounded samples/chunks;
- как исключается вызов на каждую строку;
- structured response schemas;
- provenance/source references;
- context/token/call budgets;
- prompt versioning;
- prompt-injection boundaries;
- PII routing/redaction;
- deterministic fallback;
- `NEEDS_REVIEW` behavior.

Не реализовывай DB mapping в M6.
```

## M6-A. Fake и NoLLM providers

```text
$structuraguard-llm $structuraguard-python $structuraguard-tests

Реализуй provider-neutral foundation для semantic parsing.

Требования:
- общий `LLMProvider` contract suite;
- `FakeLLMProvider` поддерживает scripted success, malformed response,
  timeout, rate limit, unavailable и injected payload;
- `NoLLMProvider` реализует явный deterministic-only режим;
- provider capabilities типизированы;
- request/response metadata не содержит secrets;
- prompt version/fingerprint сохраняется;
- tests полностью deterministic с controlled clocks/IDs;
- provider-specific types не выходят из infrastructure adapter.
```

## M6-B. OpenAI-compatible provider и router

```text
$structuraguard-llm $structuraguard-security $structuraguard-tests

Реализуй `OpenAICompatibleProvider` и минимальный policy-aware router.

Provider:
- async HTTP client с явным lifecycle;
- structured output/JSON Schema при поддержке;
- timeout, cancellation, rate limit, unavailable, malformed response,
  context limit и capability mismatch как typed errors;
- API key/headers/credential-bearing URL не логируются;
- fake HTTP transport в default tests;
- никаких реальных внешних вызовов в CI.

Router:
- `fixed`, `no_llm`, `local_only`, `privacy_first`, `fallback`;
- fallback не понижает допустимый data classification;
- budget по calls/tokens/time;
- provider/model/latency/usage/fallback reason видимы в safe metadata;
- restricted data не отправляется cloud provider;
- при отсутствии разрешённого provider возвращается policy error.
```

## M6-C. LLMStructureAnalyzer и ParsePlan generation

```text
$structuraguard-llm $structuraguard-parser $structuraguard-security $structuraguard-tests

Реализуй `LLMStructureAnalyzer`.

Вход:
- bounded `ExtractedSource` sample;
- `StructureProfile`;
- deterministic candidates;
- parsing policy;
- разрешённая схема `ParsePlan`.

LLM должна уметь определить:
- header/data/footer regions;
- record boundaries;
- несколько record variants;
- candidate fields и semantic names;
- parent/child entity groups;
- tree record roots и paths;
- log event variants;
- document sections и extraction targets;
- locale/type hints;
- source block/path references.

Требования:
- source content явно маркируется как недоверенные данные;
- LLM не получает DB credentials, DB catalog, tools, SQL или filesystem access;
- модели передаются только bounded samples/chunks;
- ответ строго валидируется Pydantic/JSON Schema;
- identifiers/paths/block IDs проверяются по source;
- любые code/SQL/command fragments отклоняются;
- model self-confidence не является итоговым confidence;
- `ParsePlanValidator` обязателен после LLM;
- tests: valid, malformed, unknown source ref, oversized plan,
  prompt injection, timeout, rate limit и ambiguity.
```

## M6-D. HybridStructureAnalyzer и semantic document parsing

```text
$structuraguard-parser $structuraguard-llm $structuraguard-python $structuraguard-tests

Реализуй `HybridStructureAnalyzer` и полный semantic parsing flow.

Алгоритм:
1. Получить deterministic candidates из M5.
2. Если confidence достаточен — использовать plan без LLM.
3. Если структура неоднозначна — вызвать LLM на bounded sample.
4. Проверить LLM plan.
5. Применить plan детерминированно ко всему источнику.
6. Для prose/PDF/DOCX/HTML document extraction работать по bounded chunks.
7. Объединить entities, не теряя provenance.
8. Нераспознанные/конфликтующие записи поместить в review issues.

Требования:
- режимы `deterministic`, `llm_assisted`, `llm_first`;
- default `llm_assisted`;
- никакого per-row LLM для повторяющихся табличных данных;
- chunk deduplication и stable merge;
- каждое ненулевое semantic value имеет source references;
- no hallucinated source IDs;
- final confidence использует deterministic evidence, validation,
  agreement и penalties;
- `SemanticParseReport` содержит plan, provider metadata, unresolved blocks,
  issues, provenance coverage и usage;
- end-to-end tests для:
  - CSV с метаданными/header/footer;
  - смешанного LOG;
  - nested JSON/XML;
  - PDF/DOCX договора;
  - prompt injection внутри документа.
```

Рекомендуемый commit:

```text
feat: add llm-assisted semantic parsing
```

# 12. M7 — Database Inspector, каталог и граф БД

## Ветка и профиль

```bash
git switch main
git pull --ff-only
git switch -c feat/m07-database-inspector
codex -C . --profile quality
```

## Последовательность

```text
M7-P → M7-A → M7-B → M7-C → G1 → G2 → G3 → G4 → G5 при необходимости → G6
→ commit → push → PR в main → merge
```

## M7-P. План

```text
$structuraguard-plan $structuraguard-database

Цель: спланировать M7 — безопасный Database Inspector для SQLite и PostgreSQL, stable catalog fingerprint и FK graph.

Извлеки через индекс только разделы 9, 10, 13, M7 и DB security requirements.
Изучи DatabaseAdapter contract и catalog DTO.
Сохрани план в `docs/plans/M07_database_inspector.md`.

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

## M7-A. Каталог и SQLite

```text
$structuraguard-database $structuraguard-python

Реализуй часть A `docs/plans/M07_database_inspector.md`.

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

## M7-B. PostgreSQL reflection

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

## M7-C. Fingerprint и FK graph

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

# 13. M8 — Normalized Data Profiler

## Ветка и профиль

```bash
git switch main
git pull --ff-only
git switch -c feat/m08-normalized-data-profiler
codex -C . --profile quality
```

## Последовательность

```text
M8-P → M8-I → G1 → G2 → G3 → G4 → G5 при необходимости → G6
→ commit → push → PR в main → merge
```

## M8-P. План

```text
$structuraguard-plan $structuraguard-python

Цель: спланировать bounded Normalized Data Profiler M8.

Извлеки только FR-013, 11.4–11.6, 12 и M8.
Изучи NormalizedBatch/SemanticEntity model и semantic parsing batching contract.
Сохрани план в `docs/plans/M08_normalized_data_profiler.md`.

План должен определить:
- online/bounded statistics;
- sampling strategy;
- type inference и ambiguity;
- null/unique ratios;
- min/max/string lengths;
- patterns email/phone/UUID/URL/date/money/INN;
- identity hints;
- PII classification interfaces;
- stable normalized-data fingerprint;
- memory/performance limits;
- property-based tests.
```

## M8-I. Реализация

```text
$structuraguard-python $structuraguard-tests $structuraguard-security

Реализуй `docs/plans/M08_normalized_data_profiler.md`.

Требования:
- profiler принимает `NormalizedBatch` после semantic parsing и не зависит от исходного формата;
- samples строго ограничены и не сохраняют весь dataset;
- статистики обновляются инкрементально;
- типы не «угадываются» при конфликте: возвращается ranked/ambiguous result;
- raw PII examples не попадают в безопасные summaries/logs;
- Russian/English number/date formats различаются по locale policy;
- `Decimal` для денег;
- normalized-data fingerprint детерминирован и документирован;
- tests для пустых, смешанных, Unicode и очень больших логических потоков.
```

Рекомендуемый commit:

```text
feat: add bounded normalized data profiler
```

---

# 14. M9 — Deterministic Mapper

## Ветка и профиль

```bash
git switch main
git pull --ff-only
git switch -c feat/m09-deterministic-mapper
codex -C . --profile quality
```

## Последовательность

```text
M9-P → M9-I → G1 → G3 → G4 → G5 при необходимости → G6
→ commit → push → PR в main → merge
```

## M9-P. План

```text
$structuraguard-plan $structuraguard-python

Цель: спланировать deterministic mapping M9 без вызова LLM.

Извлеки через индекс только разделы 10–14 и M9.
Изучи NormalizedDataProfile, DatabaseCatalog, FK graph и MappingCandidate DTO.
Сохрани план в `docs/plans/M09_deterministic_mapper.md`.

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

## M9-I. Реализация

```text
$structuraguard-python $structuraguard-tests

Реализуй `docs/plans/M09_deterministic_mapper.md`.

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
feat: add deterministic database mapping candidates
```

---

# 15. M10 — LLM semantic mapping с нормализованных сущностей в БД

M10 использует уже готовый LLM provider/router из M6. Здесь LLM не определяет
структуру файла повторно, а помогает выбрать таблицы, столбцы и отношения БД.

## Ветка и профиль

```bash
git switch main
git pull --ff-only
git switch -c feat/m10-llm-database-mapping
codex -C . --profile quality
```

## Последовательность

```text
M10-P → M10-A → M10-B
→ G1 → G2 → G3 → G4 → G5 при необходимости → G6
→ commit → push → PR в main → merge
```

## M10-P. План

```text
$structuraguard-plan $structuraguard-llm $structuraguard-database

Цель: спланировать M10 — semantic mapping нормализованных сущностей
на таблицы/столбцы целевой БД через существующий provider-neutral LLM layer.

Прочитай только:
- Database Semantic Catalog;
- MappingCandidate и MappingPlan;
- LLM semantic mapping;
- confidence;
- prompt injection/PII rules;
- M10.

Изучи M6 provider/router, M7 DatabaseCatalog, M8 data profile
и M9 deterministic top-k candidates.
Сохрани план в `docs/plans/M10_llm_database_mapping.md`.

План должен определить:
- bounded prompt на одну группу связанных сущностей;
- top-k target pruning до вызова LLM;
- strict `SemanticMappingDecision` schema;
- table/column/relation candidates;
- provider/router reuse без нового provider abstraction;
- data classification и masking;
- confidence aggregation;
- ambiguity и `NEEDS_REVIEW`;
- запрет credentials/tools/SQL;
- provider contract и security tests.

Не реализовывай MappingPlan execution или DB writes.
```

## M10-A. Semantic mapping analyzer

```text
$structuraguard-llm $structuraguard-python $structuraguard-tests

Реализуй semantic database mapper поверх deterministic top-k candidates.

LLM получает только:
- semantic entity/field descriptors;
- bounded redacted examples;
- candidate table/column identifiers;
- safe comments/descriptions;
- DB type и FK relation summaries;
- candidate evidence.

Требования:
- полный DatabaseCatalog и raw БД-данные не отправляются без необходимости;
- ответ строго соответствует `SemanticMappingDecision`;
- модель может выбирать только из переданного candidate set;
- неизвестный identifier отклоняется;
- SQL/code/commands отклоняются;
- LLM score является одним signal;
- итоговый score рассчитывает SDK;
- ambiguous mappings остаются explicit;
- provider metadata и prompt fingerprint сохраняются;
- default tests используют FakeLLMProvider.

Покрой:
- однозначный выбор;
- одинаковые column names в разных tables;
- entity split across related tables;
- composite relation candidate;
- неизвестный target;
- malformed structured response.
```

## M10-B. Privacy, prompt injection и confidence aggregation

```text
$structuraguard-llm $structuraguard-security $structuraguard-tests

Заверши M10 policy и confidence layer.

Требования:
- source values считаются недоверенными данными;
- prompt injection из field name/value не меняет инструкции;
- confidential/restricted data соблюдает routing policy M6;
- redacted placeholders восстанавливаются только после безопасного ответа,
  если это необходимо;
- fallback не понижает classification;
- итоговый confidence включает:
  deterministic name/alias/type/pattern/graph signals,
  LLM decision, ambiguity, validation и security penalties;
- model self-confidence не принимается напрямую;
- threshold policy: auto/confirm/reject;
- raw response хранится только по retention/redaction policy;
- security tests для prompt injection, PII leakage,
  candidate escape и cloud fallback violation.
```

Рекомендуемый commit:

```text
feat: add llm-assisted database semantic mapping
```

# 16. M11 — MappingPlan Validator

## Ветка и профиль

```bash
git switch main
git pull --ff-only
git switch -c feat/m11-mapping-plan-validator
codex -C . --profile quality
```

## Последовательность

```text
M11-P → M11-I → G1 → G2 → G3 → G4 → G5 при необходимости → G6
→ commit → push → PR в main → merge
```

## M11-P. План

```text
$structuraguard-plan $structuraguard-database

Цель: спланировать строгую валидацию декларативного MappingPlan M11.

Извлеки только 14.1, 16, 18, 20.3–20.4 и M11.
Изучи MappingPlan DTO, DatabaseCatalog, fingerprints и policy types.
Сохрани план в `docs/plans/M11_mapping_plan_validator.md`.

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

## M11-I. Реализация

```text
$structuraguard-database $structuraguard-python $structuraguard-security $structuraguard-tests

Реализуй `docs/plans/M11_mapping_plan_validator.md`.

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

# 17. M12 — Validation Engine и нормализация

## Ветка и профиль

```bash
git switch main
git pull --ff-only
git switch -c feat/m12-validation-engine
codex -C . --profile quality
```

## Последовательность

```text
M12-P → M12-A → M12-B → M12-C → M12-D → G1 → G2 → G3 → G4 → G5 при необходимости → G6
→ commit → push → PR в main → merge
```

## M12-P. План

```text
$structuraguard-plan $structuraguard-python

Цель: спланировать M12 — conservative normalization и многоуровневый Validation Engine.

Извлеки только разделы нормализации/валидации, ParsePlan/MappingPlan provenance и M12.
Изучи DTO validation issues, MappingPlan и provenance.
Сохрани план в `docs/plans/M12_validation_engine.md`.

Разбей:
A. normalizer registry и locale-aware built-ins;
B. JSON Schema Draft 2020-12;
C. DB constraints и safe business-rule DSL;
D. provenance validation и all-errors report.

Определи порядок уровней, immutability/raw value preservation, error codes и repair boundaries. Никакого `eval`.
```

## M12-A. Normalizers

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

## M12-B. JSON Schema

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

## M12-C. DB constraints и business rules

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

## M12-D. Provenance validation и отчёт

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

# 18. M13 — Staging и transactional Loader

## Ветка и профиль

```bash
git switch main
git pull --ff-only
git switch -c feat/m13-staging-loader
codex -C . --profile quality
```

## Последовательность

```text
M13-P → M13-A → M13-B → M13-C → M13-D → G1 → G2 → G3 → G4 → G5 при необходимости → G6
→ commit → push → PR в main → merge
```

## M13-P. План

```text
$structuraguard-plan $structuraguard-database

Цель: спланировать безопасные staging и loader M13 для PostgreSQL.

Извлеки только разделы 17–18, 20.3–20.4 и M13.
Изучи MappingPlan validator, DatabaseCatalog graph и ValidationReport.
Сохрани план в `docs/plans/M13_staging_loader.md`.

Разбей:
A. StagingStore contract/implementation и run lifecycle;
B. dry-run и execution plan;
C. insert_only/upsert/FK resolution/load order;
D. rollback/quarantine/idempotency/schema recheck.

Зафиксируй separate writer user, parameterized values, validated identifiers, transaction boundaries, cancellation semantics и integration test matrix.
```

## M13-A. Staging

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

## M13-B. Dry run

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

## M13-C. Insert, upsert и FK resolution

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

## M13-D. Atomicity, quarantine и idempotency

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

# 19. M14 — Security layer

## Ветка и профиль

```bash
git switch main
git pull --ff-only
git switch -c feat/m14-security-layer
codex -C . --profile quality
```

## Последовательность

```text
M14-P → M14-A → M14-B → M14-C → M14-D → G1 → G2 → G3 → G4 → G5 при необходимости → G6
→ commit → push → PR в main → merge
```

## M14-P. План

```text
$structuraguard-plan $structuraguard-security

Цель: спланировать M14 — единый Security Policy layer и audit chain, не дублируя controls уже встроенные в parsers/DB/LLM.

Извлеки только раздел 20, 30.4, 32.5 и M14.
Прочитай `docs/threat-model.md` и текущие security tests.
Сохрани план в `docs/plans/M14_security_layer.md`.

Разбей:
A. resource/file/source policy;
B. PII/secrets classification и redaction;
C. prompt-injection signals и LLM routing restrictions;
D. database policy, audit events/HMAC chain и sandbox runner interface.

Для каждого control укажи trust boundary, deny-by-default behavior, regression tests и residual risk.
```

## M14-A. Resource и source policy

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

## M14-B. PII, secrets и redaction

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

## M14-C. Prompt injection и LLM routing

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

## M14-D. DB policy, ParsePlan/MappingPlan policy, audit chain и sandbox runner

```text
$structuraguard-security $structuraguard-database $structuraguard-parser $structuraguard-tests

Реализуй оставшиеся controls M14.

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

# 20. M15 — интеграционный pipeline и публичный SDK facade

После готовности M2–M14 компоненты соединяются в единый end-to-end pipeline.

## Ветка и профиль

```bash
git switch main
git pull --ff-only
git switch -c feat/m15-sdk-orchestrator
codex -C . --profile quality
```

## Последовательность

```text
M15-P → M15-I
→ G1 → G2 → G3 → G4 → G5 при необходимости → G6
→ commit → push → PR в main → merge
```

## M15-P. План

```text
$structuraguard-plan $structuraguard-python

Цель: спланировать end-to-end SDK orchestrator от source до report,
используя только уже реализованные contracts/adapters.

Прочитай только разделы режимов работы, полного pipeline, public API,
statuses, reports и критерии приёмки.
Изучи текущие facades и component APIs.
Сохрани план в `docs/plans/M15_sdk_orchestrator.md`.

План должен покрыть методы:

- `inspect_source`;
- `analyze_structure`;
- `create_parse_plan`;
- `validate_parse_plan`;
- `parse_semantically`;
- `inspect_database`;
- `profile_records`;
- `create_mapping_plan`;
- `validate_mapping_plan`;
- `execute`;
- `ingest`;
- sync wrappers.

Полный stage flow:

```text
source security gate
→ detection
→ technical parser
→ structural profile
→ deterministic/LLM semantic analysis
→ ParsePlan validation
→ ParsePlan execution
→ normalized data profile
→ database inspection
→ deterministic/LLM DB mapping
→ MappingPlan validation
→ normalization/record validation
→ staging
→ dry-run/load
→ report/audit
```

Зафиксируй:
- state transitions;
- event hooks;
- cancellation;
- timeout/retry boundaries;
- parsing policy и LLM policy;
- no hidden fallback;
- dry-run;
- security gates;
- composition root;
- повторное использование ParsePlan/MappingPlan по fingerprint;
- failure/review states.
```

## M15-I. Реализация

```text
$structuraguard-python $structuraguard-parser $structuraguard-database
$structuraguard-llm $structuraguard-security $structuraguard-tests

Реализуй `docs/plans/M15_sdk_orchestrator.md`.

Требования:
- pipeline следует утверждённым states;
- зависимости внедряются через constructor/configuration;
- global registry отсутствует;
- каждый stage принимает/возвращает typed DTO;
- technical parsing и semantic parsing являются разными stages;
- `ParsePlan` валидируется до применения;
- `MappingPlan` валидируется до DB operations;
- ошибки нормализуются, но первопричина не скрывается;
- cancellation и timeout проходят через stages;
- режимы `deterministic`, `llm_assisted`, `llm_first`, `no_llm` работают;
- dry_run не изменяет target DB;
- security policy выполняется до внешнего LLM и перед load;
- повторяющиеся табличные данные не вызывают LLM на каждую строку;
- `IngestResult` содержит source/parse/DB/mapping fingerprints,
  parse plan, mapping plan, provider metadata, validation/load/security reports
  и audit references;
- sync facade корректно обрабатывает уже запущенный event loop;
- end-to-end tests:
  fake parser/LLM/DB;
  PostgreSQL happy/error paths;
  unknown structure;
  prompt injection;
  ambiguous parse;
  schema drift;
  rollback.
```

Рекомендуемый commit:

```text
feat: connect full semantic ingestion pipeline
```

# 21. M16 — демонстрационный проект

## Ветка и профиль

```bash
git switch main
git pull --ff-only
git switch -c feat/m16-demo-application
codex -C . --profile quality
```

## Последовательность

```text
M16-P → M16-A → M16-B → M16-C → M16-D → G1 → G2 → G3 → G4 → G5 при необходимости → G6
→ commit → push → PR в main → merge
```

## M16-P. План

```text
$structuraguard-plan

Цель: спланировать отдельный demo project M16, доказывающий встраиваемость SDK и не загрязняющий core framework dependencies.

Извлеки только разделы 2, 23, 28 и M16.
Изучи public SDK API, examples и repository layout.
Сохрани план в `docs/plans/M16_demo_application.md`.

Разбей:
A. FastAPI API;
B. worker/job execution abstraction;
C. minimal UI;
D. Docker Compose/PostgreSQL/demo data.

Определи API contracts, auth/demo boundaries, file limits, secret handling, progress events и e2e scenario. Core package не импортирует FastAPI/Celery/UI.
```

## M16-A. FastAPI demo API

```text
$structuraguard-python $structuraguard-security $structuraguard-tests

Создай отдельное приложение `apps/demo-api`, которое использует только public StructuraGuard SDK.

Маршруты минимум:
- `POST /documents/analyze`;
- `POST /documents/plan`;
- `POST /documents/import`;
- `GET /runs/{run_id}`;
- `GET /runs/{run_id}/parse`;
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

## M16-B. Worker

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

## M16-C. Минимальный UI

```text
$structuraguard-docs $structuraguard-security

Создай минимальный demo UI, достаточный для защиты диплома.

Пользователь должен уметь:
- загрузить поддерживаемый файл;
- выбрать target DB connection из заранее настроенного списка, не вводя DSN в браузере;
- увидеть detected format/source profile;
- увидеть ParsePlan, normalized entities, mapping candidates/MappingPlan и confidence;
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

## M16-D. Docker Compose и demo scenario

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
feat: add semantic parsing demo application
```

---

# 22. M17 — Evaluation и экспериментальная часть

## Ветка и профиль

```bash
git switch main
git pull --ff-only
git switch -c feat/m17-evaluation
codex -C . --profile quality
```

## Последовательность

```text
M17-P → M17-A → M17-B → M17-C → G1 → G3 → G4 → G5 при необходимости → G6
→ commit → push → PR в main → merge
```

## M17-P. План

```text
$structuraguard-plan $structuraguard-tests

Цель: спланировать воспроизводимую evaluation framework M17 для сравнения deterministic-only, LLM-heavy и hybrid semantic pipeline.

Извлеки только разделы 30–33 и M17.
Изучи текущие adapters, reports и sample data.
Сохрани план в `docs/plans/M17_evaluation.md`.

План должен определить:
- dataset manifest и лицензирование/синтетическое происхождение;
- target DB schemas;
- gold ParsePlan, semantic entities, MappingPlan и expected values;
- experiment configurations;
- fixed seeds/provider snapshots;
- метрики semantic parsing/mapping/load/LLM/performance/security;
- separation train/calibration/test, если веса настраиваются;
- output CSV/JSON/Markdown;
- способ не коммитить sensitive data и огромные generated artifacts.
```

## M17-A. Dataset и fixtures

```text
$structuraguard-tests $structuraguard-security $structuraguard-docs

Создай структуру `evals/` и минимальный репрезентативный dataset manifest.

Требования:
- форматы CSV/TSV, JSON/JSONL, XML, XLSX, HTML, text-layer PDF, DOCX, LOG/TXT;
- несколько target DB domains;
- gold parse plans, semantic entities, mapping plans и expected normalized values;
- benign, malformed, ambiguous и security attack variants;
- синтетические/обезличенные данные;
- source/license metadata;
- deterministic generators для расширения набора;
- никаких реальных персональных данных или secrets.

Не генерируй сразу сотни тяжёлых бинарных файлов, если достаточно manifest + generator + минимальных fixtures.
```

## M17-B. Evaluation runner и метрики

```text
$structuraguard-python $structuraguard-tests

Реализуй evaluation runner.

Конфигурации минимум:
- deterministic structure parsing + deterministic DB mapping;
- LLM-first semantic parsing + LLM DB mapping с Fake/recorded provider для CI;
- hybrid: deterministic extraction/profile + selective LLM + validation.

Метрики:
- header/record-boundary/field/entity parsing accuracy;
- semantic field precision/recall/F1 и provenance coverage;
- table/column/entity/relation mapping accuracy;
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

## M17-C. Отчёт эксперимента

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
feat: add semantic parsing evaluation framework
```

---

# 23. Финальная приёмка SDK

## Ветка и профиль

```bash
git switch main
git pull --ff-only
git switch -c chore/final-acceptance
codex -C . --profile quality
```

## Последовательность

```text
FINAL-A → G7 → FINAL-B → G4 → при необходимости G5 → G6
→ commit → push → PR в main → CI/review → merge
```

## FINAL-A. Матрица критериев приёмки

```text
$structuraguard-review $structuraguard-tests $structuraguard-security

Проведи полную приёмку StructuraGuard по актуальному разделу критериев
приёмки технического ТЗ версии 2.0.

Создай `docs/acceptance/FINAL_ACCEPTANCE.md` с таблицей для каждого критерия:
- идентификатор;
- статус PASS/PARTIAL/FAIL;
- реализация `path:line`;
- подтверждающий test;
- команда проверки;
- residual risk или gap.

Не отмечай PASS без проверяемого доказательства.

Отдельно проверь:
- технические parsers возвращают физическое представление;
- `ParsePlan` и `MappingPlan` разделены;
- неизвестная структура обрабатывается через LLM-assisted semantic parsing;
- ParsePlan валидируется до применения;
- повторяющиеся tabular records не требуют LLM-вызова на каждую строку;
- provenance сохраняется через все стадии;
- DB credentials не попадают в LLM;
- LLM не формирует исполняемый SQL;
- no-LLM/deterministic режим работает;
- dry-run, staging, rollback и idempotency;
- prompt-injection/PII routing;
- public API и отсутствие import-time side effects;
- воспроизводимый evaluation report.

Код пока не меняй. Сначала сформируй честный gap report.
```

## FINAL-B. Закрытие gaps

```text
$structuraguard-plan $structuraguard-python $structuraguard-tests

На основании `docs/acceptance/FINAL_ACCEPTANCE.md` сформируй минимальный
план закрытия только критериев со статусом PARTIAL/FAIL.

Сохрани его в `docs/plans/FINAL_gap_closure.md`.
Сгруппируй gaps по severity и зависимости.
Не добавляй новую функциональность вне актуального ТЗ.

После утверждения реализуй gaps небольшими commits-ready изменениями:
- сначала regression/acceptance test;
- затем минимальная реализация;
- затем узкие и полные quality gates;
- security review для trust boundaries;
- обновление acceptance matrix по фактическим доказательствам.

Не создавай commit и PR автоматически.
```

# 24. Специальные пайплайны после основной разработки

## 24.1. Исправление воспроизводимого бага

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

## 24.2. Новый parser plugin

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
- physical `ExtractedBatch` output;
- exact provenance;
- streaming, если формат потенциально большой;
- typed parse/unsupported/limit/security errors;
- registration через registry/entry point;
- contract, malformed, boundary, cancellation и malicious tests;
- optional extra, если нужна тяжёлая dependency.

Не менять ParsePlan/MappingPlan, DB loader и core pipeline без отдельного требования.
После реализации: `$structuraguard-tests`, `$structuraguard-security`, `$structuraguard-review`.
```

---

## 24.3. Новый semantic parsing strategy

```text
$structuraguard-plan $structuraguard-parser $structuraguard-llm

Цель: добавить semantic parsing strategy `<STRATEGY>` для источников `<SOURCE_TYPES>`
без изменения technical parser contract и DB mapping contract.

Сначала зафиксируй:
- наблюдаемую неизвестную структуру;
- physical `ExtractedSource` input;
- ожидаемый `ParsePlan`/`NormalizedBatch`;
- deterministic signals;
- необходимость LLM;
- bounded sample/chunk policy;
- provenance;
- confidence и review thresholds;
- security risks.

Реализуй strategy через `SemanticStructureAnalyzer` и существующий
`ParsePlanValidator/Executor`.

Требования:
- technical parser не изменяется без реальной форматной причины;
- LLM не вызывается на каждую повторяющуюся запись;
- response strictly typed;
- source refs проверяются;
- no code/SQL/tools;
- deterministic/no-LLM behavior остаётся работоспособным;
- contract/property/security tests.

После реализации: `$structuraguard-tests`, `$structuraguard-security`,
`$structuraguard-docs`, `$structuraguard-review`.
```

---

## 24.4. Новый database dialect

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

## 24.5. Новый LLM provider

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

## 24.6. Изменение публичного API

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

## 24.7. Добавление production dependency

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

## 24.8. Оптимизация производительности

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

## 24.9. Рефакторинг без изменения поведения

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

## 24.10. Обновление документации без кода

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

## 24.11. Проверка чужого или большого diff

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

# 25. Исправление незавершённой задачи

## 25.1. Codex не выполнил tests

```text
$structuraguard-tests

Ты завершил реализацию, но не подтвердил её обязательными проверками.
Не добавляй новую функциональность.

Определи доступные quality gates из Makefile/pyproject/AGENTS.md и последовательно выполни минимально необходимые проверки текущего diff.
Если окружение блокирует команду, покажи точную команду, полный существенный фрагмент ошибки и минимальный способ разблокировки.
Не называй задачу готовой без фактического результата.
```

## 25.2. Codex вышел за scope

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

## 25.3. Codex хочет ослабить test/lint/typecheck

```text
$structuraguard-debug $structuraguard-tests

Не ослабляй и не отключай failing test/lint/typecheck.
Найди первопричину падения.
Запрещено использовать skip, xfail, noqa, broad `type: ignore`, исключение файла из checks или понижение strictness без отдельного доказанного требования.
Исправь код или узкую ошибочную конфигурацию, затем повтори проверку.
```

## 25.4. Остались Critical/High findings

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

## 25.5. Контекст сессии стал слишком большим

Перед завершением старой сессии:

```text
$structuraguard-docs

Обнови `docs/codex/PROJECT_STATE.md` и plan-файл текущего milestone и актуальную версию pipeline перед переносом работы в новую сессию.
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
Сначала прочитай `AGENTS.md`, `docs/codex/PROJECT_STATE.md` и plan-файл текущего milestone и актуальную версию pipeline, затем только непосредственно затронутые файлы/tests.
Не перечитывай полное ТЗ.
Проверь `git status` и продолжи с указанного в PROJECT_STATE следующего шага.
```

---

# 26. Команды Git и Pull Request между milestones

Codex не должен автоматически выполнять merge. Commit и Pull Request создаются
после вашего просмотра diff.

## 26.1. Начало milestone

Всегда начинайте новую ветку от актуального `main`:

```bash
git status --short
git switch main
git pull --ff-only
git switch -c <branch-name>
```

Если `git status` показывает незавершённые изменения, не переключайтесь на новый
milestone, пока не разберётесь с ними.

## 26.2. Проверка после G6

```bash
git status --short
git diff --check
git diff --stat
git diff
```

После ручной проверки:

```bash
git add <конкретные-файлы-и-каталоги>
git diff --cached --check
git diff --cached --stat
git diff --cached
git commit -m "<recommended Conventional Commit message>"
git push -u origin <branch-name>
```

## 26.3. Финальный запрос Codex перед PR

```text
$structuraguard-review $structuraguard-tests

Проведи финальную проверку ветки перед Pull Request в `main`.

Проверь весь diff:
`git diff main...HEAD`

Особенно проверь:
- соответствие milestone и plan-файлу;
- отсутствие изменений вне scope;
- архитектурные границы;
- public API/type safety/error paths;
- security;
- достаточность tests;
- отсутствие secrets/debug/generated artifacts;
- `git diff --check`;
- актуальные quality gates.

Код не меняй.

В ответе:
1. blocking findings;
2. non-blocking findings;
3. выполненные проверки;
4. PR READY: YES/NO.
```

## 26.4. Создание PR

Через GitHub CLI:

```bash
gh pr create \
  --base main \
  --head <branch-name> \
  --title "<Milestone title>" \
  --body-file <pr-body-file>
```

Либо через GitLab CLI:

```bash
glab mr create \
  --source-branch <branch-name> \
  --target-branch main \
  --title "<Milestone title>" \
  --description-file <mr-body-file>
```

В PR/MR должны быть:

- цель milestone;
- scope и out-of-scope;
- основные архитектурные решения;
- выполненные проверки;
- security impact;
- residual risks;
- ссылка на plan-файл.

После успешных CI и review выполните merge через интерфейс GitHub/GitLab.

## 26.5. После merge

```bash
git switch main
git pull --ff-only
git branch -d <branch-name>
```

Если remote-ветка не удалена автоматически:

```bash
git push origin --delete <branch-name>
```

Только после этого создавайте ветку следующего milestone.

# 27. Краткая шпаргалка последовательностей

## 27.1. Общий цикл milestone

```text
обновить main
→ создать feature branch
→ новая сессия Codex
→ $structuraguard-plan
→ профильный implementation skill
→ $structuraguard-tests
→ $structuraguard-security при trust boundary
→ $structuraguard-docs
→ $structuraguard-review
→ исправление findings
→ G6
→ ручной diff
→ commit
→ push
→ PR в main
→ CI/review
→ merge
→ обновить main
```

## 27.2. Technical parser

```text
$structuraguard-plan
→ $structuraguard-parser + $structuraguard-python
→ ExtractedBatch/provenance tests
→ $structuraguard-security
→ $structuraguard-review
```

## 27.3. Semantic parsing

```text
$structuraguard-plan
→ $structuraguard-parser + $structuraguard-llm + $structuraguard-python
→ ParsePlan/structured-output tests
→ prompt-injection/PII security tests
→ $structuraguard-docs
→ $structuraguard-review
```

## 27.4. Database

```text
$structuraguard-plan
→ $structuraguard-database + $structuraguard-python
→ PostgreSQL integration tests
→ $structuraguard-security
→ $structuraguard-review
```

## 27.5. LLM DB mapping

```text
$structuraguard-plan
→ $structuraguard-llm + $structuraguard-database
→ structured-output/candidate-boundary tests
→ prompt-injection/privacy security tests
→ $structuraguard-review
```

## 27.6. Баг

```text
$structuraguard-debug
→ failing regression test
→ minimal fix
→ $structuraguard-tests
→ $structuraguard-security при необходимости
→ $structuraguard-review
```

## 27.7. Финальный релиз

```text
Acceptance matrix
→ gap closure
→ full quality gate
→ security review
→ wheel install smoke test
→ docs/evaluation verification
→ final PR review
```

## 27.8. Актуальный порядок разработки после готового M1

```text
M2  contracts и двухэтапная source model
M3  Parser Registry
M4  technical parsers
M5  Structural Profiler + deterministic ParsePlan
M6  LLM-assisted semantic parsing
M7  Database Inspector
M8  Normalized Data Profiler
M9  deterministic DB mapper
M10 LLM semantic DB mapper
M11 MappingPlan Validator
M12 normalization/validation
M13 staging/loader
M14 security layer
M15 SDK orchestrator
M16 demo application
M17 evaluation
```
