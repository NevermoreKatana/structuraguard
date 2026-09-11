# M09 — Deterministic DB Mapper

Статус на 2026-09-11: M9 реализован и прошёл локальную приёмку; подготовлен
к ручному commit и последующему Pull Request в `main`. Commit и PR не созданы.
Фактическое состояние приведено в [checklist передачи](#checklist-commit-pr),
покрытие — в [приёмке](M09_acceptance.md), публичный API —
в [руководстве](../deterministic-mapping.md). Алгоритмические разделы ниже
сохраняют утверждённый design и границы M9.

## Цель

По завершённому `NormalizedDataProfile` и разрешённому `DatabaseCatalog`
возвращать воспроизводимый top-k `MappingCandidate` для каждого semantic field,
с числовым объяснением, ограничениями и явной неоднозначностью, без embeddings,
LLM, сетевых запросов, чтения исходных файлов и доступа к DB connection.

### Основание и наблюдаемое поведение

Через [индекс](../codex/SPEC_INDEX.md) извлечены **только разделы 10–14 и M9**:
`python3 scripts/extract_spec_sections.py '10. Database Semantic Catalog'
'11. Механизм автоматического сопоставления' '12. Оценка достоверности'
'13. Определение сущностей и отношений' '14. Mapping Plan'
'M9. Deterministic DB Mapper'` — одна команда с шестью аргументами.

На этапе планирования индекс ошибочно относил Deterministic Mapper к M7,
а MappingPlan validator — к M9. Индекс исправлен: M9 и M11 соответственно.
Канонический scope — [M9 в ТЗ](https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m9-deterministic-db-mapper).
Пример LLM mapping из §11.9 и LLM-сигнал из
§12 не входят в реализацию. §13–14 задают границы будущей сборки и проверки плана,
а не требование реализовать loader в M9.

До M9 были доступны профиль M8, catalog-v1 и FK graph M7, а также
неавторизованный `MappingCandidate`. M9 добавляет отдельный асинхронный API
ранжирования, его port, Semantic Catalog и числовой breakdown. Существующие
facade, `MappingPlan`, profiler, inspection и wire format кандидата сохранены.

Обязательный scope: имена и aliases, совместимость, source/graph context,
ранжирование, calibration config и двуязычная проверка. Сборка entity mappings,
выбор insert/upsert, преобразование значений, FK lookup/resolution, независимый
MappingPlan validator и исполнение загрузки остаются последующими этапами.
Ни embeddings, ни LLM provider, ни автоматический вызов существующего semantic
mapper не допускаются даже при пустом результате или неоднозначности.

## Критерии приёмки

- Для каждого `SemanticFieldRef` есть результат: до `top_k` уникальных targets
  либо пустой список с reason code; поля разных entity types не смешиваются.
- Результат содержит исходные bindings, версии алгоритмов/config/catalog,
  score и его воспроизводимый breakdown. Одинаковые snapshots и options дают
  одинаковый порядок, решения и candidate IDs, независимо от hash seed,
  Decimal context, порядка перечисления неупорядоченных metadata и concurrency.
- Русские и английские имена, явные aliases и транслитерация покрыты отдельными
  positive/negative fixtures; близкие конкуренты сохраняют неоднозначность.
- Allowlist/denylist и structural writability применяются до scoring. Запрет,
  доказанная несовместимость и неполное FK evidence не обходятся весами.
- Отсутствующие labels, неизвестные типы, null-only и сокращённый evidence
  отражаются явно. Mapper не восстанавливает контекст через I/O и не меняет данные.
- Top-k вычисляется после контекстных сигналов; pruning не скрывает конкурента,
  необходимого для проверки margin. Превышение бюджета даёт typed error целиком.
- Высокий score означает рекомендацию кандидата. Результат не является
  `ValidatedMappingPlan`, `SecurityApproval` или разрешением на импорт.

## Затронутые контракты

### Что уже реализовано и используется повторно

Пути ниже относительны корню репозитория; `src` в шагах означает
`packages/structuraguard/src/structuraguard`, `tests` —
`packages/structuraguard/tests`.

| Контракт / реализация | Использование и ограничение |
|---|---|
| `src/contracts/profiling.py`: `NormalizedDataProfile`, `NormalizedFieldProfile` | `fields`, `inference`, `observed_kinds`, `patterns`, checked/skipped counts, lengths/extrema, `identity`, `categorical`, `labels`, `relationships`, `reasons`; inference и identity — evidence, не разрешение конверсии/PK |
| Там же: `ProfileLabel`, `FieldRelationship` | Имеются source name, file/sheet/parent/heading/section/table/path и origin; отношения `co_occurrence` / `parent_child` не доказывают FK; `context_available=False` нельзя заменять догадкой |
| `src/contracts/database.py`: `DatabaseCatalog`, `DatabaseType`, `CatalogColumnRef` | M7 schema 1.1.0 / `catalog-v1`, qualified names, canonical types, длина/precision/scale/timezone, domain/enum/array metadata, writable/generated/default/identity и ключи |
| `src/domain/database_graph.py`: `build_dependency_graph` | Готовые parent→child edges с ordered composite pairs, SCC/self-reference, join-table hints; использовать graph каталога, не создавать второй граф и не выбирать стратегию цикла |
| `src/domain/database_fingerprint.py` | `verify_database_fingerprint` проверяет содержимое переданного snapshot; не доказывает актуальность живой БД |
| `src/contracts/mapping.py`: `MappingCandidate` | `source: SemanticFieldRef`, `target: CatalogColumnRef`, `confidence: Decimal`, уникальные `evidence` codes, `issues`, полный lineage и target/policy binding; physical source refs в issues запрещены |
| `src/contracts/database.py`: `MappingPolicyRef` | Только ссылка на policy; не содержит allowlist, grants или правил scoring |
| `src/database/target.py`: inspection targets | Содержат trusted scope и connection metadata; не передавать их в mapper. Column selectors в M7 пока не поддержаны; M9 может сузить уже отражённый каталог своей policy |

Соблюдать [ADR 0017](../adr/0017-canonical-catalog-and-dependency-graph.md) и
[ADR 0018](../adr/0018-bounded-normalized-profiling.md). В частности, schema hash
не включает target/policy и derived graph; masked samples не делают весь профиль
безопасным для logs.

### Добавления M9

- `src/contracts/deterministic_mapping.py`: immutable `DeterministicMappingOptions`,
  `MappingScope`, `CandidateSignal`, `CandidateExplanation`, `FieldCandidates`,
  `DeterministicMappingResult`; `src/contracts/semantic_catalog.py`:
  `DatabaseSemanticCatalog` с типизированными entries. Не добавлять свободный
  `dict[str, Any]`, callable rules или исполняемые transformations.
- `src/ports/mapping.py`: protocol `CandidateMapper` с async методом `rank`,
  принимающим profile, catalog, обязательный scope и optional semantic catalog.
  `src/mapping/mapper.py`: `DeterministicMapper`, options явно в конструкторе.
  Чистые локальные helpers — `src/mapping/_names.py`, `_compatibility.py`,
  `_context.py`, `_ranking.py`; imports только contracts/domain, общий errors
  и нужный stdlib. Отдельного sync facade и registry в M9 не требуется.
- `MappingScope`: trusted caller передаёт `target_id`,
  `target_policy_fingerprint`, явный набор разрешённых `CatalogColumnRef` и
  deny refs. Deny имеет приоритет; пустой allowlist разрешает ноль колонок,
  неизвестная ссылка или несовпадение binding — ошибка. Состав scope получает
  собственный fingerprint; он не подменяет fingerprint inspection policy.
- `FieldCandidates` связывает ровно одно source field со списком кандидатов,
  объяснениями по `candidate_id`, решением `auto_candidate` / `review` /
  `rejected` / `unmapped`, gap и reason codes. Все числовые contributions,
  availability и blockers хранятся в новых explanations; старый candidate DTO
  не расширяется обязательными полями и не заменяется примером DTO из §11.8.
- Result хранит `profile_fingerprint`, `normalized_data_fingerprint`, fingerprints
  options/scope/semantic catalog, algorithm/normalization/dictionary versions,
  classification и безопасную сводку счётчиков. Полный result чувствителен:
  repr/logs/errors не должны выводить names, labels, extrema или aliases.
- В candidate `normalized_fingerprint` переносится именно
  `profile.normalized_manifest_fingerprint`: это текущий binding MappingPlan
  к manifest. `normalized_data_fingerprint` не подставляется вместо него.
  Остальные source/extraction/parse fingerprints копируются из profile,
  database/target/policy — из проверенного catalog/scope; producer фиксирован
  версией mapper, без текущего времени и случайного run ID.
- Ошибки через существующий `MappingError`: новые коды
  `MAPPING_INPUT_INVALID`, `MAPPING_UNSUPPORTED_SCHEMA`, `MAPPING_BINDING_MISMATCH`,
  `MAPPING_SEMANTIC_CATALOG_INVALID`, `MAPPING_LIMIT_EXCEEDED`;
  schema hash mismatch сохраняет существующий `DATABASE_SCHEMA_DRIFT`.
  Пустой список, конфликт и недостаток evidence — нормальные результаты.

Новый API принимает profile M8 schema 1.0.0 и catalog-v1 schema 1.1.0;
legacy catalog явно отклоняется только новым API. После bounded preflight
проверяются DTO invariants и hashes, включая объекты, созданные через
`model_construct`/`model_copy`; это не аутентификация передавшего их caller.
Миграции данных и production dependencies не нужны. Wire/result design и
границу «кандидат ≠ approval» закрепить ADR при реализации шага 1; этот документ
сам по себе не помечает предлагаемое решение как уже принятое.

## Шаги

### Алгоритм и правила реализации

#### 1. Нормализованные имена, transliteration и tokenization

- Сохранять оригинальные идентификаторы для ссылок. Matching-представления:
  raw, Unicode NFKC, split camelCase/acronyms до `casefold`, токены по whitespace,
  `_`, `-`, пунктуации и границам букв/цифр; snake_case и compact без разделителей.
  Пустое имя после нормализации не совпадает с другим пустым именем.
- `name_similarity` — максимум: raw equality `1.00`, нормализованное token
  equality `0.98`, compact equality `0.95`, weighted token Dice × `0.90`,
  транслитерированное equality × `0.80` / token Dice × `0.75`.
  Dice вычисляется по множествам: удвоенный вес пересечения / сумма весов;
  generic tokens `id`, `name`, `date`, `code`, `value`, `имя`, `дата`, `код`,
  `значение` имеют вес 1, остальные — 4. Словарь generic tokens версионируется.
- Проверять semantic field name и только labels `kind=source_name`;
  брать максимум, не суммировать дублирующие варианты. Sheet/file/parent labels
  используются отдельно в context. Не интерпретировать opaque IDs как пути.
- Собственная фиксированная таблица `ru_lat_v1` без зависимости от locale:
  а=a, б=b, в=v, г=g, д=d, е=e, ё=yo, ж=zh, з=z, и=i, й=y, к=k, л=l,
  м=m, н=n, о=o, п=p, р=r, с=s, т=t, у=u, ф=f, х=kh, ц=ts, ч=ch,
  ш=sh, щ=shch, ъ/ь опускаются, ы=y, э=e, ю=yu, я=ya.
  Вариант ё→е — только дополнительное сравнение с тем же cap `0.80`.
  Другие алфавиты не транслитерируются; смешанные Latin/Cyrillic confusables
  дают reason и запрещают auto recommendation на одном таком совпадении.
- Stemming, lemmatization, fuzzy edit distance и автоматический перевод отложены.
  `CUSTOMERNAME` и `customer_name` связывает compact form; омонимы и коллизии
  транслитерации остаются разными targets и участвуют в ambiguity.

#### 2. Aliases / Database Semantic Catalog

- Вход — готовый immutable DTO или ограниченный Python mapping, валидируемый
  тем же DTO; загрузка YAML/JSON-файла не входит в mapper. Catalog entries
  адресуют точные `(schema, table, column)`, разрешаются в existing IDs и
  привязаны к `database_fingerprint`, target и target policy.
- Поддержать table aliases/description, column aliases/description,
  `semantic_type`, `identity_keys` и ограничения `allowed_operations` из
  закрытого списка. Это metadata: M9 не выбирает операцию и не разрешает её.
  Description/comments сохраняются как недоверенный текст, но в M9 не
  преобразуются автоматически в aliases или команды.
- Встроенный небольшой `ru_en_aliases_v1` связывает concept tokens:
  Контрагент/Покупатель→customer, Общая сумма→total_amount,
  Дата создания→created_at, ИНН/Tax ID→inn. Внешний catalog конкретизирует,
  например, Контрагент→customers.legal_name; table concept customer сам по себе
  не означает колонку legal_name. Для expanded token comparison используется
  та же Dice-метрика; веса alias: exact scoped match `1.00`, нормализованный
  scoped match `0.98`, built-in concept match до `0.85`, transliteration до `0.75`.
- Alias — hint, не override: если один alias адресует customers и suppliers,
  сохранить обоих. Exact target-specific entry сильнее общего словаря,
  но не обходит type/scope. Повтор той же записи дедуплицировать канонически;
  противоречивые annotations одного target, неизвестные refs и устаревший
  binding отклонять. Ни нормализация, ни aliases не расширяют allowlist.
- `identity_keys` — только предложения natural keys. Сверять ordered columns
  с PK/безусловным UNIQUE и evidence M8; estimated uniqueness, nullable keys,
  partial unique и CHECK-текст не доказывают identity strategy.

#### 3. Type/value-pattern compatibility

Использовать готовые агрегаты M8; samples, seeds и маскирование не влияют на
scoring. Не распознавать значения заново и не читать DB rows. Каждый результат
compatibility различает `compatible`, `conditional`, `unknown`, `incompatible`.

| Evidence профиля → тип БД | Решение |
|---|---|
| Полностью наблюдаемый native scalar → то же семейство; integer→decimal | `compatible`, type score `1.00`, если известные ограничения не нарушены |
| String с resolved date/decimal/money/boolean/UUID pattern → соответствующий тип | `conditional`, до `0.75`, reason `TRANSFORMATION_REQUIRED`; mapper ничего не преобразует |
| Identifier/ИНН/phone с ведущими нулями → text | Совместим с text; преобразование identifier→numeric не предлагается, чтобы не потерять формат |
| Float↔decimal, date↔datetime, timezone mismatch, mixed kinds/locale/currency | `conditional` либо `unknown`, до `0.50`, явный blocker auto; не выбирать локаль за profiler |
| Null-only, unknown DB type, недоказанная enum membership / array shape / domain CHECK | `unknown`, score `0`, явный blocker auto; domain base type даёт только частичную оценку |
| Доказанный overflow длины/диапазона, заведомо несовместимый native kind | `incompatible`: убрать из кандидатов, учесть безопасным reason/count |

У nullable/default/generated/identity проверять отдельный смысл: generated и
non-writable запрещены до scoring; nullable не делает тип несовместимым.
Missing/null при NOT NULL, неполная проверка precision/scale, enum membership,
domain constraints или timezone запрещают auto recommendation до validation.
Default не устраняет explicit NULL автоматически. Extrema позволяют доказать
часть конфликтов, но не точную scale всех значений; SQLite affinity не заменяет
declared type. Консервативное `unknown` предпочтительнее выдуманной гарантии.

`value_pattern_match`: ожидаемые patterns берутся из явного semantic_type,
закрытого alias/concept словаря или canonical DB type. Совпадение — максимальная
доля подходящего pattern среди **всех non-null occurrences**, а не только
checked; нулевой знаменатель даёт unavailable. Неполный scan отмечается отдельно.
Email/phone/URL/INN требуют соответствующего semantic hint у text target,
произвольный VARCHAR не получает за них бонус. Money/currency, UUID, date,
datetime, boolean, integer ID, categorical и free text учитываются раздельно;
categorical берётся из флага профиля, identity — из `IdentityHint`.
Конфликт ожидаемого semantic pattern с полным evidence блокирует auto.

#### 4. Source structural context и DB graph context

- Сначала найти anchors по lexical/type evidence **без** context/graph:
  native/normalized name или scoped alias ≥ `0.95`, type compatible,
  единственный лучший target с lexical margin ≥ `0.10`, без blocker.
  Transliteration и общий `id`/`name`/`дата` сами по себе anchor не создают.
- `structural_context` — максимум similarity context label→table name/alias
  и доли подтверждённых соседей, anchored в эту таблицу. Соседи — явные
  co-occurrence relationships внутри semantic entity type; denominator — все
  известные соседи, включая неуверенные. Текущее поле исключается из anchors.
  Имя `entity_type` также служит context label, кроме generic `row`/`entity`.
  File name используется без расширения, path разбивается только лексически;
  file/sheet/json/xml/html/pdf/document labels не требуют чтения источника.
- Отсутствие labels/relationships даёт unavailable. `pair_limit`/`context_limit`
  сохраняются как неполнота; частичный context не используется как отрицательное
  доказательство. Repeated entities разделяются по `entity_type`, а не по record;
  одна исходная entity может иметь кандидатов в нескольких таблицах (§13).
- `database_relation_score` — средняя поддержка известных parent_child
  relationships поля: `0.50` при anchored таблице другого конца и FK между
  таблицами в нужном направлении, `1.00` при полном column-pair evidence,
  иначе `0`. В M8 left — parent, right — child. Table-level hint не доказывает
  field-level FK и не снимает `FK_UNRESOLVED`. Проверять table refs и ordered
  column pairs; не повышать score за центральность/число FK.
  Same-table neighborhood уже учтён structural signal и повторно не суммируется.
- Для composite FK нужен полный набор сопоставленных компонент, включая
  проверяемую компоненту кандидата; частичное совпадение даёт reason, не бонус
  доказанной связи. Для одной source relationship брать максимум по подходящим
  FK, не сумму. Конкурирующие FK с одинаковыми endpoints остаются разными
  evidence refs. Join-table hints — только structural evidence из M7.
- SCC/self-reference сохраняются; если рекомендация требует такой связи,
  `FK_STRATEGY_REQUIRED` блокирует auto. Неполный parent mapping означает
  `FK_UNRESOLVED`, а не доказательство невозможности: родитель может уже быть
  в БД, но M9 этого не проверяет. Не выдумывать связь, если FK отсутствует.
  Anchors замораживаются после первого прохода: нет iterative self-reinforcement,
  greedy global assignment или автоматического переноса всех полей в одну table.

#### 5. Candidate generation и top-k pruning

1. Ограничить размеры входов, проверить supported schemas, invariants/hashes
   и bindings. Пересечь catalog с явным scope, исключить denied, системные объекты,
   views/materialized views и non-writable/generated columns. Системные schemas
   PostgreSQL и SQLite internal names проверять по фиксированным dialect rules.
   Даже переданный caller каталог не является доказательством allowlist.
2. Один раз построить bounded lookup по refs/qualified names, normalized tokens,
   aliases и canonical types только разрешённых targets; graph context работает
   только с endpoints в scope. Generated PK может оставаться разрешённой
   metadata для graph, хотя он исключён из writable candidate targets.
   Индексы ускоряют поиск evidence, не вводят скрытый shortlist. Первым проходом
   по допустимым field×column парам найти anchors.
3. Вторым проходом оценивать все допустимые пары с хотя бы одним name/alias,
   value-pattern или context evidence. Type equality в одиночку не порождает
   кандидата для каждой безымянной текстовой колонки. Hard incompatibility
   исключать; conditional/unknown сохранять с blocker для ручного анализа.
4. После полного context scoring хранить bounded heap из `max(top_k, 2)` лучших
   кандидатов каждого поля. Безопасное раннее pruning разрешено только когда
   сумма уже вычисленных contributions и максимума оставшихся строго меньше
   текущего последнего retained score; сравниваются scores **до penalties**.
   При равенстве upper bound нельзя отбрасывать потенциальную ничью.
5. Gap/ambiguity определить до обрезки пользовательского top-k. При `top_k=1`
   учитывать скрытого runner-up; diagnostics хранят его score/gap и факт наличия,
   без раскрытия исключённых policy targets. Все равные пограничные кандидаты
   участвуют в tie count, выдаются первые k по стабильному ключу.

`top_k=5`, допустимо 1–10. M9 использует полный bounded scan и heap, без
приблизительного ANN/embedding поиска или произвольного pre-top-50. Время —
O(F×C×стоимость bounded signals + F×C×log(k)), два прохода; не хранить матрицу
F×C. Контекст заранее агрегировать по source entity/table и FK lookup.

Начальные budgets: 1024 source fields, 10 000 разрешённых колонок, 20 000 FK
edges, 4096 source relationships, 10 000 aliases, 256 UTF-8 bytes на matching
label/alias, 32 tokens на имя, 2 000 000 field×column pairs **на оба прохода
суммарно**, 16 MiB входных DTO, 32 MiB retained state и 8 MiB result.
Лимит пары проверять до проходов; expansions/graph evidence operations имеют
дополнительный cap 10 000 000. Budgets валидируются и имеют hard ceilings;
превышение любого — `MAPPING_LIMIT_EXCEEDED`, без усечённого «успешного» ranking.
Разрешённый размер whole catalog проверяется до построения проекций/rehash.
Async wrapper делает cancellation checkpoints между bounded блоками работы;
никаких background tasks, shared mutable caches или обещаний OS/RSS isolation.

#### 6. Signal breakdown, weights и calibration

Для baseline `deterministic_mapping_v1` выбрать прямую объяснимую сумму §12:

| Сигнал | Начальный вес | Объяснение |
|---|---:|---|
| `name_similarity` | 0.35 | Лучший вариант сравнения имени, с кодом метода |
| `alias_match` | 0.20 | Scoped alias / версия concept dictionary |
| `type_compatibility` | 0.20 | Семейство и constraints; conditional отдельно |
| `value_pattern_match` | 0.10 | Доля поддерживающих observations и coverage |
| `structural_context` | 0.10 | Source labels / соседние anchors |
| `database_relation_score` | 0.05 | Поддержанные FK relationships |

`base_score = Σ(weight × signal)`, итоговый `confidence = clamp(base_score −
ambiguity_penalty − validation_penalty − security_penalty, 0, 1)`.
LLM-сигнала и LLM-веса в options нет. Недоступный сигнал имеет value `0` и
`available=False`, наблюдаемое несовпадение — `0` и `available=True`.
Не перераспределять вес отсутствующего evidence: sparse profile не должен
становиться уверенным только от совпадения имени. Это сознательно снижает
auto coverage; точное имя само по себе не обязано достигать `0.90`.

В каждом breakdown сохранять signal code/value/weight/contribution, availability,
coverage, reason codes и безопасные refs подтверждающих связей; penalties —
отдельно. `evidence` старого DTO — уникальные machine-readable codes в стабильном
порядке. Не записывать raw examples, значения extrema, comments или alias text
в explanations/errors. Name и alias могут быть коррелированы: веса проверяются
ablation, отдельного обученного вероятностного confidence в M9 нет.

Weights — конечные `Decimal`, неотрицательные, сумма ровно 1; thresholds в
[0,1], `review_threshold < auto_threshold`, положительный margin. Ограничить
размер коэффициента/exponent до арифметики. Вычислять в локальном фиксированном
Decimal context, итог квантовать до 6 знаков с ROUND_HALF_EVEN; gap/threshold
сравнивать по этому же опубликованному score. Никаких float, process locale,
чтения env или случайной инициализации. Options содержат веса, thresholds,
penalty constants, budgets и фиксированные версии правил; конфигурация immutable.
Версия Unicode database stdlib входит в normalization metadata/fingerprint:
воспроизводимость между Python minor versions не обещается при её изменении.

Calibration выполняется offline на размеченных синтетических ru/en fixtures:
отдельные calibration и holdout схемы/сущности, фиксированный порядок перебора
небольшой сетки весов, метрики recall@1/@5/@10, MRR, precision auto и review/abstain
coverage, отдельно ru/en/транслитерация и generic names. Выбирать weights по
минимуму ложных auto assignments, затем recall@k; tie — по каноническому tuple
weights. Начальные weights выше — гипотеза, не измеренная вероятность.
Приёмка: ни одного ложного auto решения на обязательных negative fixtures,
100% recall@5 на перечисленных однозначных bilingual fixtures и неизменность
holdout при повторном запуске; более широкие метрики фиксируются без выдуманного
production SLA. Новые defaults требуют version/config fingerprint и записи
результатов; обучения, адаптации по feedback и network datasets в runtime нет.

#### 7. Tie-breaking, ambiguity и конфликты

- Упорядочивать по убыванию quantized base score, затем по точным
  `(schema_name, table_name, column_name, table_id, column_id)` в Unicode code-point
  порядке. Matching-нормализация не меняет identity или ключ сортировки.
  Source fields сортировать по `(entity_type, field_name)`; сигналы — по закрытому
  порядку codes. Candidate ID — versioned canonical SHA-256 от source/target refs,
  всех lineage/scope/config/semantic/profile bindings и producer version.
  Перестановки эквивалентных alias entries канонизируются перед fingerprint.
- `gap = first.base_score − second.base_score`, вычислять по всем admissible
  конкурентам; если конкурент один, gap отсутствует, а не искусственно равен 1.
  Базовые thresholds: auto `0.90`, review `0.70`, margin `0.10`.
  `gap < 0.10` — `AMBIGUOUS_TARGET`, exact tie всегда неоднозначна.
- Penalties одинаковы для всех кандидатов одного source field, чтобы не
  менять смысл heap/pruning: ambiguity `0.10` при малом gap, validation `0.10`
  при конфликте двух top-1 source fields за один target, security `0` после hard
  policy filtering. Candidate-specific проблемы отражаются совместимостью
  и blockers. Запреты никогда не превращаются в малый числовой штраф.
- Итоговый top-1 ≥ auto, достаточный margin (либо единственный кандидат),
  native type compatible, достаточный evidence и отсутствие blocker дают
  `auto_candidate`. Среди blockers: inference ambiguous/insufficient,
  transformation required, недостаточная проверка constraints/FK, parse issues,
  усечённый используемый context/pattern scan, generic-only/confusable evidence.
  Применимость blocker должна ссылаться на конкретное использованное evidence:
  absence of optional labels сама по себе не является ошибкой.
- Score ≥ review при невозможности auto даёт `review`; меньший score —
  `rejected`, отсутствие admissible кандидатов — `unmapped`. Exact boundary
  `0.70` включается в review, `0.90` — в auto только при остальных условиях;
  gap ровно `0.10` достаточен. В списке сохраняются и низкие scores для объяснения.
- Два top-1 source fields к одному target получают `TARGET_COLLISION` и blocker
  auto независимо от score; не переназначать второе поле жадно. Кандидаты одной
  source entity в разных таблицах допустимы, а финальная grouping/identity/FK
  strategy и проверка required targets остаются за MappingPlan validator.

### Вертикальная последовательность и проверки

Каждый шаг выполнен с regression/contract tests, минимальной реализацией,
узкой проверкой и review diff. Ниже указаны фактические пути реализации.
Команды `uv run --locked --no-sync pytest` выполняются из корня репозитория;
к относительным `tests/...` в таблице подставляется полный префикс, указанный выше.

| Шаг | Файлы и наблюдаемое поведение | Тест и команда проверки |
|---|---|---|
| 1. Контракт и bindings | `src/contracts/deterministic_mapping.py`, `src/contracts/semantic_catalog.py`, `src/ports/mapping.py`, exports в соответствующих `__init__.py`; lineage и явный scope, старый MappingCandidate без изменений; ADR 0019 о bounded ranking и объяснениях | `tests/unit/contracts/test_m09_mapping_contracts.py`, `tests/contract/mapping/test_mapper_ports.py`: legacy roundtrip, неверные refs/hashes/options, manifest vs content binding; pytest по этим файлам и `tests/unit/contracts/test_m02_security_regressions.py` |
| 2. Лексика и aliases | `src/mapping/_names.py`, `src/mapping/_aliases.py`: ru/en формы, versioned словари, коллизии без last-write-wins | `tests/unit/mapping/test_names.py`, `test_ranking.py`: bilingual matrix и scoped aliases |
| 3. Совместимость | `src/mapping/_compatibility.py`: native/conditional/unknown, checked coverage и hard conflicts по M7/M8 metadata | `tests/unit/mapping/test_compatibility.py`; pytest по нему и существующему `tests/unit/profiling/test_inference.py` |
| 4. Context и полный top-k | `src/mapping/_context.py`, `_ranking.py`, `mapper.py`, `__init__.py`: frozen anchors, ordered FK pairs, bounded scan/heap, gap, collisions, explanations и options | `tests/unit/mapping/test_context.py`, `test_ranking.py`: orders/customers/products, runner-up при k=1, безопасный pruning; pytest по ним и `tests/unit/database/test_dependency_graph.py` |
| 5. Reproducibility и boundaries | Preflight/cancellation в `mapper.py`, safe summary и budget validation в новых contracts; явный smoke boundary для `mapping` без импорта database adapters/LLM | `tests/property/mapping/test_mapper_properties.py`, `tests/security/mapping/test_boundaries.py`, `tests/smoke/test_mapping_boundaries.py`: permutations, hostile metadata, бюджеты, отсутствие I/O/утечек; pytest по этим путям |
| 6. Calibration и сквозная приёмка | `tests/fixtures/mapping/ru_en_cases.json`, `scripts/evaluate_deterministic_mapping.py`, `docs/deterministic-mapping.md`, `docs/plans/M09_acceptance.md`, `mkdocs.yml`: reproducible metrics, API example, defaults/ограничения | `tests/integration/test_deterministic_mapping.py`, `tests/unit/mapping/test_m09_acceptance.py`: real M8 profile + M7 SQLite catalog → top-k; pytest по файлам, запуск evaluator, `make docs` |

Для smoke boundary ограничить импорты нового mapping package явным набором
pure dependencies, сохранив текущие запреты `test_m02_layer_boundaries.py`.
Не ослаблять domain allowlist ради размещения mapper в domain: алгоритмы нового
компонента могут использовать уже существующие domain helpers в разрешённом
направлении. Существующие profiler/graph tests использовать как регрессионные,
не переписывать их под новую реализацию.

### Обязательная матрица тестов на русском и английском

Test identifiers — английские, docstrings и пояснения — русские; входные данные
проверяются на обоих языках. Каждый положительный случай имеет явный target и
близкого ложного конкурента, а не только проверку «score > 0».

| Группа | Пары / условия | Проверяемое ожидание |
|---|---|---|
| Names | email→email; Customer Name / customer-name / CUSTOMERNAME→customer_name; fullwidth Unicode; ё/е; empty punctuation | Правильный top-1, точный метод/cap, пустые формы не совпадают |
| Transliteration | Имя клиента→imya_klienta; Счёт / Счет→schet; латинская a в кириллическом имени | Дополнительный сигнал слабее native/alias; коллизия и confusable не дают auto |
| Aliases | Контрагент / Покупатель / Customer→customers.legal_name; ИНН организации / Tax ID→customers.inn | Scoped catalog управляет семантикой; такой же alias в suppliers сохраняет review |
| Типы/локали | 01.09.2026 (ru_RU), 09/01/2026 (en_US), 01/09/2026 (en_GB), unspecified; 125 000,50 / 125,000.50 | Используются M8 inference/coverage; DATE/NUMERIC в top-k, необходимые conversions и locale ambiguity блокируют auto |
| Patterns | email, phone, UUID, URL, валидные ИНН 7707083893 / 500100732259, invalid checksum, money/currency, boolean, integer ID, category/free text | Alias не перекрывает конфликт patterns; text/code сохраняет leading zeros, generic text не получает чужой semantic bonus |
| Source context | Заказы / Orders, Номер заказа / Order number, Сумма / Amount; Товары / Products, SKU, Цена / Price; file/sheet/JSON/XML/HTML/PDF/DOCX labels | Контекст повышает соответствующую table без автоматического назначения всех полей одной entity туда же |
| Отсутствующий evidence | Нет labels, null-only, mixed kinds, pattern_skipped, pair_limit, estimated uniqueness | Явные unavailable/reasons, нет выдуманного context/identity или auto из недостаточных данных |
| FK | customers→orders→order_items←products; composite keys с переставленными pairs; isolated table, self-FK, SCC, join hint с payload | Правильная ориентация, полный composite evidence; невозможность/неполнота не скрыты высоким score |
| Неоднозначность | Дата / Date; Статус / Status; одинаковые имена в двух schemas; aliases с двумя targets; два source fields к одной колонке | Stable tie, gap до top-k, TARGET_COLLISION, отсутствие greedy reassignment |
| Числовые границы | Scores 0.699999 / 0.700000 / 0.899999 / 0.900000; gaps 0 / 0.099999 / 0.100000; k=1/5/10 | Точные inclusive thresholds; runner-up учитывается при k=1; penalty объясним |
| Детерминизм | Permutations schemas/tables/columns/aliases/edges, PYTHONHASHSEED, Decimal context, конкурентные rank calls | Равные ranking/score/decisions; одинаковые inputs дают те же IDs, semantic изменения config/bindings меняют IDs |
| Pruning/лимиты | Малый exhaustive oracle и heap; победитель с более слабым name, но сильным context; k ties; большие catalogs и длинные aliases | Равный top-k и gap, нет потери context winner; cap даёт полный typed failure |
| Security | Denied/generated/view/system targets, stale policy/hash, model_construct, instructions в comments/aliases, secrets в labels, mocked LLM/DB/network/file calls | Запрещённые targets отсутствуют, foreign binding отклоняется, любые внешние вызовы падают тестом, logs/repr/errors содержат только safe counters/codes |

Для content-equivalent rebatching/смены raw sampling ожидать одинаковые scores
при одинаковом scoring evidence, но не требовать одинаковых candidate IDs:
manifest/profile bindings могут измениться по контракту M8. Для permutation
tests пересчитывать profile hash, если меняется его wire порядок; сравнивать
semantic ranking отдельно от identity. DTO не сортирует ordered FK pairs или
значимые последовательности ради удобства теста.

При завершении **реализации M9** выполнить `$structuraguard-review` и
`$structuraguard-security`, затем `make lint`, `make typecheck`, `make test`,
`make test-integration`, `make test-security`, `make docs`. Если изменятся DB
adapters, дополнительно `make test-database` с Docker. Фактические результаты
реализации и причины пропусков приведены в checklist ниже.

## Checklist commit / PR

Дата передачи: 2026-09-11. Ветка `feat/m09-deterministic-mapper`.
По локальным refs `HEAD = main = origin/main = d5f3abe`; fetch не выполнялся,
актуальность удалённого `main` и будущий CI не подтверждены.
Staging area пуста. В diff **53 файла: 7 modified, 46 untracked**;
все новые файлы входят в review и предназначены для ручного commit M9.

- [x] Все семь критериев приёмки и 13 групп test matrix сопоставлены с
  наблюдаемыми assertions в [матрице](M09_acceptance.md).
  Все 65 упоминаний test functions/files разрешаются в существующие Python tests.
- [x] SDK вычисляет final score; ru/en names, aliases, type/pattern,
  structure/graph signals видимы. Top-k, weights, ties и ambiguity проверены.
- [x] Нет LLM/embeddings, SQL, MappingPlan generation или нового write authority.
  Новых production dependencies и изменений legacy wire contract нет.
- [x] Findings финального review исправлены: confusable target не становится
  anchor; table name/alias нормализуются после scope/writability checks.
  Generated/FK metadata остаётся доступной для предусмотренных graph checks.
- [x] На оба дефекта добавлены security regressions с положительными controls:
  `tests/security/mapping/test_m09_review_regressions.py` — до исправлений
  **7 failed, 3 passed**, после — **10 passed**.
- [x] Повторный review исправленного diff не выявил существенных findings.
  Публичный API и границы M9 сохранены, несвязанный код не менялся.
- [x] Русские public docstrings, копируемые examples, ограничения и ссылки
  на каноническое ТЗ отражают подтверждённое поведение; ADR 0019 сохранён.
- [x] `docs/codex/PROJECT_STATE.md` обновлён по последним проверкам.
- [x] При передаче прошли 274 tests M9/docs, lint/typecheck, strict docs,
  offline lock check и wheel/sdist verification. Evaluation JSON воспроизведён.
- [x] `git diff --check` проходит; выполнена отдельная проверка untracked
  файлов, secrets/debug markers и состава артефактов. Secrets, случайных
  generated files и закомментированных debug blocks не обнаружено.
  `site/`, `dist/`, caches игнорируются Git; stdout evaluator — штатный JSON.
- [x] Причины непроверенных сценариев перечислены ниже.
- [ ] Пользователь проверил итоговый состав staging и создал commit.
- [ ] Пользователь опубликовал ветку и открыл PR в актуальный `main`.
- [ ] CI прошёл для конкретного опубликованного commit.

### Фактически выполненные команды

Из корня репозитория на Python 3.12.9, после последних runtime исправлений:

```bash
env UV_CACHE_DIR=/private/tmp/structuraguard-m09-uv-cache uv run --locked --no-sync pytest \
  packages/structuraguard/tests/security/mapping/test_m09_review_regressions.py -q --tb=short

env UV_CACHE_DIR=/private/tmp/structuraguard-m09-uv-cache uv run --locked --no-sync pytest \
  packages/structuraguard/tests/unit/mapping \
  packages/structuraguard/tests/security/mapping \
  packages/structuraguard/tests/property/mapping \
  packages/structuraguard/tests/contract/mapping \
  packages/structuraguard/tests/unit/contracts/test_m09_mapping_contracts.py \
  packages/structuraguard/tests/smoke/test_mapping_boundaries.py \
  packages/structuraguard/tests/integration/test_deterministic_mapping.py \
  packages/structuraguard/tests/docs/test_m09_examples.py -q --tb=short

env UV_CACHE_DIR=/private/tmp/structuraguard-m09-uv-cache make lint typecheck
env UV_CACHE_DIR=/private/tmp/structuraguard-m09-uv-cache make test test-integration test-security docs
git diff --check
```

| Проверка после исправлений | Фактический результат |
|---|---|
| Regression/control cases | 10 passed |
| M9 и его примеры | 161 passed |
| `make lint` | 336 файлов; Ruff без ошибок |
| `make typecheck` | 332 файла; mypy без ошибок |
| `make test` | 2900 passed, 84 deselected; 5 существующих SWIG warnings |
| `make test-integration` | 23 passed |
| `make test-security` | 642 passed |
| `make docs` | Strict MkDocs build прошёл |
| `git diff --check` | Passed |

Локальные журналы: `/private/tmp/structuraguard-m09-review-fixes-full-gates.log`,
`/private/tmp/structuraguard-m09-review-fixes-lint-typecheck.log`,
`/private/tmp/structuraguard-m09-review-regressions-before.log`.
Они не входят в commit. Полные tests запускались с разрешённым доступом
к loopback/watchdog fixtures вне sandbox; security controls не отключались.

Дополнительно на этапе передачи выполнены:

```bash
env UV_CACHE_DIR=/private/tmp/structuraguard-m09-uv-cache uv run --locked --no-sync pytest \
  packages/structuraguard/tests/unit/mapping \
  packages/structuraguard/tests/security/mapping \
  packages/structuraguard/tests/property/mapping \
  packages/structuraguard/tests/contract/mapping \
  packages/structuraguard/tests/unit/contracts/test_m09_mapping_contracts.py \
  packages/structuraguard/tests/smoke/test_mapping_boundaries.py \
  packages/structuraguard/tests/integration/test_deterministic_mapping.py \
  packages/structuraguard/tests/docs -q --tb=short

env UV_CACHE_DIR=/private/tmp/structuraguard-m09-uv-cache make lint typecheck docs
env UV_CACHE_DIR=/private/tmp/structuraguard-m09-uv-cache make docs
env UV_CACHE_DIR=/private/tmp/structuraguard-m09-uv-cache uv lock --check --offline
env UV_CACHE_DIR=/private/tmp/structuraguard-m09-uv-cache make test-build
make test-build

env UV_CACHE_DIR=/private/tmp/structuraguard-m09-uv-cache uv run --locked --no-sync python scripts/evaluate_deterministic_mapping.py > /private/tmp/structuraguard-m09-handoff-evaluation-a.json
env UV_CACHE_DIR=/private/tmp/structuraguard-m09-uv-cache uv run --locked --no-sync python scripts/evaluate_deterministic_mapping.py > /private/tmp/structuraguard-m09-handoff-evaluation-b.json
cmp /private/tmp/structuraguard-m09-handoff-evaluation-a.json /private/tmp/structuraguard-m09-handoff-evaluation-b.json

git status --short --untracked-files=all
git diff --stat
git diff --cached --stat
git branch --show-current
git log -1 --format='%h %s'
git rev-parse HEAD main origin/main
git diff --check
```

| Проверка передачи | Фактический результат |
|---|---|
| M9 и весь каталог documentation examples | 274 passed: 157 runtime M9 + 117 docs |
| Lint/typecheck | 336/332 файла, passed |
| Strict docs | Passed после исправления ссылки на несуществующий anchor; strict mode сохранён |
| Offline lock check | 105 packages; passed вне sandbox после panic `uv` в macOS SystemConfiguration внутри sandbox |
| `make test-build` | Wheel/sdist 0.3.0 собраны; distribution verification OK с обычным uv cache |
| Offline evaluation | Два JSON побайтно равны; holdout 12 случаев, recall@5=1, false_auto=0, auto только 1/12 |
| Test references и чистота diff | 65 ссылок проверены через Python AST; secrets/debug/artifacts scan всех 53 файлов и просмотр production comments без находок; whitespace проверен также для untracked |

Первая попытка distribution verification с временным `UV_CACHE_DIR` не прошла:
в этом cache нет `hatchling==1.32.0` для изолированной offline-пересборки sdist.
Повторный `make test-build` использовал существующий обычный cache и прошёл.
Зависимости не скачивались, constraints/checks не менялись. Это устранённые
проблемы окружения и ссылки документации, а не пропущенные gates.

Журналы передачи: `/private/tmp/structuraguard-m09-handoff-tests.log`,
`/private/tmp/structuraguard-m09-handoff-docs-gates.log`,
`/private/tmp/structuraguard-m09-handoff-docs-final.log`,
`/private/tmp/structuraguard-m09-handoff-build.log`,
`/private/tmp/structuraguard-m09-handoff-build-final.log`.

### Состав ручного commit

| Группа | Число файлов | Состав |
|---|---:|---|
| SDK | 15 | `src/contracts/deterministic_mapping.py`, `semantic_catalog.py`, `contracts/__init__.py`; `src/ports/mapping.py`, `ports/__init__.py`; 10 файлов `src/mapping/` |
| Tests и fixtures | 28 | `tests/unit/mapping/` (9), `contract/mapping/` (3), `property/mapping/` (2), `security/mapping/` (6); `unit/contracts/test_m09_mapping_contracts.py`, `unit/ports/test_m02_protocols.py`, `smoke/test_mapping_boundaries.py`, `integration/test_deterministic_mapping.py`, `docs/test_m09_examples.py`; `fakes/mapping.py`, `fakes/mapping_evaluation.py`, `fixtures/mapping/ru_en_cases.json` |
| Документация | 7 | `docs/codex/PROJECT_STATE.md`, `docs/codex/SPEC_INDEX.md`, `docs/deterministic-mapping.md`, `docs/adr/0019-deterministic-mapping-candidates.md`, `docs/plans/M09_deterministic_mapper.md`, `M09_acceptance.md`, `M09_security_review.md` |
| Scripts/config | 3 | `scripts/evaluate_deterministic_mapping.py`, `scripts/verify_distribution.py`, `mkdocs.yml` |

`src` и `tests` используют префиксы из раздела контрактов. Остальные пути
относительны корню репозитория. Список сверен с `git status --untracked-files=all`;
автоматических `git add`, commit, push или PR не выполнялось.

### Пропущенные проверки и residual risks

| Проверка / сценарий | Причина и граница подтверждения |
|---|---|
| `make test-database`, 84 opt-in PostgreSQL tests | DB adapters не менялись. Mapper проверен на catalog DTO и сквозном SQLite пути; живая PostgreSQL не проверялась |
| Production accuracy и реальные пользовательские схемы | Доступен только синтетический корпус из 24 случаев; calibration/holdout разделены, но это не доказательство production precision |
| Максимальные F×C, wall-clock/RSS, все формы графов | Проверены bounded failure, heap oracle и перечисленные graph fixtures; workload у hard ceilings и исчерпывающий перебор не выполнялись |
| Другие Python/Unicode и платформы | Локально проверена Python 3.12.9; более широкая матрица остаётся за CI |
| Живые grants, schema drift после snapshot, FK values | В M9 нет DB access; свежесть и авторизация требуют будущей validation перед загрузкой |
| LLM/embeddings, MappingPlan/SQL/load | Исключены утверждённым scope M9; реальные LLM API не вызывались |
| Remote main, внешние URLs, опубликованный commit/CI | Сетевой fetch и создание commit/PR не выполнялись по задаче; внутренние ссылки проверяет strict docs, заголовки канонического ТЗ сверены локально |
| Повтор полного runtime suite после правок передачи | На этапе передачи менялась только документация; актуальные результаты runtime приведены выше, examples/docs проверяются отдельно |

## Риски

- Recall/false positives: транслитерация, общие aliases и связанные signals
  могут завысить score; fixed thresholds без holdout не подтверждают точность.
  Sparse profiles при фиксированных весах чаще потребуют review — это видимая
  цена консервативного baseline, измеряемая отдельно для ru/en.
- Schema drift/permissions: проверяется только переданный snapshot. Перед
  будущим load нужны новый inspection, policy/identity/FK validation и штатные
  staging, dry-run, transaction/rollback; M9 этих гарантий не выдаёт.
- Неполные агрегаты: M8 не хранит все enum values/decimal scales и может сокращать
  context/pattern evidence. Некоторые несовместимости можно выявить только
  последующей валидацией records; их нельзя считать доказанно безопасными.
- Resource exhaustion: полный scan ограничен F×C и budgets. Большая легальная
  схема может не поместиться; caller должен явно сузить scope или изменить
  допустимые limits. Не возвращать случайный shortlist при исчерпании ресурсов.
- Compatibility/privacy: новый result и его hashes не являются legacy wire
  MappingCandidate и могут содержать sensitive refs. Нужны versioned contract,
  отдельная safe summary и regression на manifest/content fingerprint binding.
