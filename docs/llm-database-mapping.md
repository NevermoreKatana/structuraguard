# Семантическое сопоставление с БД M10

`LLMSemanticMapper` предлагает отображение нормализованных сущностей на существующие
таблицы/столбцы, используя deterministic top-k M9 и provider-neutral LLM layer M6.
Он не создаёт и не исполняет `MappingPlan`, не обращается к БД и не меняет данные.

## Получить предложение для поля email

Функция ниже принимает завершённый [профиль M8](normalized-profiling.md) с одним
полем `email` и [каталог M7](database-inspection.md). Она разрешает только
`public.customers.email`; колонка `email` другой таблицы не попадёт в prompt.
Для SQLite замените selector `public` на имя schema из каталога, обычно `main`.
Если разрешённой колонки нет, результат требует review без вызова LLM.

<!-- example:m10-proposal:start -->
```python
from structuraguard.contracts import (
    CatalogColumnRef,
    DatabaseCatalog,
    MappingScope,
    NormalizedDataProfile,
    SemanticMappingContext,
    SemanticMappingOptions,
    SemanticMappingResult,
)
from structuraguard.llm import PolicyAwareLLMRouter
from structuraguard.mapping import LLMSemanticMapper
from structuraguard.ports import SecurityScanner


async def propose_email(
    profile: NormalizedDataProfile,
    catalog: DatabaseCatalog,
    *,
    router: PolicyAwareLLMRouter,
    scanner: SecurityScanner,
    run_id: str,
    options: SemanticMappingOptions | None = None,
) -> SemanticMappingResult:
    allowed = tuple(
        CatalogColumnRef(table_id=table.table_id, column_id=column.column_id)
        for schema in catalog.schemas
        for table in schema.tables
        for column in table.columns
        if (schema.name, table.name, column.name) == ("public", "customers", "email")
    )
    scope = MappingScope(
        target_id=catalog.target_id,
        target_policy_fingerprint=catalog.target_policy_fingerprint,
        allow=allowed,
    )
    mapper = LLMSemanticMapper(
        router=router,
        scanner=scanner,
        context=SemanticMappingContext(run_id=run_id),
        options=options,
    )
    return await mapper.propose(profile, catalog, scope=scope)
```
<!-- example:m10-proposal:end -->

В своём async коде вызовите `await propose_email(profile, catalog, router=router,
scanner=scanner, run_id=run_id)`. `run_id` должен совпадать с run router.
Приложение создаёт trusted scanner и один router на весь run, владеет lifecycle
providers и их конфигурацией. По умолчанию metadata имеет класс RESTRICTED:
нужен разрешённый local provider либо режим `no_llm`.
Router не является `LLMProvider`. Настройка и offline-пример provider:
[руководство M6](llm.md).

Этот блок проверяется отдельно с `FakeLLMProvider` и синтетическими M7/M8
snapshots; тестовый scanner не является production DLP. Команда из корня проекта:

```bash
uv run --locked --no-sync pytest packages/structuraguard/tests/docs/test_m10_examples.py -q
```

## Настроить и прочитать результат

Для обычного log используйте `proposal.safe_summary()`: только counts,
classification, status и action. В `group.choices` доступны проверенные source IDs,
selected IDs, SDK scores, reasons и action. Их связь с исходными полями и точными
table/column refs хранится в `group.candidates.fields` и
`group.candidates.columns[*].mapping`. Table/column/relation IDs локальны группе;
их нельзя переносить между candidate sets.

`group.prompt` и `group.calls` сохраняют prompt fingerprint, provider/model,
usage и metadata фактических attempts. Полный proposal содержит чувствительные
локальные refs и evidence даже при `response_retention="metadata_only"`.

`proposal.response_schema_id`, `response_schema_version` и
`response_schema_fingerprint` сохраняют identity ожидаемой schema из M6 registry.
Они заполняются во всех режимах retention, включая `no_llm` и пустой target set;
это не свидетельство фактического provider call. У старых сериализованных
результатов все три поля остаются `None` и не добавляются при повторной
сериализации. Частично заполненная schema metadata отклоняется как
`pydantic.ValidationError`; старому ответу не приписывается текущий hash.

`semantic_catalog=DatabaseSemanticCatalog(...)` необязателен. `ranking_options`
настраивает существующий M9 ranking; `options=SemanticMappingOptions(...)` задаёт
лимиты и aggregation M10. Для локальной инспекции candidate set доступен async
`prepare_semantic_mapping(profile, catalog, scope=scope, ...)`, без scanner/provider
и сетевых вызовов. Полный результат подготовки чувствителен.

