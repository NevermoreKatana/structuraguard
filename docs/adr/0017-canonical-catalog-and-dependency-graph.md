# ADR 0017: canonical catalog-v1 и FK dependency graph

Статус: принято для M7-C. Дата: 2026-09-10.

Fingerprint вычисляется по явной versioned projection `catalog-v1`, а не по
полному wire DTO. Qualified имена разрешают ссылки независимо от opaque IDs.
Schemas, objects, columns и наборы constraints/indexes сортируются; ordinal
position сохраняется отдельно, порядок компонентов ключей и enum labels —
семантический. Native types, default/generated/identity, nullable, constraints,
indexes, comments и capabilities входят в hash. Comments влияют на mapping.
Target, policy, producer, display name, permissions, writable policy, derived
graph и служебные IDs не входят. Credentials/rows/statistics в каталоге нет.
Имена автоматически созданных SQLite indexes исключаются из projection; их
содержание и origin сохраняются. Эквивалентность произвольных SQL expressions
не доказывается: текст выражений сохраняется, hash разных dialects может отличаться.

Граф содержит все catalog objects и отдельные FK edges parent→child с ordered
column pairs. Для DAG вычисляется стабильный topological order; load order
содержит только структурно writable tables. Порядок не подтверждает grants и
не разрешает загрузку. Циклы представлены strongly connected components,
включая self-reference и внутренние FK. Обход итеративный, число простых циклов
не перечисляется. При любом цикле оба порядка отсутствуют и требуется явная
стратегия; SDK не отключает constraints и не выбирает deferral автоматически.

Join-table candidate требует ровно двух FK к разным внешним таблицам,
непересекающихся non-null/generated-free локальных колонок, покрытия всех
колонок и PK либо безусловного unique key их объединением. Payload/surrogate
колонки, nullable endpoints, self-links и только совпадение имени не дают hint.
Результат — evidence о структуре, а не утверждение бизнес-смысла.

Новые `inspect` обоих adapters возвращают `DatabaseCatalog` schema `1.1.0`
с `fingerprint_version`, capabilities и graph после cleanup. Legacy `1.0.0`
wire format сохраняется. `inspect_metadata` остаётся доступен; writer `execute`
явно сообщает `SDK_OPERATION_NOT_IMPLEMENTED` до I/O и чтения batches.
Перед будущим load fingerprint нужно вычислить заново; несовпадение даёт
`DATABASE_SCHEMA_DRIFT`. Fingerprint не заменяет target/policy binding или
защиту TOCTOU. SDK facade, loader и стратегии разрешения циклов сюда не входят.


Domain algorithms используют только contracts, общий модуль `exceptions` и
stdlib (`collections`, `heapq`, `hashlib`, `typing`). `exceptions` — общий
контракт ошибок, без DB adapters, facade и I/O; его зависимости отдельно
проверяются boundary test. Это уточняет исходный M02 allowlist, ограниченный
прежними hash helpers. Запрет infrastructure/network/process imports в domain
и import-side-effect probes сохраняется. Сравнение schema hashes обычное:
это identity metadata, а не проверка секрета или полномочий.
