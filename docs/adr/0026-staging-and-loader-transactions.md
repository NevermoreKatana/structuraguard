# ADR 0026 — PostgreSQL staging lifecycle и граница будущего loader

Статус: принято для реализации M13-A. Дата: 2026-09-13.

## Контекст

Существующий StagingStore имеет только stage(batch, context). Задача M13-A
требует durable run/batch/record metadata, retention references, явного bootstrap
и отсутствия DDL у runtime writer. Полный loader из
[плана M13](../plans/M13_staging_loader.md) остаётся последующим этапом.

## Решение

Добавить RunStagingStore поверх прежнего protocol, сохранив signature stage.
PostgreSQLStagingStore и MemoryStagingStore используют одинаковые правила
intake/lifecycle/retention и общий contract suite. Domain не зависит от adapters;
SQLAlchemy и asyncpg импортируются при операции, не при импорте SDK.

Сохранять references вместо копирования raw/normalized payload. Владелец artifacts
передаёт opaque IDs, fingerprints и обязательства retained_until; store их не
открывает и не удостоверяет. Набор категорий и retention задаются trusted policy
экземпляра. Batch/record fingerprints и ordinals связывают индекс с входом;
validation/mapping/provenance references не дают полномочий target write.

Фиксированная схема содержит schema_info, runs, batches, records. Данные mapping,
validation и provenance представлены refs, поэтому отдельные payload tables из
рекомендации §17 не создаются без потребности. Bootstrap — отдельная coroutine
с purpose=bootstrap и выделенным admin DSN; runtime её не вызывает. Разрешён
только зарезервированный staging namespace. Bootstrap не выдаёт grants и не
мигрирует существующие таблицы. Writer/maintenance обязаны не иметь production
DDL/owner privileges; проверка не заменяет административную настройку ролей.

Одна DB transaction на вызов; stage меняет run/batch/records атомарно.
Transaction advisory lock связывает namespace/target/run, CAS защищает переходы.
До schema check берутся table locks, исключающие подмену relations и несовместимый
DDL; владелец отдельно согласует migrations. Все values параметризованы,
identifiers принадлежат фиксированной схеме. Drivers очищаются при отмене,
неизвестный commit требует reconcile через get_run и идемпотентный повтор.

Cleanup выполняет отдельный maintenance principal. Terminal refs/index удаляются
после retention, а run tombstone остаётся. Брошенные OPEN/SEALED становятся EXPIRED
после deadline; EXECUTING/UNKNOWN автоматически не удаляются. Удаление внешних
payload и управление продлением artifact lease остаются у их владельца.

## Последствия и альтернативы

Хранение payload прямо в staging упростило бы автономный replay, но создало бы
дополнительные копии restricted данных и неоднозначность raw retention. В текущей
задаче выбран references-only контракт; он требует durable artifact owner.

Полная metadata run читается и проверяется в пределах конечных budgets. Это
простой baseline для небольших наборов; оптимизация incremental verification
возможна отдельно, без ослабления contract suite.

StagingRunStatus COMMITTED — декларация caller, не атомарное свидетельство
target commit. Execution plan, M12 projection gate, target writer, commit ledger,
transactional target audit, FK и loader quarantine не поставляются в M13-A.
Будущему loader потребуется отдельное evidence, а не доверие статусу staging.
Существующие LoadContext/LoadReport/M11/M12 и inspector execute-заглушка сохранены.

Подробный контракт и grants: [PostgreSQL staging](../staging.md).