При раздельной настройке M9/M10 эффективный column top-k равен минимуму
`ranking_options.top_k` и `options.top_k`; более широкий ranking не расширяет
budget prompt. Результат M9 сохраняет competitor count для отброшенного хвоста.

При создании HTTP provider зарегистрируйте `semantic_mapping_prompt()` и
`semantic_mapping_response_schema()` из `structuraguard.mapping` в существующих
`prompts`/`schemas` M6. Deployment должен поддерживать purpose `semantic_mapping`,
structured output и известные byte/token/context limits. JSON object mode также
использует строгую локальную schema. Schema и trusted system messages учитываются
в размере wire request; native JSON Schema дополнительно увеличивает этот размер.
Подключение provider/router описано в [руководстве M6](llm.md).

## Candidates и split

Mapper повторно проверяет snapshots, fingerprints и scope, затем вызывает M9.
`parent_child` M8 связывает entity types в группы; `co_occurrence` не объявляется FK.
Для каждой группы выполняется одна логическая заявка router. Default top-k — 5,
ceiling — 10; таблицы выбираются только из column candidates. Generated,
non-writable, system и запрещённые scope targets в writable candidates отсутствуют.

Модель возвращает `SemanticMappingDecision`: обязательные `schema_version`,
`group_id`, `candidate_set_fingerprint`, `tables`, `columns`, `relations`,
`review_required`. Каждый source имеет полный список `assessments` переданных
ему candidates, status `selected/ambiguous/unmapped` и закрытый `reason_code`.
Для колонок и связей выбор один или null; для таблиц `selected_candidate_ids`
допускает несколько значений. Scores — строки `0.000000`–`1.000000` с шестью
дробными знаками. Extra fields, свободный reasoning, SQL, код и команды запрещены.

Несколько таблиц одного entity type означают **proposal split**. SDK требует,
чтобы выбранные поля покрывали выбранные таблицы, а выбранные FK связывали их.
Отдельные relation contexts для пар таблиц позволяют цепочки из нескольких FK.
Каждый composite FK сохраняет весь ordered список компонентов и допустимые пары
source anchors. Пропуск одного компонента, выбор чужой таблицы или unrelated split
отклоняется целиком. Исходные normalized entities при этом не разделяются и не
переписываются; компиляция будущего load plan остаётся отдельной задачей.

## Privacy и доверие

В prompt попадает отдельная bounded проекция: entity/field descriptors, masked
example kinds, ratios/pattern evidence, retained target names/types, безопасные
comments/descriptions/aliases и FK summaries. Полный catalog, raw rows, extrema,
SQL expressions, source paths, credentials и connection objects не отправляются.
Raw examples заменены `[MASKED]`; типовые email/phone/credential/URL fragments
маскируются, длинные текстовые значения исключаются с `redacted_items`.
Masking v2 проверяет byte limit до regex: превышающие 256 bytes descriptors
или 512 bytes comments/descriptions целиком заменяются `[REDACTED]`.
Известный active content в передаваемых descriptors запрещает запрос до egress.

Этот локальный фильтр — минимизация, а не универсальный DLP. Внедрённый trusted
scanner обязан проверить окончательный canonical payload, включая DB descriptions
и пользовательские PII patterns, и вернуть фактический разрешающий report.
`SecurityApproval` сверяется с exact payload/source/run/classification/redaction/
routing bindings. Неполное PII evidence M8 повышает classification до RESTRICTED
и сохраняет review. Default metadata classification — RESTRICTED; masking
никогда не понижает класс. Credentials запрещены также для local providers.

M6 router запрещает RESTRICTED cloud, а `privacy_first` — также CONFIDENTIAL cloud.
Fallback после transient failure сохраняет тот же payload и policy, использует
общий бюджет и повторяет проверку маршрута. Собственных retries/repairs у mapper
нет. Raw payload/response не попадают в call history; полный proposal нельзя
сериализовать в обычный log. Для этого предназначен `safe_summary()`.

`SemanticMappingOptions.response_retention` задаёт, сохранять ли проверенный ответ:

| Policy | Результат |
|---|---|
| `validated_decision` (default) | `group.decision` содержит только закрытый DTO после schema, membership и FK validation |
| `metadata_only` | `group.decision=None`; остаются SDK choices/scores, fingerprints и provider call metadata |

Raw response отбрасывается в обоих режимах, включая ошибки; сохранение произвольного
raw text не поддерживается. M10 не создаёт постоянного response store: срок хранения
возвращённого sensitive proposal задаёт приложение. Retention не влияет на обязательную
валидацию, cross-group collision checks, classification и SDK outcome.

