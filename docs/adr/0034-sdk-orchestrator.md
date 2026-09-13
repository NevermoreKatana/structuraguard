# ADR 0034: SDK orchestrator и явное владение run

Статус: принято для M15. Дата: 2026-09-13.

## Контекст

M3–M14 предоставляют самостоятельные contracts/adapters. Общие SDK operations
оставались stubs. Требуется source→report с теми же gates и строгими states.
Каноническая граница — [M15 ТЗ][spec-m15];
[§24][spec-result] требует общий IngestResult для полного и частичного результата.

## Решение

Добавить `pipeline` как composition над существующими ports. Runtime DTO
`SourceAnalysis` и `NormalizedData` содержат конечный snapshot и явный source lease.
Один `RunSession` владеет state, deadline, безопасными events и reports. Constructor
принимает `SDKDependencies`; instance-local registry сохраняется. `domain/contracts`
не импортируют coordinator. Парсеры/LLM/БД не подменяются новыми реализациями.

`IngestResult` schema 1.0.0 — композиция существующих reports с nullable artifacts
невыполненных этапов. Он содержит safe codes, transaction outcome и audit references.
Полный JSON чувствителен; для logs существует `safe_summary`. Пошаговые failures
возвращаются как `PipelineError.result`; конечные операции — как `IngestResult`.
Cancellation сохраняет стандартный `CancelledError`.

Состояния проверяются явно. M11 предшествует constraint reads и load; M13 повторяет
проверки внутри transaction. Dry-run не вызывает staging writer. Timeout оборачивает
отменяемые этапы, а loader получает resource guard напрямую: известный commit
нельзя заменить FAILED из-за запоздалого timer/hook. Ошибка observers после commit
оставляет COMPLETED_WITH_WARNINGS. SDK не делает автоматических DB retries.

Identity source зависит от полного source fingerprint. ParsePlan допускает reuse
после replay. MappingPlan требует полного normalized fingerprint; существующий M5
включает run identity в record IDs. Поэтому одинаковые bytes сами по себе не дают
cross-run MappingPlan reuse. Сохраняем fail-closed mismatch, не переписываем план,
не изменяем M5 IDs и wire contracts ради скрытого rebind.

Source transport, base DLP scanner, provider lifecycle, persistent artifacts,
OS sandbox backend, bootstrap и durable audit delivery принадлежат host. M15
принимает существующий SourceStream. Live load требует реальных retained references;
memory staging не заменяет artifact store. Unverified normalization/semantic
ambiguity останавливают flow до target side effects.

## Последствия

Public signatures stubs теперь типизированы; aliases относятся к mapping API,
`propose_schema` остаётся unsupported. Async API основной; sync context сохраняет
один Runner, rejects nested loops и закрывает source leases. Новых production
зависимостей, environment reads при import, глобальных registries и DDL нет.

Bounded copy-only E2E не означает выполнения всех критериев полного ТЗ:
streaming spill, cross-run resume, generated PK, производственные host adapters
и автоматическая redaction сложного payload остаются отдельными задачами.
При невозможности безопасного egress система отказывает, а не отправляет raw
данные по запасному маршруту.

[spec-m15]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m15-sdk-orchestrator
[spec-result]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#24-результат-работы
