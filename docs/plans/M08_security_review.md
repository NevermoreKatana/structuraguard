# M08 — Security review текущего diff

Дата: 2026-09-11. Scope: незакоммиченный M8 diff, включая новые файлы
`contracts/profiling.py`, `domain/normalized_fingerprint.py`, `ports/profiling.py`,
`profiling/`, exports/exception, tests, docs и изменения distribution whitelist.
Основание: [план M8](M08_normalized_data_profiler.md),
[аудит приёмки](M08_acceptance.md), применимые пункты security checklist.
Код существующих parser/DB/LLM adapters повторно не пересматривался и не менялся.

Найдены и исправлены **4 Medium**. Critical/High не подтверждены.
Добавлены **15 security cases**: до исправлений — **11 failed, 4 passed**,
после — **15 passed**. Неподтверждённые timing предположения не используются
в качестве regression assertions. Платные LLM и внешние сервисы не вызывались.
Этот отчёт сохраняет результаты отдельного этапа security review. Более поздние
исправления и окончательная локальная приёмка отражены в
[checklist M8](M08_normalized_data_profiler.md#checklist-commit-pr).

## Trust boundaries и активы

Недоверенные данные: normalized strings/scalars, names/labels/origins, manifest,
идентификаторы/схема, DTO после `model_copy`/`model_construct`, результат внешнего
PII adapter. Options могут приходить из конфигурации или запроса host-приложения;
их арифметическая стоимость также должна оставаться конечной.

Активы: доступность event loop/памяти host, корректность полного профиля,
PII/classification, отсутствие исполнения входных данных и утечек в logs/errors.
Функция принимает только NormalizedBatch/async iterable; возвращает полный
профиль после terminal, EOF и cleanup. PII port получает агрегаты без samples,
не выдаёт SecurityApproval и не получает source/DB/network handles.
Python-код подключённых adapters — доверенный код host, а не sandboxed input.

## Findings и исправления

Пути production-кода ниже относительны `packages/structuraguard/src/structuraguard/`.
Номера строк указывают место исправления в итоговом коде.
Регрессии находятся в
`packages/structuraguard/tests/security/profiling/test_m08_security_review.py`.

### M8-S01 — Medium: phone-regex выполнялся до ограничения длины

- **Место:** `profiling/_patterns.py:199`.
- **Путь эксплуатации:** значение из пробелов и завершающего `X` проходит общий
  pattern cap 4096 bytes. `_PHONE.fullmatch()` вызывался до проверки `len <= 64`.
  Пересекающиеся whitespace-квантификаторы дают квадратичный перебор на near-miss.
- **Влияние:** повторение таких значений блокирует event loop между cooperative
  checkpoints и задерживает deadline/cancellation. Это расход CPU, а не выполнение
  кода или выдача разрешения на импорт.
- **Минимальное исправление:** проверка 64-character phone grammar cap первой,
  до вызова regex. Семантика распознавания допустимых телефонов сохранена.
- **Regression:** `test_phone_length_limit_is_enforced_before_regex` заменяет regex
  наблюдаемой обёрткой, запрещает вызов на длинной строке и проверяет положительный
  телефон. До фикса падал на 4096-character input; после проходит без timing threshold.

Диагностический замер `scan_string(' ' * (N-1) + 'X', ...)` до исправления:
1024/2048/4096 chars — 0.005205/0.019479/0.077540 s на вызов. После исправления,
среднее 100 вызовов: 0.000027/0.000049/0.000097 s; 16384 chars — 0.000381 s.
Это локальное наблюдение, не SLA и не assertion обычного suite.

### M8-S02 — Medium: компактный Decimal threshold создавал огромный integer

- **Место:** `contracts/profiling.py:157`; потребитель —
  `profiling/_statistics.py` (`type_support.as_integer_ratio()`,
  `type_margin.as_integer_ratio()`).
- **Путь эксплуатации:** если host принимает options извне, значение
  `Decimal('1e-1000000000')` удовлетворяет диапазону `[0, 1]` и занимает мало
  памяти. Inference затем материализует denominator порядка `10**1000000000`.
- **Влияние:** потенциальные сотни MiB больших целых и длительная работа CPU
  вне учёта retained ledger; синхронная арифметика не прерывается async timeout.
  Огромный denominator в review намеренно не вычислялся: regression проверяет
  отказ при создании options до опасного вызова.
- **Минимальное исправление:** независимый hard cap для двух thresholds:
  coefficient ≤4096 digits и абсолютный stored exponent ≤4096. Проверка размера
  Decimal предшествует `as_tuple()`. Проверяются исходные digits/exponent без
  `normalize()`, зависящего от process Decimal context и допускающего underflow.
  Обычные defaults 0.95/0.10 и точность решений не изменены.
- **Regression:** `test_type_thresholds_reject_unbounded_decimal_exponents`
  и `test_type_threshold_precision_has_a_finite_boundary`, оба для support/margin.
  Граница 4096 принимается, 4097 и 10⁹ отвергаются. Все четыре cases были красными.

Совместимость: слишком длинные coefficient/exponent, ранее принимавшиеся DTO,
теперь дают ValueError при валидации options. Это явное ограничение unsafe config;
оно не меняет normalized content projection или допустимые scalar values.

### M8-S03 — Medium: UTC overflow выходил из границы распознавания даты

- **Место:** `profiling/_patterns.py:145`.
- **Путь эксплуатации:** валидная normalized строка
  `0001-01-01T00:00:00+01:00` либо `9999-12-31T23:59:59-01:00` проходит ISO grammar.
  Локальная дата существует, но её UTC instant выходит за диапазон datetime.
  Конструирование DateTimeScalar вызывало необработанный OverflowError.
- **Влияние:** атакующий мог оборвать profiling неожиданным исключением,
  минуя штатную обработку доменных ошибок у host. Профиль не публиковался;
  утечка PII этим payload не подтверждена.
- **Минимальное исправление:** включить создание UTC scalar в bounded recognizer
  try-block и обрабатывать ValueError/OverflowError как недопустимый candidate.
  Исходная строка остаётся в statistics; timezone не угадывается.
- **Regression:** `test_datetime_outside_utc_range_is_an_invalid_candidate` —
  две границы. Дополнительно `test_boundary_datetime_with_representable_utc_is_preserved`
  подтверждает, что даты 1/9999 года с допустимым UTC не отбрасываются целиком.

### M8-S04 — Medium: непроверенный schema_version хешировался до preflight

- **Место:** `profiling/_stream.py:147`.
- **Путь эксплуатации:** upstream adapter передаёт NormalizedBatch после
  `model_copy`/`model_construct` с list/dict/Decimal sNaN вместо schema_version.
  Membership в set поддерживаемых версий хешировал значение до contract validation.
- **Влияние:** TypeError вместо NormalizedProfilingError, обход ожидаемого
  typed error path. Для None также отсутствовало различение malformed contract
  и неподдерживаемой строковой версии. Профиль/полный hash не публиковались.
- **Минимальное исправление:** сначала проверить точный строковый тип; затем
  сравнить с коротким tuple поддерживаемых версий. Это также не хеширует
  произвольно длинную подменённую строку до byte preflight.
- **Regression:** `test_forged_schema_version_is_a_safe_typed_failure` — list,
  dict, None и sNaN дают `NORMALIZED_PROFILE_INVALID_STREAM / batch_contract`,
  obtained iterator закрывается.

## Матрица угроз

| Угроза | Применимость и наблюдаемый контроль |
| --- | --- |
| Attacker-controlled input | Применимо: bounded preflight перед deep validation, повторная проверка DTO/hash/schema, статические ошибки. Новые S03/S04 и существующие forged value/manifest/Unicode tests |
| Resource exhaustion | Применимо: scalar/items/depth/batch/manifest/IDs/fields/state/output caps; bounded KMV/samples/context; S01/S02 закрывают CPU/arithmetic gaps. Existing limit boundary и 10¹² logical-stream tests входят в M8 suite |
| Parser exploit / unsafe deserialization | Применимо к lexical recognizers и Pydantic boundary, исправлен S03. M8 не вызывает technical parsers, не десериализует YAML/Pickle и не исполняет Python tags |
| XXE / DTD / network access | XML/DTD остаются строками. Новый `test_xml_yaml_sql_prompt_and_paths_are_inert_normalized_strings` блокирует file/network/process I/O; существующий URL test проверяет отсутствие connect. `urlsplit`/IDNA/IPv6 — локальные проверки, без DNS |
| Prompt injection / excessive agency | Нет model/tool/SQL execution и egress authority. Новый inert-input test включает инструкции подключиться к БД и удалить таблицы; PII contract не передаёт raw samples/handles и не выдаёт approval |
| PII / secrets leakage | Default examples masked; LOCAL_RAW закрывается при findings/unknown/повышенном классе. Safe summary исключает values, labels, refs, extrema и fingerprints. Existing late-PII, classifier floor/binding, label regression и canary tests проходят в M8 suite |
| SQL / identifier injection | M8 не строит и не исполняет SQL, не импортирует DB adapter. Новый test сохраняет SQL payload в value и SQL-like field name как данные, без операций. Это не проверка query builder другого milestone |
| Обход DB allowlist / denylist | Нет DB access path и полномочий на загрузку, поэтому allowlist enforcement внутри M8 неприменим. DB adapters/loader в diff не менялись; их проверки не заменялись profiler-тестом |
| Schema drift | Применимо к normalized schema/lineage: конфликт semantic type между batches отвергается новым `test_declared_semantic_schema_drift_is_rejected_before_result`; source/plan/manifest checks существующие. DB schema drift вне M8 |
| Небезопасные logs / audit events | Profiler не настраивает logging и не пишет audit. Публичный безопасный путь — allowlist `safe_summary()`; repr скрыт, external failures sanitized. Canary assertions охватывают summary/repr/caplog и errors; полный DTO намеренно чувствителен |
| Path traversal / временные файлы | M8 не открывает source paths и не создаёт temp files. Новый test включает traversal, локальный canary path и file URI при запрещённом open. Изменения distribution script — только статический whitelist; tempfile/extraction logic там не менялась |
| Supply chain новых production dependencies | Новых dependencies нет: `pyproject.toml`, package pyproject и `uv.lock` без diff. Добавлены только stdlib-based recognizers и использование уже имеющегося Pydantic. Offline wheel/sdist verification проверяет состав пакета/imports |

Пропуск PII в отдельных context labels был исправлен предыдущим
[аудитом приёмки](M08_acceptance.md); здесь он не считается новым finding.

## Проверки

Среда: Python 3.12.9, macOS 26.1 arm64.

| Команда | Фактический результат |
| --- | --- |
| `uv run --locked --no-sync pytest -q packages/structuraguard/tests/security/profiling/test_m08_security_review.py` | До исправлений 11 failed / 4 passed; после **15 passed** |
| Узкий M8 suite (команда ниже) | **209 passed**, 6.32 s |
| `make lint` | **294 files formatted**, Ruff passed |
| `make typecheck` | **291 source files**, passed |
| `make test` | **2699 passed**, 84 database cases deselected, 5 прежних SWIG warnings; 87.43 s |
| `make test-integration` | **22 passed**, 2761 deselected, 5 прежних SWIG warnings; 5.01 s |
| `make test-security` | **595 passed**, 22.45 s |
| `make test-build` | Wheel/sdist, offline installation/import/examples: **distribution verification OK** |
| `make docs` | **Strict build passed** |
| `git diff --check` | Passed |

Полная последовательность `make lint typecheck test test-integration test-security test-build`
завершилась с exit code 0. Она запускалась вне sandbox: существующим document
workers и loopback test servers нужны недоступные внутри sandbox операции.
`make docs` выполнен отдельно после подготовки отчёта. Новые production dependencies,
сетевые egress calls и изменения approval/allowlist policy отсутствуют.

```bash
uv run --locked --no-sync pytest -q \
  packages/structuraguard/tests/unit/profiling \
  packages/structuraguard/tests/property/profiling \
  packages/structuraguard/tests/security/profiling \
  packages/structuraguard/tests/contract/profiling \
  packages/structuraguard/tests/integration/test_normalized_profiling.py \
  packages/structuraguard/tests/docs/test_m08_examples.py
```

Логи вне repository: `/private/tmp/structuraguard-m08-security-{red,green,narrow,gates,docs}.log`.
Gate settings, markers, type checking, security controls и существующие assertions
не ослаблялись. Нет несвязанного refactor и изменений parser/DB/LLM adapters.
После повторной проверки исправлений незакрытых подтверждённых Critical/High/Medium
findings в рассмотренном diff не осталось. Это ограниченный review перечисленных
границ, а не утверждение об отсутствии всех возможных дефектов SDK.

## Остаточные ограничения

- Python accounting и cooperative deadlines не равны process RSS/CPU isolation;
  этот port не изолирует недоверенный исполняемый Python adapter.
- PII/identity — ограниченные эвристики, не DLP/PK verification. Полный профиль
  содержит чувствительные labels/extrema; SHA-256 не анонимизирует данные и
  сам по себе не удостоверяет источник. Egress требует отдельного SecurityScanner.
- Не проверялись PostgreSQL-specific suite (DB-код вне diff), другие Python/OS,
  remote CI и реальные paid LLM. Integration использует fake/no-LLM paths.
- Не доказано отсутствие всех adversarial distributions/hash collisions;
  проверены границы памяти, integrity contracts и конкретные exploit paths.