Source values всегда недоверенные. `[MASKED]` и `[REDACTED]` необратимы: mapper
не хранит PII substitution map и не восстанавливает значения. Ответ содержит только
IDs, поэтому обратная подстановка не нужна. Локальное разрешение выбранных IDs
в target references выполняется после безопасного ответа. Instructions отделены
от JSON data, а расширение полномочий через field name/value пресекают projection,
scanner, закрытая schema и membership checks независимо от послушания модели.

Ошибки внешнего scanner нормализуются без исходного text/notes/cause: timeout
сохраняет `LLM_TIMEOUT`, отмена — `CancelledError` без сообщения, прочие ошибки —
`LLM_POLICY_DENIED`. Process-control исключения не перехватываются как обычный сбой.

## Confidence и исходы

SDK использует `D` — исходный M9 base score и `L` — LLM signal:
`score = clamp((1 − λ) × D + λ × L − A − V − S, 0, 1)`.
D включает name, alias, type, value pattern, structural context и FK graph signals
с их весами M9. Default λ=0.20, ceiling 0.30; модель не задаёт итоговый score.
Исходные M9 signals/explanations сохраняются отдельно.

| Штраф SDK | Default | Evidence |
|---|---|---|
| A: `ambiguity_penalty` | 0.10 | Неоднозначность модели, недостаточный gap или непроверенный competitor |
| V: `validation_penalty` | 0.10 | Сохранившиеся blockers M9, кроме ambiguity/collision/confusable; требование стратегии FK |
| S: `security_penalty` | 0.20 | Разрешающий scan с warning, неполная классификация или confusable name; не меньше security penalty M9 |

Каждый штраф применяется один раз за категорию; при отсутствии evidence равен 0.
Masking сам по себе не является нарушением. M9 ambiguity/collision пересчитываются
по проверенному assignment; подтверждённая collision отклоняет ответ целиком.
Запрет security/allowlist, неизвестный identifier, несовместимый type и невозможный
FK не превращаются в компенсируемые штрафы. Даже нулевой настроенный penalty
не снимает blocker. Security report fingerprint сохраняет связь с approval.

Для table candidates D — среднее лучших column base scores по всем полям entity;
для relation candidates — минимум подтверждённых компонент. Это консервативно
для split: корректное предложение может требовать review из-за неполного evidence.
Group confidence — минимум выбранных обязательных choices, с 0 для пропуска.

`choice.action`, `group.action` и `result.action` явно задают threshold policy:

| Условие | Action |
|---|---|
| Score ≥ auto threshold (default 0.90), нет blockers/ambiguity | `auto` |
| Review threshold ≤ score < auto threshold (default 0.70–0.90) либо blocker при высоком score | `confirm` |
| Выбранный score < review threshold либо required source явно unmapped | `reject` |
| Неопределённый выбор (`ambiguous`, без target) | `confirm`, с confidence 0 |

Group/run сохраняют самый строгий action: reject, затем confirm, затем auto.
Оба неавтоматических исхода имеют `NEEDS_REVIEW`; reject запрещает принятие
текущего proposal, не удаляет ambiguity/evidence. `auto` относится только к proposal.
Default ambiguity margin=0.10.
`MULTIPLE_PLAUSIBLE_TARGETS` в choice либо assessment также включает ambiguity;
`INSUFFICIENT_EVIDENCE`, в том числе у альтернативы, сохраняет review.
`NO_MATCH` для выбранного target противоречит `selected` и не допускает auto.
Отрицательная оценка невыбранного target сама по себе review не требует.
Равенство scores, недостаточный gap, несогласие модели с SDK, непроверенный
competitor после pruning, явная model ambiguity или `review_required` сохраняют
`NEEDS_REVIEW`. При k=1 скрытый конкурент M9 не исчезает из проверки. Для хвоста
используется консервативная верхняя граница score с учётом округления.
SCC/self-reference сохраняет требование стратегии разрешения FK.

`COMPLETED` относится только к proposal; это не validation/approval `MappingPlan`.
`no_llm` возвращает исходные M9 candidates, `decision=None`, причину `LLM_DISABLED`
и `NEEDS_REVIEW`. Пустой target set также не вызывает scanner/provider.
Provider/policy/scan/schema failure возвращается typed exception без partial
результата; history фактических attempts остаётся в переданном router.
Unknown ID или inconsistent assignment → `SEMANTIC_MAPPING_DECISION_INVALID`;
malformed response → M6 `LLM_INVALID_RESPONSE` либо `LLM_SCHEMA_VIOLATION`.
Отмена распространяется без fallback. Превышение ресурсов → `MAPPING_LIMIT_EXCEEDED`
или соответствующий M6 budget/context/timeout code.

## Лимиты и проверки

