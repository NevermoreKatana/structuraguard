# ADR 0019 — Ограниченное детерминированное ранжирование кандидатов

Статус: принято для M9.

Основание: [M9 в каноническом ТЗ](https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m9-deterministic-db-mapper).

## Контекст

M7 возвращает catalog-v1 с FK graph, M8 — профиль завершённого normalized
stream. Нужен локальный mapper с объяснимыми сигналами и воспроизводимой
неоднозначностью. Имеющийся `MappingCandidate` связывает semantic field с
catalog column, но не содержит числовой breakdown и не разрешает загрузку.

## Решение

- Новый `CandidateMapper.rank` принимает готовые snapshots, явный `MappingScope`
  и optional `DatabaseSemanticCatalog`. Реализация `mapping.DeterministicMapper`
  не принимает connection/provider, не читает источники или environment и не
  создаёт `MappingPlan`, SQL, embeddings либо запросы LLM.
- Новые immutable result/explanation DTO дополняют старый `MappingCandidate`,
  не меняя его wire contract. `normalized_fingerprint` кандидата остаётся hash
  manifest; content/profile fingerprints сохраняются отдельно. Schema hash,
  scope и target policy — разные bindings, не проверка полномочий caller.
- Scope применяется до generation; generated columns могут оставаться graph
  metadata, но исключены из writable targets. Known mismatch исключает кандидата,
  unknown/conditional evidence блокирует auto recommendation. Description и
  comments не превращаются в исполняемые правила или автоматические aliases.
- Чистые функции `_names`, `_aliases`, `_compatibility`, `_scores` и `_ranking`
  отделены от async checkpoints. Все состояния, индексы, budgets и heaps
  принадлежат вызову. Domain не импортирует новый компонент или infrastructure.
- Используется полный bounded scan с двумя проходами: lexical/type anchors
  замораживаются перед source/FK context. Heap хранит `max(k, 2)` элементов,
  чтобы `k=1` не скрывал runner-up. Приблизительный shortlist и раннее pruning
  по неподтверждённой верхней границе не используются.
- Scoring — фиксированная сумма шести сигналов без перенормировки отсутствующего
  evidence. Exact/normalized/transliteration доступны отдельно как diagnostics.
  `Decimal` context локальный, precision 80, ROUND_HALF_EVEN; произведения
  округляются до 12 знаков, опубликованные scores/gaps — до 6. Versioned
  options, aliases и Unicode database включены в reproducibility metadata.
- Stable tie-breaking использует score, qualified names и catalog IDs.
  `ambiguous` сохраняется отдельно от recommendation status. Общие для source
  penalties не меняют порядок heap; hard policy/type violations не превращаются
  в штраф, который можно компенсировать другим сигналом.
- Все входы ограничиваются до serialization/revalidation/hash; ошибки не
  содержат payload. Budgets задают также ceilings, без silent truncation и
  частичного результата. Отмена проверяется между блоками; OS/RSS isolation
  и время исполнения в секундах этот компонент не гарантирует.

## Последствия

Прямой scan требует явного сужения больших схем. Fixed weights дают низкий
score sparse profiles; это измеряемый компромисс, а не доказательство отсутствия
подходящей колонки. Offline calibration отделена от runtime и holdout.
Применение предложенных weights остаётся явным действием владельца конфигурации.

FK graph отражает только структуру. Полное column-pair evidence не проверяет
значения FK, существование родителей, grants или возможность транзакции.
Identity hints не являются approval, циклы требуют будущей явной стратегии.
Перед загрузкой остаются обязательными отдельные validation, актуальный DB
inspection, staging, dry-run и transaction/rollback.

Связанные решения: [catalog и graph](0017-canonical-catalog-and-dependency-graph.md),
[normalized profiling](0018-bounded-normalized-profiling.md).
