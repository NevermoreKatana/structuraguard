# StructuraGuard SDK

StructuraGuard — встраиваемая Python-библиотека для безопасного импорта
разнородных данных в существующие реляционные БД. Основной API асинхронный, а
sync API предоставляется отдельной facade.

## Доступность в M6

M6 добавляет provider-neutral semantic parsing поверх M4 extraction и M5
profiling/validation/execution. Начните с [копируемого offline-примера
CSV → semantic records](semantic-parsing.md#m06-offline-example).
Режим по умолчанию — `llm_assisted`; explicit `deterministic` запрещает LLM calls.
Публично доступны:

- immutable `SDKConfig`, который принимает только явные значения и не
  использует environment как источник config;
- `AsyncStructuraGuard` и отдельный `StructuraGuard`;
- typed hierarchy `StructuraGuardError`, включая
  `OperationNotImplementedError`;
- machine-readable поле `error_code`;
- frozen DTO физической модели `Extracted*`, декларативного `ParsePlan`,
  семантической модели `Normalized*`, `DatabaseCatalog`, `MappingPlan` и reports;
- десять adapter protocols в `structuraguard.ports`, включая `Parser`,
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
  endpoint, source-bound egress approval и изоляцией сервера силами caller;
- `StructuralProfiler`, `DeterministicStructureAnalyzer`, `ParsePlanValidator`
  и `ParsePlanExecutor` через `structuraguard.structure`, с bounded options,
  evidence, явной неоднозначностью и physical provenance;
- `FakeLLMProvider`, `NoLLMProvider`, optional `OpenAICompatibleProvider` и
  отдельный `PolicyAwareLLMRouter` через `structuraguard.llm`;
- `LLMStructureAnalyzer` и `HybridStructureAnalyzer`, bounded document extraction
  с exact source spans, `SemanticParsingSession` и `SemanticParseReport`.

Обычный `import structuraguard` не загружает Pydantic: lazy top-level exports
подгружают `SDKConfig` и facade только при явном обращении к этим
символам. Код StructuraGuard не читает environment.

Format adapters возвращают физические `ExtractedBatch`, не окончательные
бизнес-сущности. Executor M5 создаёт normalized records/entities, но business
meaning остаётся `unresolved`. M6 дополняет его проверяемыми semantic proposals;
ненулевые значения сохраняют provenance, неоднозначность требует `NEEDS_REVIEW`.
DB reflection/load и общая ingest facade не реализованы. Вызов операции facade
завершается контролируемой ошибкой
`SDK_OPERATION_NOT_IMPLEMENTED`; это не успешный placeholder. Sync-вызов внутри
активного event loop завершается `SYNC_API_IN_ASYNC_CONTEXT`.

Contracts доступны через `structuraguard.contracts`, `structuraguard.ports` и
`structuraguard.parsers`; сервисы M5/M6 — через `structuraguard.structure`,
`structuraguard.llm` и `structuraguard.parsing`.
Корневые exports M1 не расширены. Создание facade и
registry не сканирует installed distributions: discovery начинается только по
явному вызову с allowlist policy. Подробнее см.
[публичный API](public-api.md#m4-technical-parsers).

Extra требуется только выбранному backend; установка не активирует adapters.
Document workers поддерживают Linux/macOS и не являются sandbox; strict mode
отказывает до реализации M12. OCR отсутствует. Полная приёмка M4 ещё не закрыта:
остаточные проверки и фактическое evidence перечислены в
[аудите M4](plans/M04_acceptance_audit.md) и
[состоянии проекта](codex/PROJECT_STATE.md).

M5 поддерживает закрытую policy четырёх семейств; это не произвольные expressions,
semantic conversions или автоматический выбор неоднозначной структуры. Границы:
[приёмка M5](plans/M05_acceptance.md) и [security semantics](security.md).

M6 также имеет ограниченный scope: production PII scanner/redaction, analyzer
registry, безопасный aggregate report целиком и router/session composition
не реализованы. [Матрица M6](plans/M06_acceptance.md) фиксирует частичные K5/K7;
[security review M6](plans/M06_security_review.md) — проверенные угрозы и ограничения.
LLM получает только bounded approved payload; tools, БД и filesystem ей недоступны.

Копируемые сценарии M3 показаны в примерах
[manual registration и selection](public-api.md#m3-registry-copyable-example) и
[descriptor-only discovery](public-api.md#m3-discovery-copyable-example). Они не
реализуют реальный format parser, не активируют plugin и не запускают ingest.

## Навигация

- [Требования](requirements.md) задают нормативный scope и критерии приёмки.
- [Архитектура](architecture.md) фиксирует границы и направление зависимостей.
- [Публичный API](public-api.md) различает доступный scaffold и целевой contract.
- [Структурный разбор M5](structure.md) показывает исполняемый пример и ограничения.
- [Модель угроз](threat-model.md) описывает trust boundaries и controls.

## Проверки

После явной синхронизации окружения команды запускаются из корня репозитория:

```bash
make sync
make check
```

`make check` проверяет lockfile, lint, типы, тесты, документацию и сборку
wheel/sdist с установкой wheel в изолированное окружение.