Default/ceiling: groups 32/64; entity types 8/8, fields 32/32 и relation contexts
32/32 на группу; payload и response по 64/128 KiB; preparation work
100 000/1 000 000 операций; retained state 16/32 MiB; deadline 30/300 секунд.
Превышение размера связной компоненты не вызывает её молчаливое разбиение.
Router имеет отдельный общий call/token/deadline budget на все группы.

M10 проверяет абсолютный monotonic deadline после синхронных этапов, перед
scanner/provider и перед возвратом результата. Истёкший deadline подготовки даёт
`MappingError/MAPPING_LIMIT_EXCEEDED`; истечение общего deadline `propose` —
`LLMProviderError/LLM_TIMEOUT`, в том числе внутри подготовки. Частичный результат
не возвращается. Уже начатые provider attempts остаются в истории и расходуют
резерв; следующий вызов на том же mapper не получает новый бюджет router.
Эти проверки не прерывают синхронную Python-операцию посередине: лимиты work/state
действуют дополнительно к deadline, без обещания изоляции CPU отдельным процессом.

Поддержаны profile schema `1.0.0` и catalog schema `1.1.0` / `catalog-v1`.
`scope.deny` сильнее `scope.allow`; неизвестные refs и несовпадающие target/policy
bindings отклоняются. Hash проверяет согласованность snapshots, не происхождение
данных и не свежесть живой БД. Caller отвечает за доверенный источник catalog,
доступные ему targets и актуальность policy.

| Исключение | Условие и код |
|---|---|
| `MappingError` | Неверные snapshots/options: `MAPPING_INPUT_INVALID`, `MAPPING_UNSUPPORTED_SCHEMA`, `MAPPING_BINDING_MISMATCH`, `MAPPING_SEMANTIC_CATALOG_INVALID`; превышение лимитов: `MAPPING_LIMIT_EXCEEDED`; неверное решение: `SEMANTIC_MAPPING_DECISION_INVALID` |
| `DatabaseInspectionError` | Catalog не соответствует своему hash: `DATABASE_SCHEMA_DRIFT` |
| `LLMProviderError` | M6 policy/scan, budget/context, timeout, provider или schema failure; см. [коды и metadata M6](llm.md#scripted-outcomes) |
| `asyncio.CancelledError` | Отмена без partial result и fallback |
| `pydantic.ValidationError` | Создание некорректного DTO или ревалидация `context` в конструкторе; ошибка может содержать входные данные и не предназначена для обычного log |

Создание mapper не выполняет I/O. Одновременные `propose` на одном instance
запрещены (`LLM_BUDGET_EXCEEDED`). Каждый активный запрос проходит scanner и router;
он может расходовать бюджет и обращаться к разрешённому provider. Mapper не
управляет сроком хранения данных на стороне самого provider.

Default tests используют `FakeLLMProvider`. Общий provider contract проверяет
NoLLM и HTTP MockTransport native/JSON object modes. SQLite integration сравнивает
файл БД до и после M7→M8→M9→M10. Не проверяются live provider deployment и
пригодность реальных данных к загрузке: нужны будущие MappingPlan validation,
актуальный DB inspection, проверка grants/identity, staging и транзакция.

Калибровка confidence на production benchmark, общий SDK orchestration,
MappingPlan execution, проверка живых grants/parent rows, generated-key propagation,
staging и загрузка не входят в готовое M10. Split проверен только как proposal
на явно покрытых цепочках FK; неизвестные source anchors не восстанавливаются.

Подтверждение поведения: [матрица приёмки](plans/M10_acceptance.md),
[security review](plans/M10_security_review.md).
Долгоживущее решение об отдельном proposal и повторном использовании M6:
[ADR 0020](adr/0020-llm-semantic-mapping-proposals.md).
Канонические требования: [Database Semantic Catalog][spec-catalog],
[candidate generation][spec-candidates], [LLM semantic mapping][spec-mapping],
[confidence][spec-confidence], [роль LLM][spec-llm],
[prompt injection][spec-injection], [конфиденциальность][spec-private],
[маскирование][spec-masking] и [M10][spec-m10].
[Mapping Plan][spec-plan] задаёт границу следующего этапа, который здесь не исполняется.

[spec-catalog]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#10-database-semantic-catalog
[spec-candidates]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#118-candidate-generation
[spec-mapping]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#119-llm-semantic-mapping
[spec-confidence]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#12-оценка-достоверности
[spec-plan]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#14-mapping-plan
[spec-llm]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#195-роль-llm-в-db-mapping
[spec-injection]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#202-prompt-injection
[spec-private]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#2010-конфиденциальные-данные
[spec-masking]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#2011-маскирование
[spec-m10]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m10-llm-semantic-db-mapper
