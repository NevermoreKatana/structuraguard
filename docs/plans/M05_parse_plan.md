# M05 — Structural profiling и deterministic ParsePlan

Статус: A/B/C готовы к ручному commit и Pull Request в `main` в пределах
документированной закрытой policy. Полная приёмка исходного C3 не подтверждена;
ограничения и незакрытые пункты явно перечислены в checklist ниже.
Дата: 2026-09-10.

Матрица критериев, фактические тесты и ограничения приёмки:
[отчёт M05](M05_acceptance.md). Зелёные quality gates не расширяют поддерживаемую policy.
Исправленные security findings и проверки: [security review](M05_security_review.md).

Фактические contracts описаны в [ADR 0008](../adr/0008-bounded-structural-profiling.md),
[ADR 0009](../adr/0009-deterministic-structure-analysis.md) и
[публичном API](../public-api.md#deterministicstructureanalyzer-m5-b).
Пустой input даёт пустой profile; analyzer возвращает `NEEDS_SEMANTIC_ANALYSIS`.
B реализован в `structure/analysis.py`, `structure/planning.py`, `contracts/analysis.py`
с расширением существующих ParsePlan DTO. Вместо proposed weighted score/margin
используется версия policy с минимумом компонентов и без автоматического выбора
между конкурентами. План выдаётся только по проверенному physical replay с полным
sampling coverage. LOG/document scopes конечны; field semantics unresolved,
conversions и обобщение plan на непроверенный stream отложены до отдельной policy.
C реализован в `structure/validation.py`, `structure/execution.py`, общем
`structure/_runtime.py` и `structure/_plan_check.py`; DTO budgets/issues/origins —
`contracts/execution.py`. Фактическая policy и совместимость закреплены в
[ADR 0010](../adr/0010-verified-parse-plan-execution.md). Acceptance validator
требует полного replay: `validate(..., batches=...)` либо async `validate_source`.
Executor повторяет проверки, сохраняет origins и выдаёт success manifest после EOF.
Sync port без source возвращает REPLAY_REQUIRED; identity/conversion/отдельные
ParseRule и неоднозначные legacy starts остаются явно неподдержанными.
Разделы «Цель» — «Риски и границы» сохраняют исходный design M5; предложенные
файлы и расширения за пределами фактической policy не считаются реализованными.

## Checklist перед ручным commit и PR

Проверка: 2026-09-10, macOS, Python 3.12.9. Ветка `feat/m05-parse-plan`;
локальные `HEAD` и `main` указывают на `dfed5ce`. Commit, staging, push и PR
автоматически не выполнялись. Это локальная готовность, без утверждения об
успешном remote CI. Канонические требования: [FR-014][spec-fr-014] и [M5][spec-m5].

- [x] **A / K1:** bounded versioned `StructureProfile`, пустой source,
  coverage, evidence и confidence подтверждены тестами.
- [x] **A / K3:** наблюдения для tabular/tree/text/document, header/data/footer,
  repeated groups, candidate fields и primitive alternatives проверены.
- [x] **B / K1–K2:** четыре plan variants, configurable threshold,
  deterministic ranking, `needs_review` и `NEEDS_SEMANTIC_ANALYSIS` без fallback.
  Независимые document blocks не теряются при наличии physical lines.
- [x] **C / K1, K4:** строгая schema/hash/reference validation и полный replay;
  execution сохраняет selected raw values, origins и parent/child relations.
  Промежуточные batches не равны успешному terminal manifest.
- [x] **K5:** bounded samples/state, streaming, N/N+1, cancellation, timeout,
  partial errors и cleanup. Sparse row index не накапливает пустые строки;
  fan-out ограничивается до materialization полного record.
- [x] **K6:** закрытые enums/selectors, инертные SQL/code literals, запрет
  executable regex/callbacks, отсутствие LLM/network/DB authority.
- [x] **K7:** property tests на determinism/hash seeds, batch boundaries,
  multiplicity, raw values и provenance. Числовой hint всегда Decimal-valid.
- [x] Все **59 ссылок** `файл::test_function` в [матрице приёмки](M05_acceptance.md)
  разрешаются в реальные tests; K1–K7 сопоставлены с наблюдаемым поведением.
- [x] Исправлены три findings security review и три findings финального review;
  regressions и повторный review отражены в [PROJECT_STATE](../codex/PROJECT_STATE.md).
- [x] Устранён packaging blocker подготовки: строгий manifest включает 18 новых
  модулей M5, isolated smoke проверяет новый port и семь exports `structure`.
  Пять новых packaging cases включают отказ для посторонних файлов; allowlist
  остаётся явным, проверки не ослаблены.
- [x] Русские public docstrings, offline example, ADR 0008–0010, ограничения и
  `PROJECT_STATE.md` актуальны. Нового архитектурного решения в подготовке нет.
- [x] Lint, typecheck, unit/property/security/integration, docs и distribution
  gates прошли; фактические команды перечислены ниже.
- [x] `git diff --check`, проверка состава diff, secrets/debug/generated audit
  выполнены. Новых production dependencies и изменений lockfile нет.
- [ ] Полный automatic A→B→C исходного C3 для XML/HTML/Markdown/нескольких
  JSONL roots: текущая policy поддерживает только описанные варианты и явные
  отказы; caller-authored XML/HTML/Markdown планы проверены отдельно.
- [ ] Identity, semantic conversions, optional-missing policy, отдельные
  `ParseRule`, include-descendants и ambiguous legacy starts: deferred по
  ADR 0010; negative tests не считаются реализацией этих функций.
- [ ] Remote CI, дополнительные окружения и нагрузочные измерения:
  причины и границы перечислены в разделе пропущенных проверок ниже.

### Фактически выполненные команды

Команды запускались из корня workspace. Для обычных gates использован
`UV_CACHE_DIR=/private/tmp/structuraguard-m05-uv-cache`; для полного offline
distribution verification — `UV_CACHE_DIR=/Users/katana/.cache/uv` с уже
доступными locked build/runtime dependencies. Сеть в verification запрещена.

```bash
uv run --locked --no-sync pytest -q packages/structuraguard/tests/packaging
uv run --locked --no-sync pytest -q packages/structuraguard/tests/docs
uv run --locked --no-sync pytest -q \
  packages/structuraguard/tests/unit/structure \
  packages/structuraguard/tests/property/structure \
  packages/structuraguard/tests/security/structure \
  packages/structuraguard/tests/integration/test_structure_documents.py
make lock-check
make lint typecheck
make test test-integration test-security
make test-build
make docs
git diff --check
git status --short
git diff --numstat
git ls-files --others --exclude-standard
git rev-list --left-right --count main...HEAD
git diff --exit-code -- pyproject.toml packages/structuraguard/pyproject.toml uv.lock .gitignore
```

| Проверка | Фактический результат |
| --- | --- |
| Packaging suite | 26 passed; перед исправлением нового contract — 3 failed / 7 passed |
| Documentation suite | 56 passed; включает 11 M5 cases с offline example и docstrings |
| Узкие M5 suites | 184 passed |
| `make lock-check` | exit 0, 100 packages resolved; lockfile не изменён |
| `make lint typecheck` | Ruff: 175 файлов, без замечаний; mypy: 173 файла, без ошибок |
| `make test` | 1869 passed, 5 SWIG warnings, 62.95 s |
| `make test-integration` | 16 passed / 1853 deselected, 5 SWIG warnings, 3.48 s |
| `make test-security` | 321 passed, 15.62 s |
| `make test-build` | wheel/sdist, byte-for-byte offline rebuild и isolated base import: `distribution verification OK` |
| `make docs` | strict clean MkDocs build: exit 0 |
| `git diff --check` | exit 0 |
| Локальный `main...HEAD` | 0 / 0; весь M5 пока находится в uncommitted diff |

Дополнительно выполнена AST-проверка ссылок матрицы и локальный scan всех
tracked/untracked файлов diff: 64 файла, 17 modified и 47 new. Шаблоны private
keys/provider tokens/credential assignments не дали совпадений, debugger imports
не найдены. Проверены все `print`: `_hashseed_probe.py` — используемый property
test subprocess, `scripts/verify_distribution.py` — существующий проверочный CLI.
Production debug prints, посторонние расширения и случайные generated files
в diff не обнаружены. `git check-ignore` подтвердил исключение `dist/`, `site/`
и pytest cache; результаты команд хранятся вне repository в `/private/tmp`.

Первичные неуспешные прогоны не скрыты: sandbox вызывал panic uv в macOS
SystemConfiguration, а document watchdog требует `/bin/ps`; соответствующие
команды повторены вне sandbox с сохранением всех checks. Затем `test-build`
выявил устаревший manifest M4, исправленный regression-first. После этого
offline rebuild не нашёл Hatchling 1.32.0 во временном cache; повтор с существующим
локальным cache завершил полный gate. Первый strict docs build после записи
checklist обнаружил две неверные anchor-ссылки; они исправлены по фактически
сгенерированным MkDocs IDs. Это не пропущенные проверки.

### Пропущенные проверки и residual risks

| Проверка / ограничение | Почему не подтверждено |
| --- | --- |
| Remote CI на Ubuntu/Python 3.12–3.14, свежесть remote `main` | Push/PR/fetch в этой подготовке не выполнялись; CI запускается после ручной публикации. Локальный результат относится к macOS/Python 3.12.9 |
| Многогигабайтный corpus и пиковый RSS native backends | Проверены ограниченные fixtures, counters/state и N/N+1; production workload и benchmark target для M5 не заданы. Bounded counters не заменяют замер RSS |
| Полная матрица optional backend versions и fresh all-extras install | M5 не меняет dependencies/backends; проверены установленные document adapters и isolated base wheel. Все комбинации extras этим не подтверждаются |
| Реальный Tika/JVM, OCR, сложные PDF layouts, downstream DB/staging/rollback | За пределами M5; Tika opt-in и ограничения M4 сохраняются. До terminal manifest output требует downstream staging |
| Реальные LLM API | M5 не использует LLM; calls запрещены scope, offline tests подтверждают отсутствие обращения к сети |
| Внешний CVE/secret service и HTTP link checker | В repository нет настроенного gate для этих сервисов; выполнены локальный scan и MkDocs internal links/anchors. Production dependencies не добавлены |

Sampling gaps и неподтверждённый scope запрещают automatic plan. Hash binding
доказывает согласованность snapshot, а не подлинность внешнего источника.
Существенных незакрытых findings в повторно проверенном diff не выявлено.

### Состав ручного commit

В diff 64 файла. Все untracked файлы относятся к M5 и должны быть включены
при ручном staging; выбор только tracked diff пропустит основную реализацию.

| Область | Файлы / каталоги |
| --- | --- |
| Core contracts, 6 файлов | `contracts/{analysis,common,execution,normalized,parsing,structure}.py` |
| Public ports/errors, 3 файла | `ports/{__init__,semantic}.py`, `exceptions.py` |
| Реализация, 15 файлов | `packages/structuraguard/src/structuraguard/structure/*.py` |
| Tests, 25 файлов | `tests/{unit,property,security}/structure/`, `tests/docs/test_m05_examples.py`, `tests/integration/test_structure_documents.py`, изменённые contracts/ports/layer-boundary и packaging tests |
| Документация/config, 14 файлов | `docs/{architecture,index,public-api,security,structure}.md`, `docs/codex/{PROJECT_STATE,SPEC_INDEX}.md`, `docs/plans/M05_*.md`, ADR 0008–0010, `mkdocs.yml` |
| Distribution gate, 1 файл | `scripts/verify_distribution.py` |

Пути `contracts/`, `ports/`, `exceptions.py` в этой таблице относительны
`packages/structuraguard/src/structuraguard/`; `tests/` — `packages/structuraguard/`.

[spec-fr-014]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#fr-014-structural-profiling-и-parseplan
[spec-m5]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m5-structural-profiler-и-deterministic-parseplan

## Цель

Преобразовать проверенный поток `ExtractedBatch` в ограниченный структурный
профиль, детерминированно предложить варианты структуры и потоково исполнить
независимо проверенный декларативный `ParsePlan` с точным provenance, без LLM.

### Основания и текущее поведение

- Требования: ТЗ `FR-012.1` (Extracted Source Model), `FR-014` (Structural
  profiling и ParsePlan), `20.9`, `20.13` (ресурсные пределы и изоляция), `M5`.
  Строка `Database Inspector → M5` в `docs/codex/SPEC_INDEX.md` устарела;
  настоящий план следует текущей задаче и разделу M5 ТЗ. Индекс здесь не меняется.
- `contracts/source.py`, `contracts/parsing.py`, `contracts/normalized.py` и
  `ports/semantic.py` уже задают физические DTO, четыре вида plan,
  `SemanticStructureAnalyzer`, `ParsePlanValidator`, `ParsePlanExecutor` и
  цепочку fingerprints. На момент исходного планирования конкретных реализаций M5 ещё не было.
- M4 сохраняет физическую структуру: TXT — строки; LOG — строки, технические
  события и captures; CSV/TSV/XLSX — cells и table segments; JSON/XML/YAML —
  узлы, порядок, исходные ключи; HTML/Markdown/PDF/DOCX — деревья, блоки,
  headings, таблицы и доступные координаты. LOG recognizers и CSV
  `header_candidate` — входные наблюдения, а не окончательные правила M5.
- По ADR 0003/0004 данные и plans недоверенны; physical extraction `1.1.0`
  сохраняет raw values, continuation и canonical fingerprints. Manifest
  содержит не более 10 000 indexed refs; это selective allowlist, а не каталог
  всего источника. Built-in parsers преимущественно индексируют префикс.
- `StructureAnalysisRequest` уже ограничен 1 000 samples и 1 MiB их canonical
  UTF-8 serialization; defaults — 100 и 65 536 bytes. `ParseExecutionContext`
  пока ограничивает только records на выходной batch — для M5 этого мало.

Здесь и далее пути `contracts/`, `ports/`, `domain/`, `parsers/`, `structure/`
относительны `packages/structuraguard/src/structuraguard/`; пути `tests/` —
`packages/structuraguard/tests/`. Новые файлы перечислены как планируемые.

## Критерии приёмки

- A выдаёт bounded профиль с наблюдениями, sample coverage и доказательствами;
  B — ranked candidates и `plan_created`, `needs_review` либо `rejected`;
  C — accepted/rejected validation и поток `NormalizedBatch` с manifest.
- Обязательные семейства: таблицы, деревья, TXT/LOG и документы. Для каждого
  есть однозначный положительный пример, неоднозначный пример и отрицательные
  проверки. Неоднозначность не разрешается неявным выбором первого кандидата.
- Обнаруживаются candidate headers, data/meta/footer/summary regions,
  repeated headers, record boundaries, repeated key-value groups, nested
  collections, log templates, document sections/block groups и candidate fields.
  Semantic type hints содержат основания и альтернативы, не сведения о БД.
- Состав полей, parent/child relations, пропуски, исключаемые regions и
  преобразования определены plan. Исполнитель не переанализирует структуру и
  не исправляет plan по новым данным молча.
- На больших входах размер retained state ограничен явными бюджетами;
  границы batches не теряют и не дублируют records. Отмена, timeout,
  повреждённый stream и превышение лимита имеют отдельные контролируемые исходы.
- Не исполняются code, SQL, shell, callbacks, выражения XPath/CSS или
  пользовательские regex; нет DB/LLM/network dependencies и новых production
  dependencies. Технические adapters сохраняют свою ответственность.
- Property tests подтверждают детерминизм, корректность разбиения, provenance,
  соблюдение лимитов и неизменность raw content независимым oracle.

## Затронутые контракты и решения

### Совместимость и недостающая выразительность

Сохранить существующие ports анализатора, валидатора и executor. Добавить
`StructuralProfiler` port в `ports/semantic.py`: async обработка extracted
stream с явными immutable options; результат содержит terminal manifest,
профиль и bounded samples либо typed empty/insufficient-evidence outcome.
Profiler не получает source reader, parser registry, LLM или DB port.

В `contracts/parsing.py` минимально расширить существующие DTO закрытыми
discriminated unions, а не создавать второй набор plans:

| Пробел текущего контракта | Изменение M5 |
| --- | --- |
| Shape observations содержат только базовые размеры/refs | Typed observations для regions, boundaries, repeated groups, collections, templates, sections, candidate fields и покрытия; у каждого конечные размеры и источник evidence |
| Candidate содержит kind и refs, но не саму проверяемую гипотезу | Закрытое описание структуры и ссылки на `StructureEvidence.evidence_id`; breakdown scores, penalties, coverage и версия scoring policy |
| `TabularShapeObservation.header_candidates` — offsets внутри sample; plan использует физические rows | Явное соответствие sample offsets физическим индексам; абсолютные ranges имеют определённую систему координат, не меняют legacy offsets |
| `TabularParsePlan` требует ровно один header и одну data region | Versioned описание отсутствующего/многострочного header, непересекающихся data regions и типизированных исключений meta/summary/repeated headers; legacy поля остаются допустимыми без двусмысленного смешения |
| Tree paths используют `IdentifierStr`, недостаточный для raw Unicode/duplicate keys и повторов | Закрытые шаги raw-key с occurrence, array item/index и XML expanded name; ограниченные relative paths от record root, без интерпретатора выражений |
| LOG selector выбирает один token; document grouping не задаёт section boundaries | Bounded token-span/key-value selectors и закрытые boundary/group rules, включая повторяющийся стартовый ключ и технический тип блока |
| `line_refs`/`block_refs` могут трактоваться как полный набор записей | Явный versioned execution scope: anchors служат evidence, правила определяют покрываемый диапазон полного stream; legacy refs не получают расширенную область действия молча |
| Пустой input нельзя выразить текущим непустым profile/request | Typed outcome profiler до вызова analyzer; без фиктивного evidence, пустого sample или ослабления старых invariants |

Новые semantic/profile поля и operators публиковать как schema `1.1.0` в
своём семействе DTO; версия physical extraction от этого не меняется. Старые
payload `1.0.0` сохраняют canonical serialization и fingerprints. Новые поля
отсутствуют в legacy serialization, неподдерживаемые версии и сочетания
отклоняются. Требуемые изменения contracts сначала закрепить compatibility
tests; миграции БД и автоматической перезаписи сохранённых plans нет.

Долгоживущие решения о schema, execution scope, replay и safe patterns
оформить при реализации A1 в `docs/adr/0008-bounded-structural-planning.md`
(номер проверить на момент реализации). Настоящий план не объявляет новый ADR
принятым и не меняет уже принятые правила ADR 0003/0004.

### Samples, поток и воспроизводимость

1. A проходит stream до terminal manifest, проверяя batch sequence, hashes,
   counts, continuity и allowlist, сохраняя только bounded окна, counters,
   signatures и samples. Профиль полного extraction публикуется после проверки
   terminal manifest; досрочный stop не выдаётся за завершённое извлечение.
2. Raw samples выбираются детерминированно из реально существующих indexed
   refs: приоритет семейств и anchors, затем физический порядок. Политика,
   бюджеты, examined/retained/skipped counts и причины неполного покрытия
   сохраняются в profile. Переполнение sample budget означает неполную выборку,
   а не усечение raw value и не автоматическую ошибку всего source.
3. Для footer/summary использовать ограниченное хвостовое окно и агрегаты по
   таблице. Числовой диапазон допустимо обосновать indexed table/root anchor и
   typed observation, если полный проход подтвердил принадлежность и границы.
   Raw sample или прямая ссылка на поздний объект вне index запрещены. Когда
   доказать нужный scope нельзя, результат — `needs_review` либо явный отказ
   при отсутствии кандидатов; index не расширяется скрытно.
4. После B и validation C получает новый stream **того же extraction** от
   вызывающего кода. Для built-in parser допустим повторный запуск по тому же
   immutable snapshot с теми же adapter version и options; совпадение всего
   manifest обязательно проверяется. Для unreplayable plugin нужен уже
   доступный проверяемый replay; иначе typed отказ до execution. Создание
   универсального spool/store и автоматический повтор сетевого Tika вне M5.
5. Не делать `list()` всего source, unbounded `tee`, full DOM или полный индекс
   всех refs. Память — текущий bounded input/output batch, окна, состояние
   ограниченного числа структур и bounded manifest summaries. Один слишком
   большой неделимый record отклоняется, а не разбивается с потерей смысла.
6. Одинаковые extraction, options и component versions дают одинаковые
   profile/plan payload, ranking и fingerprints. ID выводятся из canonical
   структурных данных; порядок set/dict, hash seed, locale и случайность не
   влияют на решение. `validated_at` приходит от явного clock dependency;
   wall-clock deadline не входит в semantic scoring.
7. При смене parser batch size физические refs и extraction fingerprint могут
   измениться. Проверять одинаковую semantic projection и реальные locations,
   а не требовать одинаковых persisted fingerprints разных extraction runs.

### Ресурсные пределы

Добавить immutable `StructuralProfilingOptions`, `StructureAnalysisOptions`
и execution limits в `contracts/parsing.py`; `structure/options.py` применяет
их бюджеты, а contracts/ports не импортируют реализации из `structure/`.
Проверять ограничения до allocation, serialization, tokenization и
expansion. Не создавать фиктивный общий `SecurityLimits`: в текущем коде
работают `ParseContext` и format-specific limits; M5 получает явные budgets и
не ослабляет upstream ограничения. Предлагаемые defaults/hard caps для M5:

| Ресурс | Default / hard cap | Поведение при достижении |
| --- | --- | --- |
| Raw samples / canonical bytes | 100 / 1 000; 65 536 / 1 048 576 | Coverage issue, whole-value sampling; старые hard caps сохраняются |
| Активные tables/collections/sections | 64 / 256 | Typed limit error до добавления состояния |
| Header/data window; tail window на структуру | 64 / 256 rows; 32 / 128 rows | Ограниченное окно; неполное покрытие указано явно |
| Distinct signatures / log clusters | 128 / 1 024 | Счётчик overflow и запрет уверенного auto-plan для непокрытых variants |
| Ranked candidates | 8 / 32 | Стабильный top-K и счётчик отброшенных; неоднозначность оценивается до отсечения |
| Fields / entities в plan | 500 / 1 024; 16 / 32 | Typed отказ; existing DTO caps не повышаются |
| Retained profile state / serialized plan | 8 / 32 MiB; 1 / 4 MiB | Typed limit error; включать metadata, paths, evidence и string bytes |
| Path depth / pattern tokens / token chars | 30 / 30; 64 / 256; 1 024 / 4 096 | Отказ до traversal/matching; upstream более строгий depth имеет приоритет |
| Lines или blocks на record / record bytes | 100 / 1 000; 1 / 4 MiB | Не выдавать частичный record |
| Output records на batch / bytes | 1 000 / 10 000; 1 / 8 MiB | Flush между полными records; неделимый oversized record — отказ |
| Total normalized records / entities / values | 1 / 1 млн; 2 / 10 млн; 10 / 10 млн | Независимые counters до fan-out, без cartesian expansion |
| Total batches / physical objects scanned | Не больше upstream; hard caps 10 000 / 10 млн | Контролируемое завершение с ошибкой |
| Время одного этапа | 300 / 300 секунд | `PROCESSING_TIMEOUT`, cooperative checkpoints и закрытие owned iterator |

Все дополнительные списки rules, regions, evidence и counters также ограничены
количеством и байтами; нельзя рассчитывать только на общий serialized-size
check после построения огромного DTO. `bool`, non-finite, нулевые/отрицательные
budgets и значения сверх caps отклоняются. Глобальные counters защищают от
множества малых групп; parser sandbox requirements продолжают действовать.

## Шаги

Последовательность: A1 → A2 → A3 → B1 → B2 → C1 → C2 → C3. Каждый шаг начинается
с теста указанного поведения; после него запускаются узкие проверки. Каталоги
`structure/` и `tests/{unit,property,security}/structure/` создаются в M5.

### A. `StructuralProfiler`

**A1. Contracts, budgets и пустой источник.**

- Файлы: `contracts/parsing.py`, `contracts/common.py`, `contracts/__init__.py`,
  `ports/semantic.py`, `ports/__init__.py`, `structure/options.py`,
  `docs/adr/0008-bounded-structural-planning.md`.
- Зафиксировать таблицу расширений выше, immutable options, evidence links,
  units/index conventions и typed outcomes. Producer metadata включает
  component/policy version и options fingerprint; profiles получают проверяемый
  canonical fingerprint, а не доверяют переданной строке hash.
- Тест: `tests/unit/contracts/test_m05_structure_contracts.py` — round-trip,
  legacy bytes/fingerprints, unknown versions/operators, forged model objects,
  bounded collections, empty source без fabricated evidence.
- Проверка: `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/contracts/test_m05_structure_contracts.py packages/structuraguard/tests/unit/contracts/test_m02_semantic_contract_regressions.py`.

**A2. Проверяемый проход и sampler.**

- Файлы: `structure/profiling.py`, `structure/sampling.py`,
  `structure/_stream.py`, `structure/options.py`. Canonical/lineage helpers
  использовать из `domain/canonical.py`, `domain/lineage.py`; проверку extraction
  согласовать с `parsers/execution.py` и `parsers/_hashing.py`, не вызывать private
  parser runtime из domain. Только при реальном дублировании выделить общий
  чистый validator, сохранив parser regressions.
- Bounded stream verification, source-index membership, выбор целых samples,
  full-pass coverage, tail windows, terminal manifest и replay prerequisites.
  Не считать batch boundary концом table, tree, section или multiline record.
- Тесты: `tests/unit/structure/test_sampling.py`,
  `tests/unit/structure/test_profile_stream.py` — source больше sample/index
  budget, oversized scalar, один неделимый oversized batch, поздний footer,
  отсутствующий anchor, torn/duplicate segments, cancellation и cleanup.
- Проверка: `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/structure/test_sampling.py packages/structuraguard/tests/unit/structure/test_profile_stream.py packages/structuraguard/tests/unit/parsers/test_m04_stream_integrity.py`.

**A3. Структурные наблюдения четырёх семейств.**

- Файлы: `structure/tabular.py`, `structure/tree.py`, `structure/text.py`,
  `structure/document.py`, `structure/profiling.py`.

| Семейство | Наблюдаемое поведение | Обязательные cases в `tests/unit/structure/` |
| --- | --- | --- |
| Таблицы | Оценивать несколько header rows/spans и вариант без header по заполненности, уникальности и контрасту типов; выделять data start/end, meta/footer/summary и repeated headers. Ragged/missing/empty различать, merged ranges учитывать как контекст без fill-down | `test_tabular_profile.py`: preamble, duplicate/empty headers, headerless data, multirow header, summary в середине и хвосте, repeated header между segments, разные sheets, подозрительное слово «Итого» в обычной записи |
| Деревья | По `node_kind`, raw names, parent/order/occurrence находить repeated paths, record roots, parent/child collections и относительные field paths; не flatten и не объединять duplicate keys | `test_tree_profile.py`: JSON array и JSONL roots, nested arrays, XML namespace/siblings, YAML repeated groups, scalar/empty collections, heterogeneity, duplicate/Unicode keys и continuation root |
| TXT/LOG | Предлагать line/block boundaries: отдельная строка, фиксированный размер, blank separator, bounded start-token/template и multiline continuation. Repeated key-value groups распознавать по повтору набора/стартового ключа; разделять record variants | `test_text_profile.py`: группы с перестановкой ключей, missing/duplicate key, multiline между batches, orphan continuation, смешанные шаблоны, prose с единичным `key=value` |
| Документы | По порядку и доступным physical hints находить sections/headings, списки, соседние block groups, key-value pairs, таблицы и extraction targets; HTML tree и block projections не дублировать | `test_document_profile.py`: PDF page/block и bounding box, DOCX heading/run/table, HTML heading/list/table, Markdown sections, repeated page furniture, table-only document, граница section между batches |

- Log template clustering: фиксированный tokenizer превращает только известные
  lexical классы в typed slots; сохраняет literal anchors и позиции. Группировать
  по canonical token signature с bounded dictionary и linear token scan; не
  использовать all-pairs edit distance, LLM/embeddings или меняющийся online
  centroid. Различные literals не склеивать без явного правила. Template хранит
  counts/slot descriptors и bounded evidence; raw сообщения не попадают в logs.
- Candidate fields получают стабильные технические ID, raw label evidence,
  presence/nullability/cardinality и возможные lexical types: string, integer,
  decimal, boolean, ISO date/time, identifier. Leading zeros, locale-dependent
  числа/даты и timezone-naive timestamps сохраняют неоднозначность. Excel serial
  date и formula/cached value не получают бизнес-тип только из style/hint.
- Проверка: `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/structure/test_tabular_profile.py packages/structuraguard/tests/unit/structure/test_tree_profile.py packages/structuraguard/tests/unit/structure/test_text_profile.py packages/structuraguard/tests/unit/structure/test_document_profile.py`.

### B. `DeterministicStructureAnalyzer`

**B1. Evidence, confidence и несколько кандидатов.**

- Файлы: `structure/analysis.py`, `structure/scoring.py`,
  `structure/fields.py`, `contracts/parsing.py`.
- Реализовать существующий `SemanticStructureAnalyzer.analyze` для
  `DETERMINISTIC`; другие modes получают явный unsupported outcome без fallback.
  Вход — проверяемые profile/manifest/samples; candidate не разрешает execution.
- Breakdown содержит support/eligible counts, coverage, regularity, field/type
  consistency, boundary support и penalties за raggedness, overlap,
  contradictory/insufficient evidence. Для каждого слагаемого — evidence IDs,
  вес и вклад; веса/формула отдельно версионируются. Использовать `Decimal`
  с явными precision/rounding, не глобальный decimal context. Confidence —
  воспроизводимый score, не обещание статистической вероятности.
- Зафиксировать формулы и thresholds golden cases в B1; начальная policy:
  auto-plan требует score ≥ 0.85, отрыв ≥ 0.15 от следующей несовместимой
  гипотезы, отсутствие blocking issues и подтверждённые границы scope.
  Scoring ties сортируются по kind, физическому anchor и canonical hypothesis;
  stable order не является основанием выбрать победителя.
- Профиль различает конкурирующие гипотезы для одного scope и независимые
  структуры. Несколько независимых таблиц/sections не объявляются плохими
  альтернативами и не теряются при выборе одного plan. В M5 выдавать их
  отдельными candidates; вызывающий код явно выбирает scope. Composite plan
  для всего гетерогенного source не вводится.
- Тесты: `tests/unit/structure/test_scoring.py`,
  `tests/unit/structure/test_analysis.py` — golden ranking/breakdown, ties,
  low coverage, conflicting evidence, top-K overflow, multiple scopes,
  несовместимый mode, стабильность IDs и input immutability.
- Проверка: `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/structure/test_scoring.py packages/structuraguard/tests/unit/structure/test_analysis.py`.

**B2. Компиляция кандидата в закрытый plan.**

- Файлы: `structure/planning.py`, `structure/patterns.py`,
  `structure/analysis.py`; новые selectors/groupings — в `contracts/parsing.py`.
- Строить `TabularParsePlan`, `TreeParsePlan`, `LogParsePlan` или
  `DocumentParsePlan` с явными regions, boundaries, fields, entities, parents,
  scope и provenance. Имена из source нормализуются в стабильные identifiers
  с детерминированным разрешением collisions; raw labels сохраняются в evidence.
- Nested/repeated collections дают child entities своего parent; несколько
  child arrays не перемножаются. Repeated key-value groups задают отдельные
  records; duplicate keys имеют explicit occurrence/cardinality policy,
  отсутствующие поля не превращаются в данные соседней группы.
- Safe pattern policy: только конечный словарь token classes, literal equality,
  delimiters, bounded token spans и закрытые start/end conditions. Никакого
  поля `regex`, arbitrary callbacks, Python/SQL/shell, dynamic import или
  выполнения исходного XPath/CSS. Имеющиеся фиксированные recognizers M4
  остаются техническими наблюдениями; M5 не принимает regex из source/plan.
  Пользовательский safe-regex subset отложен: deny-all для regex проще
  проверяемого интерпретатора и исключает ReDoS на этой границе.
- Literal с текстом SQL/code допустим как данные в разрешённом поле; запрет
  проверяется по grammar/operators, а не поиском опасных слов в raw content.
- Semantic hints сами не меняют значения. В M5 допустимы только явно заданные
  lossless built-in conversions (integer, Decimal, однозначные ISO date/UTC
  datetime) с transformation codes; остальное сохраняется строкой/техническим
  типом либо требует review. Денежный тип требует Decimal; locale repair,
  timezone guessing, deduplication и business normalization вне M5.
- Тесты: `tests/unit/structure/test_plan_generation.py`,
  `tests/unit/structure/test_patterns.py` — все четыре variants, explicit scope,
  nested children, field collisions, безопасные literals, неоднозначные типы.
- Проверка: `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/structure/test_plan_generation.py packages/structuraguard/tests/unit/structure/test_patterns.py`.

### C. `ParsePlanValidator` и `ParsePlanExecutor`

**C1. Независимая validation без I/O.**

- Файлы: `structure/validation.py`, `structure/options.py`,
  `contracts/parsing.py`, `contracts/common.py`, `exceptions.py`.
- Реализация port повторно проверяет DTO, canonical hashes всей доступной
  lineage, schema/operator allowlists, plan size/depth/counts, refs и их kinds,
  existence в manifest index, profile/evidence membership, regions и overlap,
  field ownership, entity cycles, cardinality, pattern budgets и bounds.
  Проверять не только Pydantic construction: `model_copy`/`model_construct`
  и сериализованный wrapper не являются доверенной validation.
- Статическая validation доказывает согласованность с bound snapshot и
  observations. Manifest index не хранит все cells/nodes, поэтому существование
  каждого runtime path/range и точное соответствие содержимому доказывает C2
  при чтении. Нельзя сообщать о проверке позднего source location только по hash.
- Несогласованные selectors, header/footer ranges, overlapping record starts,
  parent/child scopes и неизвестные semantic conversions отклоняются. Результат
  использует `ParsePlanValidationResult`; только accepted содержит wrapper.
  Предлагаемые stable codes: `PARSE_PLAN_INVALID`, `PARSE_PLAN_UNSUPPORTED`,
  `PARSE_PLAN_SOURCE_MISMATCH`, `STRUCTURE_AMBIGUOUS`,
  `STRUCTURE_INSUFFICIENT_EVIDENCE`; зарегистрировать их в существующем словаре.
- Тесты: `tests/unit/structure/test_validation.py`,
  `tests/security/structure/test_plan_boundary.py` — tamper/stale fingerprint,
  foreign/absent ref, unsafe/unknown operator, cyclic entities, excessive rules,
  overlapping variants, error serialization без raw values/secrets.
- Проверка: `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/structure/test_validation.py packages/structuraguard/tests/security/structure/test_plan_boundary.py`.

**C2. Bounded execution с runtime verification.**

- Файлы: `structure/execution.py`, `structure/_stream.py`,
  `structure/patterns.py`, `structure/validation.py`; при необходимости чистые
  canonical builders в `domain/lineage.py`, без infrastructure imports.
- Перед первым yield проверить context, limits, wrapper/plan fingerprints и
  supported policy. Так как wrapper не capability, повторить структурные и
  security проверки. Добавить bound profile в `ParseExecutionContext`: поле
  отсутствует в legacy serialization и обязательно для execution schema
  `1.1.0`, чтобы повторить полную независимую validation C1.
  Для legacy context без требуемого evidence — typed unsupported outcome;
  это ограничение новой реализации документировать явно.
- Каждый входной batch проверить до использования: schema/hash/source/parser,
  expected summary/index/order, table/tree continuation и физические ссылки.
  Правила исполняются по полному заявленному scope, не только samples. Реальные
  selected objects могут отсутствовать в sample index; output provenance
  строится по проверенным объектам stream, никогда не копируется с примера.
- Между batches хранить только незавершённый bounded record/group, header
  signature, traversal stack и ограниченные counters. Repeated headers и
  footer исключать лишь по plan; поздний неожиданный variant, missing required
  field, неоднозначный путь или превышение cardinality — typed execution error
  (`PARSE_EXECUTION_MISMATCH`), без частичной записи или silent skip.
- Raw values и порядок сохраняются. Missing ragged cell, explicit empty,
  null и empty collection различаются; optional missing field опускается по
  объявленной missing policy, без synthetic source ref. Сохранять цепочку value →
  entity → record refs и parent relations, включая JSON duplicate occurrences,
  XPath namespace, CSS selectors, sheet/cell, page/block/box и line spans.
  Не обещать byte offsets для CSV или subspan, которого physical model не даёт.
- Если selector извлекает подстроку без отдельного physical value, сохранять
  parent ref и bounded slice/selector trace через versioned optional extension
  `NormalizedValue`; raw_value хранит исходное значение родителя,
  normalized_value — результат selector с transformation code. Проверять slice
  на исходном тексте; physical IDs не выдумывать. Новый trace требует contract tests.
- Выдавать bounded `NormalizedBatch` с устойчивыми IDs, schema и hash chain;
  terminal manifest — только после EOF/terminal verification и закрытия всех
  continuation groups. При ошибке в хвосте уже выданные batches остаются
  промежуточными; успешного terminal outcome нет. Транзакции/staging downstream
  в M5 не реализуются. Прямое выполнение валидного плана над пустым разрешённым
  scope выдаёт один пустой terminal batch; profiler пустого source не создаёт plan.
- Отмена не подавляется; idle consumer не отменяется оставленным timer task.
  Ownership input iterator, `aclose` при ошибке/раннем выходе и сохранение
  primary error при cleanup failure задаются явно и проверяются.
- Тесты: `tests/unit/structure/test_execution.py`,
  `tests/security/structure/test_execution_boundary.py` — четыре variants,
  fake checked wrapper, replay mismatch, late schema drift, synthetic refs,
  incomplete final record, fan-out budgets, backpressure и cleanup.
- Проверка: `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/structure/test_execution.py packages/structuraguard/tests/security/structure/test_execution_boundary.py`.

**C3. Сквозная приёмка и property tests.**

- Файлы: `tests/contract_suites/structure.py`,
  `tests/unit/structure/test_m05_acceptance.py`,
  `tests/property/structure/test_determinism.py`,
  `tests/property/structure/test_execution_properties.py`,
  `tests/security/structure/test_resource_limits.py`,
  `tests/integration/test_structure_documents.py`.
- Проверить путь existing parser → A → B → C для TXT/LOG, CSV/TSV, JSON/JSONL,
  XML/YAML, HTML/Markdown и XLSX/PDF/DOCX с bounded fixtures. Document backend
  cases пометить `integration`; сеть не требуется. Использовать существующие
  fixture builders из `tests/unit/parsers/builtin/`, не подменять adapters
  вручную придуманными shapes во всех сквозных тестах.
- Hypothesis: менять Unicode/duplicate keys, whitespace и raggedness,
  расположение boundaries, число повторов, read chunk/batch size; сравнивать
  независимый oracle records/locations. Для неизменного extraction проверять
  canonical equality и hash-seed independence; при rebatching — semantic
  equality и новую корректную lineage. Отдельно проверить parent/child
  multiplicity без cartesian product и сохранение каждого selected raw value.
- Property/security tests: limits ровно N и N+1, overflow signatures, множество
  мелких объектов и один огромный, truncated sample, malformed plan, hostile
  regex payload, unknown operators, resource counters до allocation,
  cancellation на разных boundaries и отсутствие secrets в diagnostic output.
  Сложность проверять счётчиками retained state/operations и adversarial input,
  не хрупким сравнением абсолютных секунд или только small fixtures.
- Документация при реализации: `docs/architecture.md`, `docs/public-api.md`
  (ports, schema compatibility, sampling/replay, промежуточные batches),
  новый `docs/security.md` (limits/pattern policy), этот план (фактическая приёмка).
- Узкая проверка: `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/structure packages/structuraguard/tests/property/structure packages/structuraguard/tests/security/structure packages/structuraguard/tests/integration/test_structure_documents.py`.
- Review реализации через `structuraguard-review` и `structuraguard-security`.
  Перед закрытием M5: `make lint`, `make typecheck`, `make test`,
  `make test-integration`, `make test-security`, `make docs`.

## Риски и границы

- Selective prefix index ограничивает подтверждаемое покрытие поздних структур;
  часть sources потребует review. Повышать confidence без evidence запрещено.
- Heuristics могут принять metadata/summary за records. Нужны конкурирующие
  candidates, отрицательные сигналы, explicit exclusions и late-drift errors.
- Закрытые selectors M2 недостаточны для всех повторов M5: schema evolution и
  compatibility tests обязательны, особенно для raw keys и execution scope.
- Два прохода удваивают техническое чтение и требуют immutable replay. M5 не
  гарантирует replay произвольного plugin и не создаёт unbounded memory cache.
- Hash binding обеспечивает целостность внутри проверяемого snapshot, но не
  достоверность присланных извне assertions. Runtime verification и повторная
  проверка wrapper обязательны; success зависит от terminal manifest.
- Сложные PDF layouts, нечёткие log templates, неоднозначные типы и heterogeneous
  roots могут завершаться review. OCR, embeddings, LLM, DB mapping, composite
  heterogeneous plans, пользовательский regex engine, новый storage и общая
  normalization engine относятся к следующим работам.

Предложенные имена файлов и команды исторического design не являются журналом
выполнения. Реальные tests, результаты gates и незакрытые пункты зафиксированы
в checklist подготовки выше и в матрице приёмки.
