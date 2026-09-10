# ADR 0015. Ограниченный SQLite metadata inspection

Статус: принято для M7-A.
Дата: 2026-09-10.

## Контекст

Существующий `DatabaseAdapter.inspect` требует fingerprint-bound
`DatabaseCatalog`. Часть A [плана M7](../plans/M07_database_inspector.md)
создаёт SQLite metadata snapshot; утверждение fingerprint format и FK graph
относятся к C. Нельзя выдавать фиктивный hash для соблюдения port signature.
M2 DTO также не описывали native types, defaults, indexes и generated expressions.

## Решение

- `SQLiteDatabaseAdapter.inspect_metadata` возвращает
  `DatabaseMetadataSnapshot` schema `1.1.0`. Он использует существующие
  `SchemaCatalog`/`TableCatalog`/`ColumnCatalog`/`ForeignKeyCatalog` и не выдаёт
  себя за реализацию полного `DatabaseAdapter` port. SDK facade не меняется.
- Новые optional `inspection` поля содержат типизированные metadata. При `None`
  они исключены из serialization, поэтому legacy wire representation сохраняется.
  Legacy `DatabaseCatalog` пока отвергает эти расширения, чтобы snapshot не
  использовался с непроверенным fingerprint. Полный version gate вводится в C.
- `DatabaseType` сохраняет native text и canonical family с параметрами.
  SQLite affinity хранится отдельно от canonical type; declared type не означает
  строгую типизацию значений. Неизвестные типы остаются `unknown`.
- Trusted immutable `SQLiteTarget` явно задаёт absolute file path, target ID,
  allowlist/denylist и budgets. Scope/request/policy fingerprint проверяются до
  открытия файла. Только `main`; deny имеет приоритет с учётом ASCII case rules
  SQLite. Самостоятельно расширять scope по FK запрещено.
- Отдельный worker открывает существующий файл `mode=ro`, включает `query_only`
  и начинает read transaction. DBAPI authorizer дополнительно запрещает DDL,
  DML, SELECT пользовательских строк, ATTACH и небезопасные PRAGMA/functions.
  SQLAlchemy core используется для bounded catalog queries и quoting identifiers.
  Полная reflection и исполнение текста из `sqlite_schema` не применяются.
- PK/FK/index metadata берутся из PRAGMA. Ограниченный lexical parser выделяет
  только границы CHECK/generated/index expressions, учитывая quotes, comments
  и вложенные скобки. Он не интерпретирует выражения. Это исключает тихую потерю
  нескольких CHECK или expression indexes при неполной Inspector reflection.
- Views отражаются через metadata PRAGMA, без SELECT из view, без выдуманных
  keys и с `writable=False`. Computed columns также не writable; default,
  rowid alias и explicit AUTOINCREMENT представлены отдельно.
- По окончании отражения FK разрешаются только внутри разрешённого snapshot,
  с сохранением порядка composite keys. Внешний FK приводит к
  `DATABASE_CATALOG_SCOPE_INCOMPLETE`, а не к удалению связи или новой reflection.
- Сортировка объектов и IDs детерминированы. IDs хешируют structured qualified
  paths без зависимости от reflection order. Это не schema fingerprint:
  `catalog-v1`, schema drift и graph algorithms остаются частью C.
- Timeouts охватывают worker, statements и lock waits. При cancellation SQLite
  прерывается и worker закрывает connection. При неуспешном bounded cleanup
  instance становится недоступен для повторного inspection. Логи engine/pool
  изолированы локальным logger без изменения application logging configuration.

## Альтернативы и последствия

Broad reflection с последующим фильтром отклонена: она читала бы запрещённые
объекты. Column-level filtering полного SQLite DDL небезопасен, поэтому column
selectors/denylist в A дают explicit unsupported до I/O. Внешние FK stubs требуют
нового closed-graph contract и отложены. Nullable SQLite PK нельзя исправлять
на NOT NULL ради DTO: такой schema получает explicit unsupported.

Virtual tables, attached/temp schemas, multi-line/control metadata и поля,
не представимые без потери в DTO, не получают успешный snapshot. SQLite не
предоставляет catalog comments. Read-only проверяет logical schema/data mutation,
а не неизменность служебных файлов SQLite/OS во всех WAL deployments.
SQLite внутренне загружает собственный schema cache: allowlist ограничивает
запросы адаптера, а не внутренние чтения страниц движком.

SQLAlchemy уже является optional dependency проекта; добавлены `sqlite` extra
и dev installation той же locked версии 2.0.52 (MIT). PostgreSQL behavior и
Testcontainers не проверяются SQLite и относятся к B. Ни loader, ни grant
discovery writer, ни запуск SQL из metadata не входят в A.
