# Состояние проекта StructuraGuard

Обновлено: 2026-09-03.

## Текущая версия и milestone

- Версия package: `0.2.0`.
- Текущий milestone: `M2 — Доменные модели и contracts`; локальная реализация
  завершена, но финальная приёмка заблокирована двумя High security findings в
  validation/report contracts.
- Активная ветка: `main`; изменения M2 находятся в рабочем дереве.
- Следующий планируемый milestone после закрытия M2: `M3 — Parser Registry и
  плагины`.

Канонический scope: [M2 в техническом задании][spec-m2]. Реализации adapters,
pipeline и facade следующих milestone не считаются доступными.

## Состояние milestones

- `M0` — зафиксированы требования, архитектурный baseline и модель угроз.
- `M1` — создан устанавливаемый typed package с проверяемым scaffold.
- `M2` — реализация завершена: добавлены immutable domain contracts и protocols
  двухэтапного parsing; перед commit остаются два security blockers, которых не
  обнаруживает текущий зелёный suite.
- `M3`–`M17` — не начаты.

## Реализованные публичные contracts

- Корневой `structuraguard.__all__` сохраняет ровно 12 lazy exports M1; facade
  по-прежнему fail-loud и не изображает реализованный pipeline.
- `structuraguard.contracts` экспортирует frozen DTO physical `Extracted*`,
  discriminated `ParsePlan`, semantic `Normalized*`, `DatabaseCatalog`,
  target-bound `MappingPlan`, checked-plan wrappers, reports и audit events.
- `structuraguard.ports` экспортирует ровно девять protocols: `Parser`,
  `SemanticStructureAnalyzer`, `ParsePlanValidator`, `ParsePlanExecutor`,
  `DatabaseAdapter`, `LLMProvider`, `SecurityScanner`, `StagingStore` и
  `AuditStore`.
- `Parser` возвращает только raw physical structure; semantic values возникают
  только после применения `ValidatedParsePlan`. DB execution принимает только
  `ValidatedMappingPlan`, связанный с catalog/target/policy fingerprints.
- Public DTO используют strict frozen Pydantic contracts, tuple collections,
  tagged scalars, UTC datetime, `Decimal` для money и обязательную provenance.
  Persisted SHA-256 имеет единственную форму `sha256:<64 lowercase hex>`.
- Persisted aggregates/reports содержат schema и producer versions. LLM/security
  payload связан typed `SecurityApproval`, bounded canonical JSON и не принимает
  tools, credentials, handles, shell или SQL authority. `ValidationIssue`
  сохраняет `code`/`message_key` без free-form text; `LoadReport` именует target
  и всю execution fingerprint chain.

Concrete format parsers, semantic analyzer/validator/executor, DB reflection и
load, LLM providers, staging/audit backends, state machine и orchestrator не
реализованы.

## Известное ограничение M2

Lineage-aware summaries и pure stream validation позволяют обнаруживать foreign
non-terminal batches, несовпадающие counts и глобальные duplicate normalized IDs
без реализации parser/executor adapter. `ExtractedSourceIndex` намеренно bounded:
он перечисляет только разрешённые sample/evidence/selectors, а не копирует каждый
physical объект большого источника.

## Последние успешные quality gates

- `make check`: lock check, Ruff для 53 файлов, strict mypy для 51 файла,
  `600 passed` на Python 3.12, MkDocs strict — успешно.
- Собраны `structuraguard-0.2.0.tar.gz` и
  `structuraguard-0.2.0-py3-none-any.whl`; wheel повторно собран из sdist.
- Installed-wheel black-box/attribution smoke подтвердил root/contracts/ports
  exports, import-safety, отсутствие optional/infrastructure imports и утечки
  source checkout.

## Открытые блокеры

- `IssueCodeStr` пропускает credential-like значения в `ValidationIssue`, откуда
  они сериализуются в reports/audit.
- Redacted `ValidationError` сохраняет исходную ошибку с raw input в
  `__context__` при constructor и union validation failures.

Оба finding требуют security regression tests и локального исправления до
ручного commit. Remote CI не запускался: он сможет независимо подтвердить
результат после публикации исправлений в ветку/PR.

## Принятые архитектурные решения

- [ADR 0001](../adr/0001-public-api-and-run-policies.md) — async-first API,
  отдельный sync facade и run policies.
- [ADR 0002](../adr/0002-security-boundary-defaults.md) — fail-closed security
  defaults и запрет secrets в errors/audit.
- [ADR 0003](../adr/0003-two-stage-parsing-contracts.md) — technical parsing,
  semantic `ParsePlan`, checked execution и отдельный DB `MappingPlan`.

## Следующий рекомендуемый шаг

Исправить два security blockers M2, повторить review и quality gates, затем
подготовить исполнимый план M3 по [каноническому разделу M3][spec-m3], не
расширяя M2 реализациями adapters.

[spec-m2]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m2-доменные-модели-и-contracts
[spec-m3]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m3-parser-registry-и-плагины
