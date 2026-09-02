# Состояние проекта StructuraGuard

Обновлено: 2026-09-02.

## Текущая версия и milestone

- Версия package: `0.1.0`.
- Текущий milestone: `M1 — Каркас Python-пакета`; локальные gates прошли,
  remote rerun packaging fix ожидается.
- Активная ветка: `feat/m01-sdk-scaffold`.
- Следующий milestone: `M2 — Доменные модели и contracts`.

Канонический scope: [M1 в техническом задании][spec-m1]. Подробное целевое API
следующих milestone остаётся в [разделе 23 ТЗ][spec-public-api] и не считается
реализованным.

## Завершённые milestones

- `M0` — зафиксированы требования, архитектурный baseline и модель угроз.
- `M1` — создан устанавливаемый typed package с проверяемым scaffold.
- `M2`–`M14` — не начаты.

## Реализованные публичные contracts

- `import structuraguard` имеет ленивые top-level exports и не выполняет I/O,
  не читает environment и не меняет process-wide state.
- `SDKConfig` в M1 пуст, immutable, принимает только явные значения и отклоняет
  неизвестные поля.
- `AsyncStructuraGuard` является основным facade; `StructuraGuard` — отдельная
  sync-оболочка. Config хранится на instance level.
- Все восемь имён pipeline-операций являются только fail-loud scaffold и
  возбуждают `SDK_OPERATION_NOT_IMPLEMENTED`.
- Sync-вызов внутри активного event loop возбуждает
  `SYNC_API_IN_ASYNC_CONTEXT` до делегирования.
- Публичная иерархия исключений использует `error_code`, ограниченные immutable
  `details`, санитизированный type-only `cause` и скрывает неявный exception
  context из стандартного traceback. Alias `code` отсутствует.
- Distribution использует src-layout, Python 3.12+, marker `py.typed` и только
  Pydantic v2 как unconditional runtime dependency.

Parsers, DB inspection/load, MappingPlan, validation pipeline, LLM adapters и
успешный ingest-сценарий не реализованы. Optional extras резервируют только
dependency bundles и не означают наличие adapters.

## Последние успешные quality gates

- `make check`: Ruff, mypy strict, `91 passed` на Python 3.12, MkDocs strict,
  wheel/sdist build, rebuild wheel из sdist и installed-wheel smoke — успешно.
- Изолированный pytest на Python 3.13: `91 passed`.
- Изолированный pytest на Python 3.14: `91 passed`.
- Копируемый пример M1 выполняется в subprocess и выводит
  `SDK_OPERATION_NOT_IMPLEMENTED`.

Initial remote package job выявил, что name-based `uv pip install --offline`
зависел от registry-index cache, который locked sync не обязан
заполнять. Verifier переведён на lock-native offline sync; fix прошёл
полный verifier в чистом cache. Remote rerun новой revision ожидается.

## Открытые блокеры

Локальных блокеров нет. Перед merge нужен успешный remote rerun
packaging fix.

## Принятые архитектурные решения

- [ADR 0001](../adr/0001-public-api-and-run-policies.md) — async-first API,
  отдельный sync facade и run policies.
- [ADR 0002](../adr/0002-security-boundary-defaults.md) — fail-closed security
  defaults и запрет secrets в errors/audit.

Отдельный ADR для M1 не добавлен: src-layout, lazy exports, пустой explicit
config и fail-loud scaffold реализуют уже утверждённые требования, не вводя
нового долгоживущего архитектурного выбора.

## Следующий рекомендуемый шаг

Сначала подготовить исполнимый план M2 по [каноническому разделу M2][spec-m2]:
зафиксировать public DTO/contracts и их invariants до реализации parser, DB или
LLM vertical slices.

[spec-m1]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m1-каркас-python-пакета
[spec-m2]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m2-доменные-модели-и-contracts
[spec-public-api]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#23-публичный-api-sdk
