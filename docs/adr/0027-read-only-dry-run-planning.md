# ADR 0027 — PostgreSQL dry-run в одной read-only transaction

Статус: принято. Дата: 2026-09-13.

## Контекст

После staging M13-A требуется настоящий dry-run: нулевые изменения target и
существующего staging, ordered plan и раздельные прогнозы inserts/updates/skips/
quarantine. M11 проверяет структуру MappingPlan, а внешний M12 ValidationReport
сам по себе не связывает проверенные values с target projection. Grants отсутствуют
в catalog fingerprint. INSERT с rollback недостаточен: sequence не откатывается.

## Решение

Предоставить отдельный `PostgreSQLDryRunPlanner` с bounded immutable
`DryRunRequest` и trusted `DryRunPolicy`. Он не принимает writer credentials,
staging или audit adapter. Snapshot проецируется в памяти без изменения artifacts;
M11 и M12 DB constraints повторяются. Внешний ValidationReport не даёт разрешения
ни пропустить проверки, ни начать write. Copy projection поддержана; изменения
values требуют physical replay и пока отклоняются.

Одна inspector transaction SERIALIZABLE READ ONLY включает locks, reflection,
schema recheck, отдельные writer grants, UNIQUE/FK lookups и upsert classification.
SQL создаётся adapter, значения bind-параметризованы, identifiers разрешаются по
свежему catalog и trusted scope. Lookup возвращает только booleans. Никакого DML,
DDL, default evaluation, nextval, stage lifecycle или автоматического audit.
Cancellation/timeout всегда закрывают transaction до передачи управления caller.

Counts описывают target units текущего snapshot: UPDATE при найденной upsert
identity и mutable columns; SKIP только при отсутствии mutable columns. Конфликт
insert_only — ошибка данных, не skip. Atomic блокирует весь план; quarantine
исключает закрытые ошибки records и зависимые группы. Unknown semantics не дают
ready даже в quarantine mode. Plan содержит lineage, зависимости и fingerprints,
но не SQL/значения. Для logs предусмотрен counts-only safe_summary.

## Последствия

Это уточняет раздел B [плана M13](../plans/M13_staging_loader.md): вместо единого
would_load доступны прогнозы INSERT/UPDATE по реальному read snapshot. Отдельный
MemoryStagingStore не нужен для immutable tuple: его роль в этом вызове выполняет
bounded проекция в памяти. Persistent staging остаётся неизменным.

Результат advisory, не executable authorization: будущий writer повторяет проверки
и устраняет TOCTOU собственной transaction. Stateful ingest facade, полный M12
physical/business/schema coordinator, execution/commit ledger и target write не
входят в этот API. Неизвестные defaults/generated keys/CHECK/collations, RLS,
triggers, expression indexes и mutable alternative keys явно блокируются, а не
считаются прошедшими проверку. Подробности: [контракт dry-run](../dry_run.md).
