# ADR 0011 — Provider-neutral foundation и disabled LLM

Статус: принято для M6-A. Дата: 2026-09-10.

## Контекст

M2 уже задаёт `LLMProvider`, immutable request/response и payload-bound
`SecurityApproval`. Его capabilities требуют structured output и непустой purpose,
поэтому не позволяют честно представить `NoLLMProvider`. Prompt hash уже существует,
но version metadata отсутствует. Foundation должен проверяться без vendor SDK,
сети, случайных IDs и ожиданий по системным часам.

## Решение

- Сохранить port. Расширить capabilities явным execution environment, JSON Schema,
  tool-calling и optional token limits. Legacy unknown/default поля не добавляются
  в serialization. Только `disabled` допускает отсутствие generation capabilities,
  пустые purposes и нулевые byte limits; действующие providers не ослабляются.
- `NoLLMProvider` явно возвращает `LLM_POLICY_DENIED`. Не создавать пустой успешный
  output: caller может выбрать deterministic branch по disabled capabilities.
- Добавить `LLMPrompt` с ID/version/fingerprint и optional поле `prompt` в request
  и response. Hash связывается с прежним `prompt_fingerprint`. Новые adapters
  сохраняют metadata; fake требует её явно. Legacy DTO остаются читаемыми.
- Scripted fake реализует structured JSON, но не обещает JSON Schema validation.
  Malformed response становится typed error; допустимый injection text остаётся
  недоверенными inert data. Проверка semantic schema/provenance не принадлежит fake.
- Script и history ограничены. Счётчики расходуются до async checkpoint; clocks,
  simulated latency и request IDs задаёт caller. Cancellation не запускает retry.
  История и `LLMProviderError.call` сохраняют только typed metadata без raw payload.
- Общий contract suite проверяет внешние DTO и outcomes обоих adapters. Types
  сценариев принадлежат infrastructure `llm`, port о них не знает.

## Последствия

Zero-LLM flow имеет проверяемый отказ, а default tests полностью offline.
Новые optional поля сохраняют legacy fingerprints при их отсутствии, но старые
consumers могут отвергнуть новые wire payloads. Сериализация raw request/response
остаётся транспортной операцией, не безопасным logging API.

Foundation не создаёт доверие одним fingerprint и не заменяет scanner, router,
prompt catalog, semantic schema validator или ParsePlan validation. Эти компоненты,
как и реальные adapters, остаются последующими этапами M6.

Связанные документы: [ADR 0003](0003-two-stage-parsing-contracts.md),
[API foundation](../llm.md), [план M6](../plans/M06_llm_semantic_parsing.md).
