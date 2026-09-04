# Состояние проекта StructuraGuard

Обновлено: 2026-09-04.

## Текущая версия и milestone

- Версия package: `0.3.0`.
- Текущий milestone: `M3 — Parser Registry и безопасное discovery parser
  plugins`; локальная реализация проверена и готова к ручному commit и Pull
  Request в `main`.
- Активная ветка: `feat/m03-parser-registry`; изменения M3 находятся в
  рабочем дереве.
- Следующий планируемый milestone: `M4 — Базовые format parsers`.

Канонический scope: [M3 в техническом задании][spec-m3]. Реальные format
adapters, semantic services и pipeline следующих milestones не считаются
доступными.

## Состояние milestones

- `M0` — зафиксированы требования, архитектурный baseline и модель угроз.
- `M1` — создан устанавливаемый typed package с проверяемым scaffold.
- `M2` — реализация завершена: добавлены immutable domain contracts и protocols
  двухэтапного parsing.
- `M3` — реализация завершена: добавлены instance-local parser registry,
  deterministic selection и descriptor-only opt-in plugin discovery.
- `M4`–`M17` — не начаты.

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
- `structuraguard.parsers` экспортирует registry/snapshot/session и
  discovery contracts. Facades принимают `parser_registry` через dependency
  injection и возвращают его через `parsers`; default registry instance-local.
- Manual trusted parsers регистрируются явно. Selection использует strong
  content evidence, `confidence`, `priority` и canonical ID; MIME/extension
  conflicts становятся warning либо `PARSER_FORMAT_CONFLICT`.
- Entry points группы `structuraguard.parsers` обнаруживаются только явным
  вызовом с allowlist. Discovery не вызывает `EntryPoint.load()` и отражает
  ошибки отдельных distributions без прекращения обработки остальных.
- Public DTO используют strict frozen Pydantic contracts, tuple collections,
  tagged scalars, UTC datetime, `Decimal` для money и обязательную provenance.
  Persisted SHA-256 имеет единственную форму `sha256:<64 lowercase hex>`.
- Persisted aggregates/reports содержат schema и producer versions. LLM/security
  payload связан typed `SecurityApproval`, bounded canonical JSON и не принимает
  tools, credentials, handles, shell или SQL authority. `ValidationIssue`
  сохраняет `code`/`message_key` без free-form text; `LoadReport` именует target
  и всю execution fingerprint chain.

Concrete format parsers, semantic analyzer/validator/executor, DB reflection и
load, LLM providers, staging/audit backends, sandbox runner, state machine и
orchestrator не реализованы.

## Известные ограничения M3

Lineage-aware summaries и pure stream validation позволяют обнаруживать foreign
non-terminal batches, несовпадающие counts и глобальные duplicate normalized IDs
без реализации parser/executor adapter. `ExtractedSourceIndex` намеренно bounded:
он перечисляет только разрешённые sample/evidence/selectors, а не копирует каждый
physical объект большого источника.

Discovery валидирует только metadata allowlisted distributions и не загружает
plugin code. Filesystem metadata читаются через bounded FD-reader; unsafe path,
oversized файл, ZIP/custom provider и платформа без безопасного `dir_fd`
отклоняются. Host metadata finder и подмена artifact после discovery остаются
границами доверия до isolated runtime M12. Исполнение untrusted descriptor до
M12 запрещено: `activate_plugin()` завершается `SECURITY_SANDBOX_REQUIRED`.

Публичный parser contract возвращает базовый `AsyncIterator[ExtractedBatch]`.
Если trusted adapter владеет внешним ресурсом, его custom iterator должен сам
предоставить корректный `aclose()`; без close-hook SDK может перевести wrapper
в quarantined state, но не может принудительно освободить ресурс adapter.
Обязательный close-capable protocol требует отдельного изменения публичного
contract.

## Quality gates M3

- Итоговый `make check` — успешно; включает все перечисленные ниже локальные
  gates.
- `make sync` и lock check — успешно.
- `make lint` — Ruff для 73 файлов, успешно.
- `make typecheck` — strict mypy для 71 source files, успешно.
- `make test` — `830 passed` на Python 3.12.
- `make docs` — MkDocs strict, успешно.
- `make test-build` — собраны и проверены
  `structuraguard-0.3.0.tar.gz` и
  `structuraguard-0.3.0-py3-none-any.whl`; wheel повторно собран из sdist,
  установлен изолированно и прошёл black-box/attribution import probes.
- `git diff --check`, secret/debug scan и audit untracked/generated artifacts —
  успешно; build outputs и caches исключены через `.gitignore`.

## Открытые блокеры

Известных блокеров внутри scope M3 нет. Remote CI не запускался: он сможет
независимо подтвердить результат после публикации изменений в ветку/PR.

## Принятые архитектурные решения

- [ADR 0001](../adr/0001-public-api-and-run-policies.md) — async-first API,
  отдельный sync facade и run policies.
- [ADR 0002](../adr/0002-security-boundary-defaults.md) — fail-closed security
  defaults и запрет secrets в errors/audit.
- [ADR 0003](../adr/0003-two-stage-parsing-contracts.md) — technical parsing,
  semantic `ParsePlan`, checked execution и отдельный DB `MappingPlan`.

## Следующий рекомендуемый шаг

Подготовить план M4 для concrete format
parsers. Не добавлять semantic analyzer или orchestrator в M4 parser slice.

[spec-m3]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m3-parser-registry
