# StructuraGuard SDK

StructuraGuard — встраиваемая Python-библиотека для безопасного импорта
разнородных данных в существующие реляционные БД. Основной API асинхронный, а
sync API предоставляется отдельной facade.

## Доступность в M1

Milestone M1 создаёт устанавливаемый typed package и инфраструктуру качества.
Публично доступны:

- immutable `SDKConfig`, который принимает только явные значения и не
  использует environment как источник config;
- `AsyncStructuraGuard` и отдельный `StructuraGuard`;
- typed hierarchy `StructuraGuardError`, включая
  `OperationNotImplementedError`;
- machine-readable поле `error_code`.

Обычный `import structuraguard` не загружает Pydantic: lazy top-level exports
подгружают `SDKConfig` и facade только при явном обращении к этим
символам. Код StructuraGuard не читает environment.

Pipeline, parsers, DB adapters и LLM adapters в M1 не реализованы. Вызов
операции facade завершается контролируемой ошибкой
`SDK_OPERATION_NOT_IMPLEMENTED`; это не успешный placeholder. Sync-вызов внутри
активного event loop завершается `SYNC_API_IN_ASYNC_CONTEXT`.

Исполнимый пример создания async facade и обработки этого typed failure приведён
в разделе [«Копируемый пример M1»](public-api.md#m1-copyable-example).

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
