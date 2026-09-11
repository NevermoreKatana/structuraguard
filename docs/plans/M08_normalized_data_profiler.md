# M08 — bounded Normalized Data Profiler

Статус: локальная приёмка M8 завершена; готов к ручному commit и последующему
Pull Request в `main` в подтверждённом bounded scope. Commit/push/PR не созданы.

## Передача для ручного commit и PR {#checklist-commit-pr}

Проверено 2026-09-11. Активная ветка `feat/m08-normalized-data-profiler`;
`HEAD`, локальный `main` и сохранённый `origin/main` совпадают: `2d813c4`.
`git rev-list --left-right --count main...HEAD` и аналогичная проверка
`origin/main...HEAD` дали `0 0`. M7 и его CI fix уже входят в эту базу.
Удалённый сервер не опрашивался: это сравнение локальных refs, не гарантия
отсутствия новых commits на сервере. Staging area пустая.

### Checklist локальной готовности

- [x] K1: полный NormalizedBatch stream, repeated/late/schema-only поля и empty
  cases подтверждены unit/property и реальным semantic parsing integration.
- [x] K2: counts/null/unique/extrema/Unicode lengths независимы от sampling;
  exact/estimated distinct и нулевые знаменатели проверены отдельными oracle.
- [x] K3: locale/type/currency/timezone конфликты остаются явными;
  regression неверных offset minutes подтверждает отсутствие ложного datetime.
- [x] K4: независимые и общие budgets, сокращение evidence, логический поток
  10¹² records и фактический serialized output cap проверены.
- [x] K5: patterns, categorical/identity и PII evidence проверены positive/negative
  cases; malformed domains не дают natural key, PII minimum сохраняется.
- [x] K6: content fingerprint проверен на invariance/sensitivity и golden vector;
  старый manifest hash и lineage сохранены отдельно.
- [x] K7: forged DTO, sequence/EOF, duplicate IDs, cancellation/deadlines,
  cleanup, повторный вызов и concurrency проверены без публикации partial profile.
