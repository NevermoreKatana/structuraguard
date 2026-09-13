# ADR 0028 — Atomic PostgreSQL insert_only/upsert из sealed staging

Статус: принято. Дата: 2026-09-13.

## Контекст

После M13-A staging и M13-B dry-run требуется реальная запись: confirmed keys,
composite FK, порядок dependency graph и bulk без дубликатов/пропусков. Inspector
не должен получить writer credentials. Сохранённый plan не подтверждает grants
или текущее состояние target. Staging metadata не атомарна с target commit.

## Решение

Добавить явный PostgreSQLLoader, PostgreSQLWriterTarget и atomic LoadPolicy.
Использовать существующую bounded copy projection и M11/M12 checks, повторяя их
на свежем catalog в writer connection. Перед DML сопоставлять supplied snapshot
с полным sealed staging index и claims через revision CAS. Bootstrap остаётся
отдельным действием администратора. Writer API не принимает SQL/parameters извне.

READ COMMITTED transaction удерживает SHARE ROW EXCLUSIVE на trusted write tables
и ACCESS SHARE на lookup-only tables. Такой baseline уменьшает параллелизм,
зато фиксирует набор target rows между precheck и UPSERT. Lookup races разрешает
реальный FK. Scope/grants/runtime metadata проверяются до DML и перед COMMIT.
Writer не владеет объектами БД, не имеет DDL и отличается от inspector.

SQLAlchemy Core формирует параметризованные multi-row INSERT и ON CONFLICT по
полной identity из M11. Группы различают missing/NULL и ограничены rows/bytes/
bind count. RETURNING константы подтверждает отсутствие пропущенных units.
Generated columns не записываются. Server evaluation существующего non-key
stored generated/default допускается только trusted permission, связанным с
точной metadata; generated-key propagation и domain defaults не добавляются.

Mapped_parent проверяется по реальным record/entity links и точным values.
Co-occurrence M8 не заменяет такую привязку. Source-values/lookup поддерживают
только exact ordered FK keys, без natural-to-surrogate преобразования.

## Последствия

Ошибка до commit откатывает весь run; отсутствуют промежуточные commits и
last-write-wins. При lost COMMIT — UNKNOWN и запрет replay. При известном commit
cleanup/finalize failure даёт warning с committed counts. Staging CAS/finalize
использует отдельные transactions и не объявляется commit ledger.

M13-D реализует quarantine writer, transactional audit и durable reconciliation
в [ADR 0029](0029-loader-ledger-and-quarantine.md).
Текущий caller обязан отдельно обеспечить business/schema policy и подлинность
physical artifacts; DB primitive не подменяет полный ingest coordinator.
Unknown semantics остаются blockers. Dry-run не получает writer engine/staging
и сохраняет прежний read-only контракт. Legacy inspector execute не изменён.

[Публичный контракт и тесты](../loader.md), [план M13](../plans/M13_staging_loader.md).
