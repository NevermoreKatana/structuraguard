# StructuraGuard SDK

StructuraGuard — встраиваемая Python-библиотека для безопасного импорта
разнородных данных в существующие реляционные БД. Основной API асинхронный, а
sync API предоставляется отдельной facade.

## Доступность в M4

M4 добавляет независимые technical parsers к contracts M2 и instance-local
registry M3. Начните с [копируемого офлайн-примера физического извлечения](
public-api.md#m4-extraction-copyable-example). Публично доступны:

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
  provider-neutral `LLMProvider`;
- `ParserRegistry` и связанные selection/discovery contracts через
  `structuraguard.parsers`;
- явная ручная регистрация trusted parser objects, воспроизводимый выбор по
  content evidence, `confidence`, `priority` и canonical `adapter_id`;
- безопасное opt-in discovery декларативных entry-point descriptors группы
  `structuraguard.parsers` без загрузки plugin code в host process;
- явно регистрируемые TXT/LOG/MD, CSV/TSV, JSON/JSONL/NDJSON, XML/HTML/YAML,
  XLSX, text-layer PDF и DOCX adapters в `structuraguard.parsers.builtin`;
- отдельный optional `TikaParserAdapter`, выключенный по умолчанию, с явным
  endpoint, source-bound egress approval и изоляцией сервера силами caller.

Обычный `import structuraguard` не загружает Pydantic: lazy top-level exports
подгружают `SDKConfig` и facade только при явном обращении к этим
символам. Код StructuraGuard не читает environment.

Format adapters возвращают физические `ExtractedBatch`, не окончательные
бизнес-сущности. Semantic services, DB/LLM adapters и orchestrator не
реализованы. Вызов
операции facade завершается контролируемой ошибкой
`SDK_OPERATION_NOT_IMPLEMENTED`; это не успешный placeholder. Sync-вызов внутри
активного event loop завершается `SYNC_API_IN_ASYNC_CONTEXT`.

Contracts доступны через `structuraguard.contracts`, `structuraguard.ports` и
`structuraguard.parsers`; корневые exports M1 не расширены. Создание facade и
registry не сканирует installed distributions: discovery начинается только по
явному вызову с allowlist policy. Подробнее см.
[публичный API](public-api.md#m4-technical-parsers).

Extra требуется только выбранному backend; установка не активирует adapters.
Document workers поддерживают Linux/macOS и не являются sandbox; strict mode
отказывает до реализации M12. OCR отсутствует. Полная приёмка M4 ещё не закрыта:
остаточные проверки и фактическое evidence перечислены в
[аудите M4](plans/M04_acceptance_audit.md) и
[состоянии проекта](codex/PROJECT_STATE.md).

Копируемые сценарии M3 показаны в примерах
[manual registration и selection](public-api.md#m3-registry-copyable-example) и
[descriptor-only discovery](public-api.md#m3-discovery-copyable-example). Они не
реализуют реальный format parser, не активируют plugin и не запускают ingest.

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