- [x] Все ссылки на tests в [матрице приёмки](M08_acceptance.md) и
  [исправлениях финального review](#m08-final-review-fix) проверены через AST:
  **86 references**. Исходные критерии и ограничения не ослаблены.
- [x] Последние четыре Medium исправлены с regression tests; повторный review
  существенных незакрытых findings в проверенном M8 diff не выявил.
- [x] Public API, старые DTO и dependency boundaries сохранены; новых production
  dependencies, DB/LLM полномочий и import-time I/O нет.
- [x] Русские docstring, копируемый offline-пример, ограничения, ADR 0018 и
  `PROJECT_STATE.md` соответствуют подтверждённому поведению и текущей ветке.
- [x] `git diff --check` и проверка новых файлов на whitespace прошли.
  Во всех 43 commit-кандидатах не обнаружены secrets, debug artifacts,
  закомментированный код, случайные binaries/generated/temp files.
  Вывод benchmark и проверок distribution — намеренная диагностика scripts.
- [x] Успешные команды и пределы проверок приведены ниже; paid LLM не вызывались.
- [ ] Вручную выбрать и stage файлы M8, проверить staged diff.
- [ ] Вручную создать commit; затем обновить refs и повторить сравнение с `main`.
- [ ] Вручную push ветку и создать PR с base `main`.
- [ ] Проверить CI на точном commit PR перед merge; commit/PR/merge этим шагом
  не выполняются по прямому указанию пользователя.

### Состав commit

**43 файла: 10 modified и 33 new.** Учитываются untracked files: один
`git diff --stat` не показывает полный состав milestone.

| Группа | Файлы / каталоги относительно корня repository |
| --- | --- |
| Production — 15 | `packages/structuraguard/src/structuraguard/`: `contracts/{__init__,profiling}.py`, `domain/normalized_fingerprint.py`, `exceptions.py`, `ports/{__init__,profiling,security}.py`, все 8 файлов `profiling/` |
| Tests — 17 | `packages/structuraguard/tests/`: `unit/profiling/` (6), `security/profiling/` (4), `property/profiling/` (2), `contract/profiling/test_ports.py`, `docs/test_m08_examples.py`, `fakes/profiling.py`, `integration/test_normalized_profiling.py`, `unit/ports/test_m02_protocols.py` |
| Docs/config — 9 | `docs/normalized-profiling.md`, `docs/public-api.md`, `docs/codex/{PROJECT_STATE,SPEC_INDEX}.md`, `docs/adr/0018-bounded-normalized-profiling.md`, `docs/plans/M08_{normalized_data_profiler,acceptance,security_review}.md`, `mkdocs.yml` |
| Scripts — 2 | `scripts/benchmark_normalized_profiler.py`, `scripts/verify_distribution.py` |

### Фактически выполненные команды

Полные gates относятся к последнему исправлению production-кода в этой сессии;
на шаге передачи изменяется только документация. Проверки передачи выполнены
повторно на текущем checkout. Python 3.12.9, macOS 26.1 arm64.

| Команда | Результат / этап |
| --- | --- |
| `make lint typecheck test test-integration test-security test-build` | После последних fixes: **2717 passed**, 84 PostgreSQL deselected; **22 integration**, **599 security**; offline wheel/sdist verification OK; exit 0 |
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/unit/profiling/test_final_review.py packages/structuraguard/tests/security/profiling/test_final_review_boundaries.py` | **17 passed** после fixes; до них **11 failed, 6 passed** |
| `uv run --locked --no-sync pytest packages/structuraguard/tests/docs packages/structuraguard/tests/unit/profiling packages/structuraguard/tests/contract/profiling packages/structuraguard/tests/property/profiling packages/structuraguard/tests/security/profiling packages/structuraguard/tests/integration/test_normalized_profiling.py -q` | Передача: **338 passed**, включая все docs examples и **227 M8 cases** |
| `make lint typecheck` | Передача: **296 files**, **293 source files**, без ошибок |
| `uv lock --check --offline` | Передача: exit 0 вне sandbox; lockfile не изменён |
| `make docs` | Передача: strict build, внутренние links/anchors; exit 0 |
| `.venv/bin/python scripts/benchmark_normalized_profiler.py --records 10000 --batch-size 100` | После fixes: **3.422 s** |
| `.venv/bin/python scripts/benchmark_normalized_profiler.py --records 50000 --batch-size 1000` | После fixes: **17.938 s**, рост **5.24×**, ledger 59379552 bytes, samples 488 bytes |
| `git status --short --branch`, `git diff --cached --name-only`, `git ls-files --others --exclude-standard`, `git diff --name-only`, `git diff --check` | Передача: состав сверён, staging пуст, whitespace errors нет |
| `git rev-list --left-right --count main...HEAD`, `git rev-list --left-right --count origin/main...HEAD` | Передача: обе команды **0 0** |
| Локальные Python проверки AST/matrix, hashes и hygiene для modified + untracked | **86 test references**, **43 файла**, source/tests/scripts не изменены на шаге передачи |

Для sandbox-compatible `uv run` использовался
`UV_CACHE_DIR=/private/tmp/structuraguard-m07-uv-cache`; tests/checks не отключались.
Первый offline lock check внутри sandbox завершился panic uv в macOS
SystemConfiguration (exit 101); повтор той же проверки вне sandbox прошёл.
Полным gates нужны существующие local document workers и loopback servers;
offline build использует основной cache. Логи не входят в commit:
`/private/tmp/structuraguard-m08-review-fix-{gates,green,narrow,docs}.log`,
`/private/tmp/structuraguard-m08-handoff-{tests,static,lock,lock-retry,docs}.log`.

### Пропущенные проверки и residual risks

- Полный suite/build не дублировались после edits передачи: runtime, tests,
  package manifests и dependencies не изменены относительно успешных gates;
  M8, examples, lint/typecheck, lock и docs проверены повторно.
- **84 PostgreSQL-specific cases** (`make test-database`) не запускались для M8:
  DB adapters, SQL, allowlist и транзакции не менялись. Старые прогоны M7 не
  выдаются за проверку точного M8 commit; CI PR ещё предстоит проверить.
- Другие Python/OS и remote CI не запускались локально; M8 commit/PR ещё нет.
  Live fetch/remote freshness и доступность внешних documentation URLs не проверены
  по сети; canonical anchors сверены с локальным ТЗ.
- Paid/production LLM, DNS/HTTP lookup распознаваемых данных не вызывались:
  profiler не имеет этих полномочий, integration использует FakeLLMProvider.
- Tracemalloc, distinct и long-Unicode benchmarks выполнены на предыдущем этапе
  [аудита](M08_acceptance.md), после последних fixes повторён small 10k/50k.
  Исчерпывающая комбинация budgets/Unicode и adversarial распределений не доказана.
- PII/identity — эвристики, KMV после overflow — estimate. Полный профиль
  чувствителен; safe summary не даёт egress/import approval. Content hash не
  доказывает authenticity и не заменяет lineage/policy для idempotency.
- Exact IDs ограничивают размер run; cooperative timeout и ledger не являются
  process CPU/RSS isolation. Benchmark подтверждает одну машину, не SLA.

### Рекомендуемые тексты для ручной публикации

Conventional Commit и PR title:
`feat(profiling): добавить Normalized Data Profiler M8`

Краткое PR body:

> Добавляет async-профилирование NormalizedBatch после semantic parsing:
> инкрементальные статистики, bounded samples/KMV, явную RU/EN locale policy
> и ranked/ambiguous types. PII evidence и safe summaries отделены от полного
> профиля; versioned content fingerprint сохраняет совместимость старых DTO.
>
> Проверено: 2717 tests, 22 integration, 599 security; lint/typecheck, strict docs
> и offline build. 50000 records — 17.938 s. 84 PostgreSQL cases исключены:
> DB adapters не менялись. PII/KMV остаются эвристиками, timeout — cooperative.

## История реализации и аудитов

Последующая [проверка критериев приёмки](M08_acceptance.md) добавила 97 cases
и исправила распознавание PII в отдельных context labels. Матрица и результаты
этого аудита находятся в отдельном отчёте; первичные проверки ниже исторические.

[Security review M8](M08_security_review.md) добавил 15 cases и исправил
4 Medium: phone regex до length cap, неограниченные Decimal thresholds,
UTC overflow и подменённый тип schema_version. Thresholds теперь имеют
hard cap 4096 coefficient digits / absolute exponent; defaults не изменены.

## Исправления финального review {#m08-final-review-fix}

Закрыты четыре Medium последнего review без изменения API, scope или dependencies.

| Finding / критерий | Исправление и regression |
| --- | --- |
| Неверные offset minutes → ложный datetime, K3/K5 | Grammar проверяет диапазон `±HH:MM` до `fromisoformat()`. `unit/profiling/test_final_review.py::test_invalid_offset_does_not_produce_datetime_evidence`; валидные границы и UTC oracle — `::test_valid_offset_boundary_preserves_utc_evidence` |
| Malformed domain → ложный natural key, K5 | Email pattern проверяет DNS-метки существующим локальным host validator; широкий PII-сигнал сохраняется. `unit/profiling/test_final_review.py::test_malformed_email_domain_cannot_be_a_natural_key`; positive/boundary — `::test_valid_email_domain_retains_natural_key_evidence` |
| Forged manifest → AttributeError/TypeError, K7 | Preflight manifest и доступ к batches помещены внутрь существующей typed error boundary. `security/profiling/test_final_review_boundaries.py::test_forged_manifest_returns_safe_typed_error_and_closes` проверяет code/reason, отсутствие canary и cleanup |
| JSON результата больше cap, K4 | Ранний budget учитывает разделители; полный canonical DTO проверяется перед публикацией, затем проверяется deadline. `security/profiling/test_final_review_boundaries.py::test_full_serialized_profile_is_rejected_above_output_cap` проверяет 4096 relationships, ASCII/Unicode, отказ/cleanup и успешный профиль с достаточным бюджетом |

Первый regression-прогон: **11 failed, 6 passed**. После исправления:
**17 passed**, полный узкий M8 suite — **227 passed**. Lint: **296 files**;
typecheck: **293 source files**, без ошибок. Команды новых tests:

```bash
uv run --locked --no-sync pytest -q \
  packages/structuraguard/tests/unit/profiling/test_final_review.py \
  packages/structuraguard/tests/security/profiling/test_final_review_boundaries.py
```

Повторный review ограничен этими тремя production-файлами и regression tests.
Проверены сохраняемые counts/content hash, отсутствие понижения PII, typed errors
и cleanup, bounded сериализация и отсутствие новых внешних вызовов.
Новых существенных findings в исправлениях не обнаружено.

Полные gates `make lint typecheck test test-integration test-security test-build`
прошли на Python 3.12.9: **2717 tests**, **22 integration**, **599 security**,
offline wheel/sdist verification — OK. **84 PostgreSQL cases** исключены:
DB adapters не менялись. Сохранились 5 прежних SWIG warnings; реальные LLM API,
другие Python/OS и remote CI не проверялись. `make docs` прошёл в strict mode.
Логи этого исправления: `/private/tmp/structuraguard-m08-review-fix-{red,green,narrow,static,gates,docs}.log`.

Benchmark после исправлений на той же macOS/Python, без tracemalloc:
`python scripts/benchmark_normalized_profiler.py --records 10000 --batch-size 100`
— **3.422 s**, `--records 50000 --batch-size 1000` — **17.938 s**, рост **5.24×**.
Retained ledger для 50000 — **59379552 bytes**, samples — **488 bytes**;
default deadline 30 s и плановый порог роста 7× соблюдены. Замер не является SLA.

## Реализация и уточнения

Добавлены `contracts/profiling.py`, `ports/profiling.py`, PII port в
`ports/security.py`, `profiling/` и `domain/normalized_fingerprint.py`.
Интерфейс принимает normalized schema 1.1.0/1.2.0 после semantic parsing,
поддерживает явную RU/EN locale policy, bounded incremental statistics,
ranked/ambiguous inference, safe summaries и versioned content fingerprint.

[Руководство и ограничения](../normalized-profiling.md),
[принятый ADR 0018](../adr/0018-bounded-normalized-profiling.md).
Исходный план ниже сохранён для трассировки; будущие формулировки в нём описывают
первоначальную последовательность работ. Фактический default preflight batch
budget **8 MiB** вместо предложенных 4 MiB: JSON batch из 1000 records × 2 fields
занимает 1250085 bytes, консервативная оценка — 5814688 bytes. Hard cap 16 MiB
и общий retained budget 64 MiB сохранены. Distinct/samples собраны в `_bounded.py`,
field statistics/inference/identity — в `_statistics.py`; candidate extrema имеют
собственный count. Safe summary намеренно исключает также labels/extrema/hashes.
SPEC_INDEX исправлен для M8; старые DTO и fingerprints не переопределены.

Покрытие реализации: `tests/unit/profiling/`, `tests/property/profiling/`,
`tests/security/profiling/`, `tests/contract/profiling/`,
`tests/integration/test_normalized_profiling.py`, `tests/docs/test_m08_examples.py`.
Updated distribution manifest и port-export assertions проверяют новый API.

Benchmark macOS 26.1 arm64, Python 3.12.9 (полный ленивый source + profiler):

| Данные / batch | Время | Retained ledger | Samples | Peak tracemalloc |
| --- | --- | --- | --- | --- |
| 10000 records, 2 fields / 100 | 3.507 s | 12121824 bytes | 480 bytes | не включён |
| 50000 records, 2 fields / 1000 | 18.934 s | 59379552 bytes | 488 bytes | не включён |
| 10000 records, 2 fields / 100, instrumentation | 20.757 s | 12121824 bytes | 480 bytes | 9164587 bytes |
| 1000 records, long Unicode / 100 | 0.487 s | 1508072 bytes | 239 bytes | не включён |

Рост времени при 5× records — 5.40×, default deadline 30 s соблюдён.
Tracemalloc измеряет отдельный инструментированный прогон с deadline 120 s;
его timing не используется как regression threshold. Логический source из
10¹² records в test останавливается после генерации 40 records при max_ids=100.

Review: исправлены некорректные currency placement, URL host validation,
declared pattern/type conflict, typed preflight failure для forged manifest,
safe cleanup boundary и classification floor для внешнего PII adapter;
каждое исправление закреплено regression test. Новые production dependencies,
сетевые вызовы и изменения DB adapters отсутствуют.



### Проверка первичной реализации до аудита приёмки

Свежие проверки 2026-09-11, Python 3.12.9, macOS 26.1 arm64:

| Проверка | Результат |
| --- | --- |
| Узкий M8 suite: unit/property/security/contract/integration/docs | **97 passed** |
| `make lint` | **290 files formatted**, Ruff passed |
| `make typecheck` | **287 source files**, passed |
| `make test` | **2587 passed**, 84 database cases deselected, 5 прежних SWIG warnings |
| `make test-integration` | **22 passed**, 2649 deselected, 5 прежних SWIG warnings |
| `make test-security` | **516 passed** |
| `make docs` | Strict build passed |
| `make test-build` | Wheel/sdist, offline install/import/examples: **distribution verification OK** |
| Diff/новые файлы | Whitespace и отсутствие generated/debug artifacts проверены |

Финальная команда: `make lint typecheck test test-integration test-security docs test-build`.
Gates запускались вне sandbox: внутри него document memory watchdog, loopback
server и macOS uv system configuration недоступны. Security controls и tests
не отключались. В отдельном cache M7 отсутствовал locked pydantic-core wheel;
основной uv cache позволил выполнить offline distribution verification.
Dependency/lock changes не потребовались. Logs находятся в
`/private/tmp/structuraguard-m08-complete-gates.log` и
`/private/tmp/structuraguard-m08-acceptance.log`, в repository не включены.

Review через `structuraguard-review` и `structuraguard-security` завершён:
незакрытых подтверждённых существенных findings не осталось в проверенном scope.
Исправленные Medium: permissive classification внешнего PII adapter и raw error
из нарушающего contract синхронного `aclose`. Исправленные correctness/error-path
issues: currency placement, URL host, declared pattern conflict, forged Unicode
в batch/manifest. Все закреплены tests; final full suite включает исправления.

Остаточные ограничения: PII/identity — эвристики, полный профиль чувствителен;
KMV — estimate после overflow; exact global IDs ограничивают размер run;
cooperative timeout/ledger не заменяют process isolation. PostgreSQL-specific
suite не перезапускался (DB adapters не менялись); остальные версии Python/OS
в этом milestone прогоне не проверялись. Commit/push/PR не выполнялись.

## Цель

За один ограниченный по ресурсам проход завершённого `NormalizedBatch` stream
построить воспроизводимый профиль каждого `SemanticFieldRef`: статистики, типовые
гипотезы, patterns, identity hints, PII evidence и стабильный отпечаток содержимого,
с явным различием точных, оценочных и недоступных результатов.

### Требования и исходное поведение

Из ТЗ извлечены **только FR-013, 11.4–11.6, 12 и M8** командой
`python3 scripts/extract_spec_sections.py FR-013 11.4 11.5 11.6 12 M8`.
FR-013 задаёт поля профиля; 11.4–11.5 — типовую совместимость и value patterns;
11.6 — структурный контекст; 12 — evidence и penalties для будущего mapper;
M8 — границу milestone. Строка «LLM layer … M8» в
[SPEC_INDEX](../codex/SPEC_INDEX.md) устарела: следуем текущей задаче и заголовку
M8 в ТЗ. Исправление всего индекса не входит в эту работу.

Изучены `contracts/normalized.py`, tagged scalars в `contracts/common.py`,
`contracts/execution.py`, `ports/semantic.py`, `ports/security.py`, security DTO
в `contracts/reports.py`, canonical helpers, соседний `structure/profiling.py`
и его stream checker; lifecycle tests `test_semantic_session.py` и property tests
`test_profile_properties.py`. Здесь и далее пути Python относительно
`packages/structuraguard/src/structuraguard/`, тестов —
`packages/structuraguard/tests/`.

- Сейчас M5 профилирует физический `ExtractedBatch`. Нормализованного profiler
  и его port нет; physical samples и `StructureProfile` не переиспользуем как
  профиль бизнес-полей. Сохраняем границы
  [ADR 0003](../adr/0003-two-stage-parsing-contracts.md) и
  [ADR 0008](../adr/0008-bounded-structural-profiling.md).
- `NormalizedBatch.records → NormalizedRecord.entities → SemanticEntity.values`:
  в записи бывает несколько сущностей, а одинаковое `field_name` у разных
  `entity_type` означает разные поля. Внутри entity поле встречается один раз,
  но разные occurrences одной entity type могут иметь разный набор полей.
- `NormalizedValue` уже содержит `normalized_value.kind`, заявленный
  `semantic_type`, `raw_value`, transformations/issues и provenance. Деньги
  с `semantic_type="money"` уже обязаны иметь `DecimalScalar`. Профилирование
  проверяет evidence и предполагает совместимость, но не меняет эти значения.
- Entity parents находятся внутри record; `parent_record_id` и
  `related_record_ids` должны разрешаться внутри batch. Отсутствуют отдельные
  `source_names` и полный контекст исходного документа. `origins.location`
  иногда даёт sheet/path/page, но physical refs сами по себе не дают заголовок.
- Schema 1.1.0/1.2.0 проверяет payload hashes; legacy 1.0.0 такой гарантии не даёт.
  Manifest доступен только в terminal batch и содержит summaries всего потока.
  `validate_batches()` проверяет глобальную уникальность record/entity/value IDs,
  но её sets растут с входом и собственного бюджета не имеют.
- По [semantic parsing contract](../semantic-parsing.md) и
  [ADR 0014](../adr/0014-hybrid-semantic-parsing.md), non-terminal output — preview.
  `NEEDS_REVIEW`, ошибка и отмена не дают completed dataset. Profiler не превращает
  такой preview в подтверждённый профиль.

После M8 появится отдельный async profiler без DB/LLM/source handles, возвращающий
результат только после проверки terminal manifest, EOF и cleanup. Реализация M5/M6
и существующая семантика fingerprints останутся совместимыми.

## Критерии приёмки

1. Все поля завершённого stream представлены по `(entity_type, field_name)`;
   учитываются повторные entities, поздно появившиеся поля и schema-only поля.
   Пустой terminal dataset даёт пустой профиль, пустой iterable — typed error.
2. Counts, null ratios, доступные extrema и длины вычисляются полным проходом;
   sampling не влияет на них. Для distinct явно указаны exact/estimated режим,
   алгоритм и размер sketch. Нулевые знаменатели дают `None`, не NaN или ложный 0.
3. Неоднозначные даты, separators, identifiers и смешанные scalar kinds сохраняют
   альтернативы и причины. Нет молчаливой конверсии, угадывания валюты/часового пояса
   и использования LLM confidence как доказательства.
4. Examples, context, distinct state, IDs, summaries, scalar sizes и output имеют
   конечные независимые и совокупные budgets. Любое сокращение evidence отражено
   в coverage; нарушение обязательной проверки завершает вызов ошибкой.
5. Все восемь запрошенных pattern families, boolean/integer ID, categorical/free
   text hints и identity evidence имеют положительные и отрицательные fixtures.
   PII-классификация не выдаёт security approval и не понижает входной класс данных.
6. Стабильный content fingerprint не зависит от допустимого re-batching, новых
   run IDs, sampling seed, времени и redaction. Изменение значения, типа, схемы
   или связи меняет его; source/parse-plan lineage сохраняется отдельно.
7. Ошибки потока, подмена DTO через `model_copy`/`model_construct`, duplicate IDs,
   cancellation, deadline и cleanup failures не публикуют профиль или полный
   content fingerprint; obtained iterator закрывается, внешние ресурсы — у caller.

## Затронутые контракты

### API и границы

- Добавить `contracts/profiling.py`: frozen DTO `NormalizedProfilingOptions`,
  `NormalizedProfileContext`, `NormalizedDataProfile`, `NormalizedFieldProfile`,
  coverage, typed estimates, type/pattern/identity evidence и safe issues.
  Новый wire contract начинается с schema 1.0.0, содержит `ProducerMetadata`,
  версии алгоритмов, options/context fingerprints и обязательную lineage.
- Добавить `ports/profiling.py`: `NormalizedDataProfiler.profile` принимает один
  terminal batch **с index 0** либо `AsyncIterable[NormalizedBatch]`, а также
  keyword-only context; возвращает `NormalizedDataProfile`. Последний batch
  многочастного dataset сам по себе недостаточен. Options/classifier/timer —
  явные зависимости concrete `profiling.NormalizedDataProfiler`, state run-local.
- `NormalizedProfileContext` задаёт минимум `DataClassification` и ограниченные
  необязательные source names/structural labels по `SemanticFieldRef`, связанные
  с source/extraction/parse-plan fingerprints. Если labels заданы, lineage
  обязательна. Все labels недоверенные; binding не доказывает истинность текста.
- Добавить PII DTO и отдельный `PIIClassifier` в `ports/security.py`; существующий
  `SecurityScanner.scan(SecurityScanRequest) → SecurityReport` не менять.
  Подробная граница классификации определена ниже.
- Экспортировать DTO/ports через соответствующие `__init__.py`, concrete profiler
  — через новый `profiling/__init__.py`. Корневая SDK facade и sync API вне M8.
  Реализация зависит от contracts/domain/ports; domain не импортирует profiler,
  infrastructure, parsers, DB или LLM. Новые production dependencies не нужны.
- Новый `NormalizedProfilingError` в `exceptions.py`: стабильные codes
  `NORMALIZED_PROFILE_INVALID_STREAM`, `NORMALIZED_PROFILE_UNSUPPORTED_SCHEMA`,
  `NORMALIZED_PROFILE_LIMIT_EXCEEDED`, `NORMALIZED_PROFILE_TIMEOUT`,
  `NORMALIZED_PROFILE_CLASSIFICATION_FAILED`, `NORMALIZED_PROFILE_CLEANUP_FAILED`.
  Reasons/resource names закрыты; exception details не содержат значений/labels.
  Cancellation сохраняет `asyncio.CancelledError` после bounded cleanup.

Существующие `NormalizedBatch`, `SemanticEntity`, manifest, ParsePlan и security
approval wire formats не меняются; миграции сохранённых данных нет. M8 принимает
проверяемые normalized schema 1.1.0/1.2.0; legacy 1.0.0 отклоняет явной ошибкой,
не называя непроверенный input завершённым. Это ограничение нового API, а не
изменение чтения legacy DTO другими компонентами.

### Проверка stream и завершение

До deep validation/serialization — итеративный preflight вложенных items, строк,
числовых разрядов, bytes, depths и terminal manifest. Затем повторная validation
batch payload, последовательного index от 0, lineage/version/producer, локальных
связей и schema. Накопить bounded summaries, фактические counts, множество schema
refs/types и **точные** sets глобальных record/entity/value IDs с общим лимитом.
Нельзя заменять проверку IDs вероятностным distinct sketch.

При terminal сравнить все summaries/counts и observed schema с manifest, проверить
его canonical hash один раз; заявленные поля без occurrences тоже профилировать.
Не вызывать публичный `manifest.validate_batch()` для каждого batch с повторным
hash всего manifest и не собирать поток для `validate_batches()`: это даёт O(B²)
работу либо удержание входа. Реализовать bounded checker для normalized модели,
переиспользуя её публичные DTO/canonical primitives, без импорта private M5 checker
и без преждевременного общего framework. Далее проверить EOF и закрыть iterator.
Данные после terminal, неполный stream или failure cleanup запрещают результат.

## Решения по статистике и evidence

### Online statistics и знаменатели

Для каждого entity type считать `entity_count`; для каждого поля — `present_count`,
`explicit_null_count`, `non_null_count`, частоты закрытых scalar kinds. В финале
`missing_count = entity_count - present_count`,
`null_count = missing_count + explicit_null_count`,
`null_ratio = null_count / entity_count`. Число значений FR-013 — `present_count`,
а число возможных позиций — `entity_count`; оба сохранены. Это позволяет учесть
поле, впервые встретившееся в конце, без O(entities × fields) обхода пропусков.

`NullScalar` — null; пустая строка, whitespace, `"null"`, `"N/A"` — строки.
Смена null markers относится к normalization, а не к profiler. Для нулевого
entity_count/null-only поля undefined метрики равны `None` с reason. Значение
`unique_ratio` считается среди **non-null** occurrences: distinct/non_null_count.
Ratios — детерминированные Decimal с precision 28, ROUND_HALF_EVEN и scale 6,
независимые от глобального decimal context; counts — ограниченные целые.
Identity/categorical decisions сравнивают исходные counts/рациональные отношения,
а не округлённые ratios: отображаемое 1.000000 не доказывает exact uniqueness.

Для distinct использовать KMV: удерживать K минимальных уникальных 128-bit
digest canonical tagged values, bounded set + max-heap. Пока не было более K
различных digest, count exact в рамках принятой hash collision assumption.
После первого overflow режим навсегда `estimated`: оценка `(K−1)/u_K`, где
`u_K = (integer_digest + 1)/(2^128 + 1)` для наибольшего удержанного digest.
Ограничить оценку диапазоном `[K+1, non_null_count]`; ratio также пометить estimate.
Persist только count/ratio, K, режим, `kmv128_v1` и overflow, не сами digests.
Не представлять estimate как доказательство уникальности или гарантированный
confidence interval. На малых независимых fixtures сравнивать с точным oracle.

Варианты: полный set нарушает bounded цель; ratio выборки нельзя экстраполировать
на distinct всего поля; один lower bound лишает mapper полезного сигнала на больших
полях. KMV выбран для ограниченной оценки с ясными ограничениями. Его статистическая
модель предполагает равномерный hash; adversarial распределение не даёт гарантий.

Extrema вычислять online по совместимым семействам: integer/Decimal сравнивать
точно; binary float хранить отдельно; boolean не смешивать с integer, date — с
datetime. Неоднозначное поле содержит extrema по типам, а общий min/max — `None`.
Для строк min/max лексикографические по Unicode codepoints, без locale/casefold;
они не означают лексикографический порядок чисел или времени. Выведенные из строк
числа/даты дают отдельные candidate metrics и не подменяют исходные extrema.
Не сравнивать суммы разных валют; currency ambiguity фиксировать отдельно.

Строковые `string_count`, `min_length`, `max_length`, `total_length` →
`mean_length = total_length / string_count`; длина в codepoints, bytes учитываются
отдельно для budgets. Пустая строка имеет длину 0; нет строк — длины `None`.
Extrema, имена и контекст могут содержать PII: профиль наследует classification,
не является автоматически безопасным payload для логов или LLM. Исключать из repr
все value-bearing поля и недоверенные labels/refs, а не только examples.

### Sampling strategy

Статистики, type/pattern evidence и PII pattern scanning используют весь допустимый
stream; выборка служит только bounded examples. Для каждого поля — bottom-k
occurrence sampling с приоритетом SHA-256 от version/seed/field ref/ordinal
не-null occurrence, без batch index, value ID и raw значения в seed. Tie-break —
ordinal; одинаковые значения разных occurrences имеют самостоятельный шанс.
Default k=8, воспроизводимый фиксированный seed в options; это не глобальный RNG.

Размер eligible example ≤512 UTF-8 bytes; более длинный sample пропускается целиком,
не обрезается. Отмечать seen/eligible/retained/skipped counts и reason
`example_too_large`; это выборка eligible occurrences, а не обещание полной
репрезентативности. 256 fields × 8 × 512 bytes дают общий cap 1 MiB. Output
сортировать по field ref и ordinal; dedup для показа допустим только с отдельными
counts, без изменения статистик. Prefix sampling отклонён из-за зависимости от
начала файла; sampling distinct values дал бы другой смысл частот.

Examples по умолчанию маскированы с typed reason; `omit` отключает удержание raw
samples. Явный режим `local_raw` разрешён только policy для PUBLIC/INTERNAL,
при полном классификационном coverage и отсутствии PII findings; default
classification INTERNAL и отсутствие находок сами по себе такой режим не включают.
Усиление classification в конце stream применяется ко **всем** examples поля,
включая ранее выбранные. Raw samples не попадают в repr, errors, audit и fingerprints
публичных diagnostics; ссылки и labels тоже считаются недоверенными данными.

### Type inference, ambiguity и patterns

Разделить `observed_kinds`, `declared_semantic_type`, `inferred_type`, candidates
с support/checked/invalid/unknown counts и `ambiguity_reasons`. Однородный tagged
тип надёжен как представление, но `string` может быть совместим с date/numeric/INN.
Никаких casts обратно в dataset. Leading zeros, integer-like ИНН/телефон и UUID
сохраняют string/identifier смысл. Binary float не становится денежным Decimal.

Закрытые recognizers выполняются на каждой строке до 4096 UTF-8 bytes. Более
длинные строки входят в counts/extrema/length/fingerprint, но дают
`pattern_scan_skipped` и unknown evidence; отсутствие совпадения не равно проверке.
Произвольные regex, динамические plugins и исполнение содержимого не допускаются.
Использовать линейные ограниченные lexical checks и stdlib validators; никаких
DNS/HTTP/DB lookup. Несколько совпадений сохранять одновременно.

| Family / стабильный pattern code | Правило и отрицательные случаи |
| --- | --- |
| email / `email` | Ограниченная синтаксическая проверка адреса; явно задокументированный subset, без подтверждения существования mailbox. Несколько `@`, whitespace и malformed domain не принимать. |
| phone / `phone` | Допустимые separator/extension rules и ограниченное число цифр; `+` даёт международный candidate. Не назначать страну по умолчанию; голая последовательность цифр конфликтует с integer ID/INN. |
| UUID / `uuid` | Полная строка и стандартный UUID parser с проверкой допустимой формы; не substring match внутри текста. |
| URL / `url` | Полный absolute HTTP(S) URL с валидным host/port; `javascript:`, `data:` и relative URL не кандидаты этой family. Credentials/query остаются чувствительными данными; URL никогда не открывается. |
| date/datetime / `date`, `datetime` | Явный список ISO-форм и `DD.MM.YYYY`; реальные calendar dates. `01.09.2026` → date candidate. Slash dates сохраняют DMY/MDY ambiguity без locale; naive datetime не получает UTC автоматически. |
| money/currency / `money`, `currency` | Строгая grammar группировки, знака и decimal separator; `125 000,50` → точный Decimal candidate. Код/символ валюты отдельный signal; `$` и число без валюты не определяют currency. `1,234` сохраняет альтернативы, malformed grouping отвергается. |
| INN / `russian_inn_10`, `russian_inn_12` | Ровно 10/12 ASCII digits и checksum; leading zeros сохраняются. Нулевая последовательность и неверные контрольные цифры отклоняются. Checksum подтверждает форму, не регистрацию лица/организации. |
| boolean / `boolean` | Закрытый словарь true/false и согласованные локализованные пары. `0/1` также integer candidates, без принудительного bool. |
| integer ID / `integer_id` | Integral representation + names/uniqueness evidence; одно целое число не доказывает identifier. |
| categorical / `categorical` | Минимум 20 non-null, exact distinct ≤20 и ratio ≤0.2, с полным coverage; при estimated count только слабая гипотеза. Не хранить неограниченный frequency dictionary. |
| free text / `free_text` | String length/token-shape evidence при отсутствии доминирующего специфического pattern; не означает отсутствие PII. |

Число без валюты и без денежного semantic/name evidence даёт numeric candidate;
само наличие дробной части не устанавливает `money`. Recognizer денег сохраняет
отдельно lexical support и context support, чтобы избежать ложного назначения типа.

Heuristic `type_inference_v1`: сохранять все применимые кандидаты; dominant
строковая гипотеза требует ≥20 проверенных non-null occurrences, support ≥0.95,
отрыва ≥0.10 от конкурирующей несовместимой гипотезы и полного scan coverage.
В остальных случаях — `ambiguous`/`insufficient_evidence` либо обычный string;
точные однородные tagged kinds не требуют минимальной выборки. Все thresholds
конфигурируются и входят в options fingerprint. Declared type conflict, parse
issues, incompatible kinds, locale/currency ambiguity и skipped values дают
отдельные penalties/reasons, а не скрытый выбор победителя.

По разделу 12 M8 отдаёт воспроизводимые signals/support/coverage для будущего
`type_compatibility`, `value_pattern_match`, `structural_context` и ambiguity
penalty. Итоговый `mapping_score`, веса, DB relation score и пороги принятия
0.90/0.70 относятся к mapper; M8 не разрешает импорт и не отменяет hard veto.

### Структурный контекст и identity hints

Соседние поля получить из entity occurrences и финальной schema; ограничить
хранимые co-occurrence пары и отражать overflow. Считать повторяемость entity type
и типы parent/child связей. Leaf records без parents не означают отсутствие
связей с другими полями. Не строить попарные корреляции всех полей и FK discovery.

Source names, имя CSV, JSON parent key, XML parent element, HTML heading,
PDF section, sheet и document table сохранять как typed context labels:
`observed_location` из доступных origins либо `caller_supplied` из bound context.
Нет label — `unavailable`, без загрузки исходного файла и догадок по opaque IDs.
Несогласованный lineage/ref отклонять; labels не могут перебить value evidence.
Ограничивать и классифицировать names/labels точно так же, как другие строки.

`IdentityHint` содержит field ref, kind (`identifier`, `natural_key`, `code`),
name/pattern/type evidence, null/unique metrics и reasons. Для сильного кандидата
нужны ≥20 non-null, null_ratio=0, exact unique_ratio=1, подходящее имя или pattern
и отсутствие ambiguity/coverage gaps. Estimated unique ratio около 1 даёт только
`possible_identifier`; малое N — `insufficient_evidence`. Email/INN могут быть
natural key hints, а последовательность integer — identifier hint, но это не
доказательство PK/UNIQUE/FK. Composite keys, DB lookup и автоматический upsert вне M8.

### PII classification interfaces

`PIIClassifier.classify(PIIClassificationRequest) → PIIClassificationResult` —
async port для bounded агрегированного evidence поля: field ref, names/context,
pattern counts, coverage, минимальный класс и versioned input fingerprint.
Сам profiler выполняет full-pass recognizers; classifier не получает весь dataset,
raw examples, source/DB/network handles. Первая реализация локальная,
детерминированная, без LLM. Вызовы последовательны под общим deadline;
результат перепроверяется по ref/input fingerprint, enum и лимитам findings.

Result содержит закрытые категории (`email`, `phone`, `personal_tax_id`,
`person_name`, `address`, `financial`, `credential`), state
`detected`/`not_detected`/`unknown`, rule IDs и coverage, а также
`DataClassification`. Email/phone/INN-12 — PII evidence, INN-10 организации
сам по себе не personal tax ID. Names/address/financial signals могут быть
слабыми и остаются явно эвристическими. Pattern miss не удостоверяет отсутствие PII.

Локальная policy задаёт category→classification: персональные контакты/ИНН
не ниже CONFIDENTIAL, credentials не ниже RESTRICTED; новые findings только
повышают минимум. Field/dataset classification — max по явному порядку
PUBLIC < INTERNAL < CONFIDENTIAL < RESTRICTED и минимуму caller. Пропущенные
длинные строки, неизвестная категория или недостаточное evidence оставляют
unknown; raw examples при этом закрыты. Нарушение classifier contract/timeout
даёт typed failure, не permissive fallback.

Этот интерфейс готовит evidence, но **не заменяет** `SecurityScanner`,
`SecurityReport` или `SecurityApproval`. Перед внешним egress caller по-прежнему
создаёт существующий fingerprint-bound `SecurityScanRequest` для точного payload
и routing/redaction policy. Генерация approval и production DLP вне M8.

### Stable normalized-data fingerprint

Существующий `manifest.normalized_fingerprint` сохранить как
`normalized_manifest_fingerprint` в новом профиле: он связывает source, run,
producer, summaries и batch boundaries. Не переопределять значение старого поля
и не подменять им semantic-content identity.

Добавить `normalized_data_fingerprint` с алгоритмом `normalized_content_v1`:
SHA-256 потока length-prefixed canonical frames с domain/version prefix.
Включить ordered records, ordered entity occurrences, `entity_type`, поля
отсортированные по `field_name`, declared semantic type и **tagged normalized
scalar**. Null и отсутствующее поле различаются; порядок fields несущественен,
порядок records/entities существенен. Использовать существующее canonical
представление Decimal/UTC и finite floats; не strip/casefold/Unicode-normalize
строки. `Decimal("1.0")` и `Decimal("1.00")` эквивалентны; string `"1"`, integer 1,
Decimal 1 и bool true различаются. В конце добавить отсортированную semantic
schema, включая поля без occurrences, и фактические counts.

Entity parent кодировать ordinal внутри record; record parent/related references
— глобальными record ordinals, полученными из текущего bounded batch, сортируя
related ordinals как set. Re-batching допустим только с сохранением record order
и всех связанных record groups целиком: текущий contract запрещает cross-batch
links. Нельзя просто удалить ID и потерять topology. Полный dataset не сортировать
и не использовать commutative XOR/sum hash с потерей порядка/кратности.

Исключить raw values, origins/physical coordinates, run/record/entity/value IDs,
batch markers, producer timestamps, source/parse-plan fingerprints, inference,
samples и classification. Их lineage и версии остаются в профиле отдельно.
Идентичное normalized содержимое разных источников получает одинаковый content
hash, но разную lineage; стабильность обещана для одной версии алгоритма.
Hash не доказывает authenticity, не является анонимизацией и не служит сам по себе
ключом разрешения импорта/cache: downstream учитывает lineage и policy.

`profile_fingerprint` — третий hash, canonical fingerprint DTO результата без
самого поля fingerprint; включает content/manifest hashes, options, context,
classification и версии алгоритмов. Он может меняться при другом batching,
sampling или redaction. Весь content hash публикуется только при successful EOF;
hash prefix stream не выдаётся как completed fingerprint.

## Memory/performance limits

Предлагаемые defaults M8 ниже — критерии реализации, не текущие runtime defaults.
Options могут понижать caps; увеличение требует сохранения документированных
hard ceilings и совокупного бюджета. Исходный batch уже выделен caller: profiler
ограничивает собственную работу/копии и отказывает до глубокого обхода сверх cap.

| Ресурс | Default / hard ceiling | Поведение при исчерпании |
| --- | --- | --- |
| Batch payload / nested items / depth | 4 MiB / 16 MiB; 100000 / 200000; 64 / 128 | Fatal до deep validation/hash; учитываются raw/origins, не только normalized values |
| Scalar UTF-8 bytes / numeric digits / absolute decimal exponent | 64 KiB / 1 MiB; 1024 / 4096; 1024 / 4096 | Fatal, без превращения большого числа в строку до проверки его размера |
| Bytes на полную pattern scan одного значения | 4096 / 16384 | Skip scan с unknown coverage; длины/counts/hash всё равно учитываются |
| Batches / terminal manifest bytes | 10000 / 100000; 8 MiB / 16 MiB | Fatal; manifest проверяется отдельным бюджетом, не скрыто внутри sample |
| Global record+entity+value IDs | 250000 / 1000000; суммарные UTF-8 bytes 16 / 64 MiB | Fatal до вставки в exact sets; это явный предел объёма run |
| Entity types / semantic fields | 64 / 256; 256 / 1024 | Fatal, неизвестные поля нельзя silently drop |
| Distinct K на поле | 1024 / 4096, минимум 16 | Sketch замещает entries; exact→estimated виден в DTO |
| Examples на поле / размер example | 8 / 16; 512 / 1024 bytes | Skip oversized с coverage; произведение field cap × k × bytes проверять против общего cap |
| Общие raw examples bytes | 1 / 4 MiB | Options с несовместимым произведением budgets отклонять заранее |
| Patterns / type candidates на поле | Закрытый registry; до 32 / 64 evidence entries | Fatal при нарушении contract, без пользовательских regex |
| Names/context / co-occurrence pairs / diagnostics | 8 labels на поле по 256 bytes; 4096 pairs; 256 issues | Bounded retention с reason/count overflow; сохранять факт неполного evidence |
| Co-occurrence pair operations за run | 1000000 / 10000000 | Прекратить сбор pair evidence с coverage reason, продолжить обязательные статистики |
| Учтённое retained state / serialized profile | 64 / 128 MiB; 4 / 16 MiB | Fatal; shared budget может сработать раньше отдельных caps |
| Run deadline / cleanup | 30 / 120 s; 2 / 5 s | Typed timeout/failure; нет публикации результата |

Общая память: один bounded batch и validation copies + bounded terminal manifest
+ sets IDs/summaries + O(fields × (K + samples + fixed counters)) + context/output.
Это конечная память **с ограничением допустимого run**, не обещание exact ID
validation бесконечного stream с O(1) storage. Самый ранний cap имеет приоритет;
M8 может отклонить output, разрешённый более широкими `ParsePlanOptions` M5/M6.

Byte budget считает payload и консервативную стоимость Python containers/entries,
не только длины строк/digests; sketch предпочтительно хранит компактные bytes.
Это не hard RSS quota процесса. Не удерживать batches, values/origins вне текущего
batch или полные словари категорий. После проверки освобождать transient copies;
ограничивать финальную сериализацию до создания большого JSON.

Работа O(total input bytes + values × log K + per-entity field sorting + bounded
context work), без повторного hashing manifest на каждый batch. Co-occurrence
обход имеет отдельный лимит pair operations, после него только counts/coverage.
Cooperative checkpoints не реже 256 values и между batches/classifier calls;
синхронная validation ограничена batch cap. Timer внедряется для тестов, никаких
arbitrary sleep. Источник, не отдающий управление event loop, не получает обещания
жёсткого wall-clock deadline внутри процесса.

Benchmark на фиксированной машине: ленивые 10000/50000 records одного schema,
2 поля на entity (≤200000 IDs), batch sizes 100/1000, отдельные small/high-cardinality
и long-Unicode cases. Зафиксировать values/s, wall time, peak tracemalloc и
retained ledger; 50000-record fixture должен укладываться в default 30 s, рост
времени 10000→50000 — не более 7× на той же машине. Peak allocations сверять с
derived envelope для state + batch/manifest/output copies; не выдавать 64 MiB
ledger за 64 MiB RSS. Stable microbench с фиксированными options, без сетевых
вызовов; wall-time regression не делать flaky assertion обычного unit suite.

## Шаги

Все шаги ниже — будущая реализация. В каждом сначала тест наблюдаемого поведения,
затем минимальная реализация и проверка; scope текущей задачи — сохранить этот план.

1. **A — DTO, API и versioned semantics.** Тесты
   `unit/contracts/test_normalized_profile.py`, `contract/test_normalized_profiler.py`:
   serialization/round-trip, enums, ratios/coverage invariants, safe errors,
   budgets, unchanged M2 DTO. Затем `contracts/profiling.py`, exports,
   `ports/profiling.py`, `exceptions.py`. Зафиксировать долгоживущие решения о
   content/manifest hashes и bounded stream validation в новом
   `docs/adr/0018-bounded-normalized-profiling.md` до реализации B/D; это пока
   предложенное решение данного плана, не уже принятое ADR.
   Проверка: `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/contracts/test_normalized_profile.py packages/structuraguard/tests/contract/test_normalized_profiler.py`.
2. **B — завершённый stream и базовая статистика.** Тесты
   `unit/profiling/test_stream.py`, `test_statistics.py` на sparse/late fields,
   multi-entity records, empty terminal, подмену/duplicate IDs и manifest mismatch.
   Затем `profiling/profiling.py`, `_stream.py`, `_statistics.py`: preflight,
   exact counts, typed extrema/lengths, budgets, cleanup; tests cancellation в
   `security/profiling/test_stream_security.py` с controlled events/timer.
   Проверка: `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/profiling packages/structuraguard/tests/security/profiling/test_stream_security.py`.
3. **C — bounded distinct/examples.** Тесты `unit/profiling/test_distinct.py`,
   `test_sampling.py`: exact oracle до K, overflow transition, rare tail,
   oversized Unicode, seed replay, отсутствие удержания origins. Затем
   `profiling/_distinct.py`, `_samples.py`; sample coverage не влияет на counters.
   Проверка: `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/profiling/test_distinct.py packages/structuraguard/tests/unit/profiling/test_sampling.py`.
4. **D — типы, контекст, identity и content hash.** Тесты
   `unit/profiling/test_inference.py`, `test_patterns.py`, `test_context.py`,
   `test_identity.py`, `test_fingerprint.py` с эталонными vectors; затем
   `profiling/_inference.py`, `_patterns.py`, `_context.py`, `_identity.py`,
   `domain/normalized_fingerprint.py` на существующих canonical primitives.
   Проверка: `uv run --locked --no-sync pytest packages/structuraguard/tests/unit/profiling`.
5. **E — классификация и отсутствие утечек.** Общий suite
   `contract/test_pii_classifier.py`, локальные `unit/profiling/test_pii.py` и
   `security/profiling/test_pii_security.py`: минимум caller, unknown, late PII,
   forged result, budget/timeout, canaries в labels/extrema/errors/repr.
   Затем PII DTO в `contracts/profiling.py`, port `ports/security.py`, локальный
   `profiling/pii.py`; `tests/fakes/profiling.py` только для тестов.
   Проверка: `uv run --locked --no-sync pytest packages/structuraguard/tests/contract/test_pii_classifier.py packages/structuraguard/tests/unit/profiling/test_pii.py packages/structuraguard/tests/security/profiling`.
6. **F — свойства, интеграция, performance и документация.**
   `property/profiling/test_profile_properties.py`,
   `test_fingerprint_properties.py`, `test_budget_properties.py` по матрице ниже;
   `integration/test_normalized_profiling.py`: реальные CSV/JSON и документный
   M6 output, saved-plan replay, `records_per_batch` 1/1000, preview без terminal.
   Добавить `scripts/benchmark_normalized_profiler.py`,
   `docs/normalized-profiling.md`, runnable `tests/docs/test_m08_examples.py`,
   обновить `docs/public-api.md`, `docs/codex/SPEC_INDEX.md` и `mkdocs.yml` только
   для фактически реализованного M8. Проверка:
   `uv run --locked --no-sync pytest packages/structuraguard/tests/property/profiling packages/structuraguard/tests/integration/test_normalized_profiling.py packages/structuraguard/tests/docs/test_m08_examples.py`;
   benchmark — отдельным `uv run --locked --no-sync python scripts/benchmark_normalized_profiler.py`.

Перед завершением реализации milestone выполнить `$structuraguard-review` и
`$structuraguard-security`, затем `make lint`, `make typecheck`, `make test`,
`make test-integration`, `make test-security`, `make docs`. M8 не меняет DB adapter:
PostgreSQL suite нужен при фактическом затрагивании его контракта, а не для чистого
profiler. В отчёте записать свежие результаты, benchmark environment и реальные
skips/risks; не объявлять M8 завершённым по одному сохранённому плану.

### Property-based tests

| Свойство | Генерация и oracle |
| --- | --- |
| Counts/ratios | Bounded ragged entities, repeated entity types, late fields, null/empty strings; сравнение с простым точным offline oracle; present + missing = entity_count, non_null + null = entity_count, ratios в [0,1] либо None |
| Extrema/lengths | Tagged integers/Decimal/float/bool/date/UTC datetime, Unicode и null; точный oracle отдельно по семействам, средняя между min/max; смена глобального Decimal context не меняет результат |
| Distinct | Duplicate-heavy и all-distinct streams, K−1/K/K+1; exact small oracle, bounded sketch, estimates в допустимых пределах, duplicates не меняют distinct state; статистическую accuracy проверять отдельным фиксированным ensemble, без ложной гарантии ошибки на каждом Hypothesis примере |
| Sampling | Повтор seed/input воспроизводит samples; допустимое re-batching не меняет selection; ни один cap не превышен, oversized values не обрезаются, изменение k/seed не меняет full statistics/content hash |
| Fingerprint invariance | Valid re-batching с целыми record link groups, новые runtime IDs с корректным remapping refs, permutation fields/mapping keys, equivalent Decimal scales/UTC instants: content hash тот же, lineage/profile hash могут отличаться |
| Fingerprint sensitivity | Мутация kind/value, empty↔null↔missing, field/entity type, record order, declared schema или parent/related topology меняет golden content projection/hash; явные framing vectors исключают неоднозначную конкатенацию |
| Patterns/ambiguity | Проверяемые генераторы валидных дат/UUID/INN и отдельные невалидные fixtures; invalid grouping, leading zeros, DMY/MDY, смешанные валюты, 0/1; negative cases не получают необоснованный dominant type |
| Stream/lifecycle | Разрывы, повторы, перестановки batches, trailing batch после terminal, forged hashes/IDs, cancellation в каждой checkpoint фазе; либо один completed профиль, либо typed failure с закрытым iterator и без fingerprint |
| Security/budgets | Caps−1/caps/caps+1 по items/bytes/IDs/fields/manifest/digits/output; malicious labels/URLs и длинные строки; нет внешнего I/O, raw canary в logs/errors/repr; в default serialized examples только masked values |
| PII monotonicity | Повышение входного класса или добавление PII occurrence не понижает результат; поздняя находка закрывает старые samples; unknown не становится PUBLIC/approval, classifier result не может подменить field/input binding |

## Риски

- KMV и inference — оценки/эвристики. Hash collision и adversarial distributions
  не дают доказательства uniqueness; identity hints не заменяют DB constraints.
- Current DTO сохраняет manifest целиком, а точная глобальная проверка IDs требует
  памяти. Hard caps ограничивают допустимый dataset сильнее M5/M6; disk-backed
  validation/replay, parallel merging и distributed sketches отложены.
- Имена/структурный контекст доступны частично; caller-supplied labels и
  self-declared semantic type могут быть ошибочны. Coverage/provenance обязательны,
  никакого восстановления headers чтением source за спиной caller.
- PII recognizers не являются полноценным DLP. PII может находиться в free text,
  source names, extrema и fingerprints; отсутствие findings не разрешает egress.
  RFC-complete email/phone validation, NER, международные tax IDs и production
  scanner остаются последующими улучшениями.
- Stable hash требует зафиксированной canonical projection и версии; он
  чувствителен к порядку records/entities и поддерживает только допустимый
  re-batching. Unordered dataset equality, изменение старого manifest fingerprint
  и automatic cache/idempotency policy вне M8.
- Python accounting и cooperative timeout не дают process RSS/CPU isolation.
  Реальные budget defaults необходимо подтвердить benchmark до приёмки M8.
