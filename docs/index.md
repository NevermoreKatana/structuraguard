# StructuraGuard SDK

StructuraGuard — встраиваемая Python-библиотека для безопасного импорта
разнородных данных в существующие реляционные БД. Основной API асинхронный, а
sync API предоставляется отдельной facade.

## Доступность в M2

Milestone M2 сохраняет scaffold M1 и добавляет типизированную contract boundary
двухэтапного parsing. Публично доступны:

- immutable `SDKConfig`, который принимает только явные значения и не
  использует environment как источник config;
- `AsyncStructuraGuard` и отдельный `StructuraGuard`;
- typed hierarchy `StructuraGuardError`, включая
  `OperationNotImplementedError`;
- machine-readable поле `error_code`;
- frozen DTO физической модели `Extracted*`, декларативного `ParsePlan`,
  семантической модели `Normalized*`, `DatabaseCatalog`, `MappingPlan` и reports;
- девять adapter protocols в `structuraguard.ports`, включая `Parser`,
  `SemanticStructureAnalyzer`, `ParsePlanExecutor`, `DatabaseAdapter` и
  provider-neutral `LLMProvider`.

Обычный `import structuraguard` не загружает Pydantic: lazy top-level exports
подгружают `SDKConfig` и facade только при явном обращении к этим
символам. Код StructuraGuard не читает environment.

Concrete parsers, semantic services, DB/LLM adapters и orchestrator в M2 не
реализованы. Вызов
операции facade завершается контролируемой ошибкой
`SDK_OPERATION_NOT_IMPLEMENTED`; это не успешный placeholder. Sync-вызов внутри
активного event loop завершается `SYNC_API_IN_ASYNC_CONTEXT`.

Contracts доступны только через `structuraguard.contracts` и
`structuraguard.ports`; корневые exports M1 не расширены. Подробнее см.
[публичный API](public-api.md).

Минимальное создание и JSON round-trip физического DTO показаны в
[contract-only примере](public-api.md#m2-contract-copyable-example). Пример не
запускает parser или ingest.

## Навигация

- [Требования](requirements.md) задают нормативный scope и критерии приёмки.
- [Архитектура](architecture.md) фиксирует границы и направление зависимостей.
- [Публичный API](public-api.md) различает доступный scaffold и целевой contract.
- [Модель угроз](threat-model.md) описывает trust boundaries и controls.

## Проверки

После явной синхронизации окружения команды запускаются из корня репозитория:

```bash
make sync
make check
```

`make check` проверяет lockfile, lint, типы, тесты, документацию и сборку
wheel/sdist с установкой wheel в изолированное окружение.
