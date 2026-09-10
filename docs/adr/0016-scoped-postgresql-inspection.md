# ADR 0016: ограниченная PostgreSQL reflection через SQLAlchemy

Статус: принято для M7-B. Дата: 2026-09-10.

## Контекст

Inspector должен соблюдать schema/table allowlist до reflection, работать без
SELECT grants на пользовательские строки и возвращать bounded metadata snapshot.
План B первоначально предлагал `AsyncConnection.run_sync(Inspector)`. В установленном
SQLAlchemy 2.0.52 `PGDialect.get_multi_columns()` вызывает `_load_domains` и
`_load_enums` с `schema="*"` даже для одной явно выбранной таблицы. Фильтрация
готового результата не защищает от такого чтения metadata вне scope.

## Решение

`PostgreSQLDatabaseAdapter.inspect_metadata` использует SQLAlchemy 2.x async Core
с asyncpg и фиксированными SELECT к `pg_catalog`. Schema/table names передаются
bind parameters; после точечного разрешения имён запросы используют OID только
внутри текущего snapshot. OID не входит в stable IDs. Перечисления всех объектов
БД нет. Enum/domain/array отражаются только как типы разрешённых столбцов;
пользовательские types должны принадлежать разрешённым схемам. FK вне scope
завершает inspection с `DATABASE_CATALOG_SCOPE_INCOMPLETE`, не расширяя allowlist.

Для каждой операции создаётся отдельный engine с `NullPool`, явным inspector DSN
и `SERIALIZABLE READ ONLY` transaction. Isolation выбран с учётом документированной
поддержки asyncpg readonly. Startup settings задают read-only, `statement_timeout`,
`lock_timeout` и фиксированный `search_path=pg_catalog` ещё до bootstrap SQLAlchemy.
Состояние transaction проверяется перед reflection. Нет DDL/DML, sampling,
исполнения views, default/check/generated/index expressions. Нет API произвольного SQL.
Приложение отдельно выдаёт inspector CONNECT/USAGE и запрещает write grants;
конфигурация не принимает writer connection. `writable` — структурный признак,
а не проверка grants пользователя загрузки.

SQL limits применяются до запросов; cursor читает ограниченное число rows.
Текстовые поля ограничиваются на сервере sentinel длиной, затем проверяются
до публикации. Переполнение, timeout, отсутствие объектов/прав или неподдержанные
metadata дают typed error без частичного snapshot. Общий async deadline включает
подключение и reflection. Cleanup отдельно ограничен, защищён от повторной отмены;
при неуспехе driver принудительно завершается, instance блокируется.

Credentials хранятся в `SecretStr`, исключены из repr/serialization и policy hash.
Исходные DBAPI exceptions не становятся cause/context публичной ошибки.
Engine/pool используют локальные отключённые loggers: application DEBUG logging
не должен раскрывать result rows или параметры. Глобальные loggers не меняются.

## Контракт и совместимость

Как A, B возвращает `DatabaseMetadataSnapshot` schema `1.1.0`; полный
`DatabaseAdapter.inspect`, schema fingerprint и FK graph относятся к C.
Inspection DTO дополнены enum/domain/array descriptors, identity mode,
materialized views, именованными constraints, index INCLUDE/method/null-order/
NULLS NOT DISTINCT/validity и comments. Legacy JSON без inspection сохраняется.
Comments с inspection metadata сохраняют пустые строки, пробелы и CR/LF/TAB
дословно; прочие controls и credential canaries по-прежнему отклоняются.
Legacy DTO без inspection сохраняют прежние strip/control ограничения comments.
Schema comments также требуют DatabaseMetadataSnapshot.

Column-level selectors отклоняются до подключения: выдавать неполные constraint
expressions небезопасно. EXCLUDE/temporal constraints, foreign tables, неподдержанная версия
сервера и непредставимые metadata дают явный отказ. Native types сохраняются;
неизвестные types имеют canonical `unknown`. Domain CHECK не исполняется.
Sequence state не читается; serial хранится как default expression, identity —
как `always`/`by_default`. Writer и identity override здесь отсутствуют.

## Проверки и зависимости

SQLAlchemy extra `asyncio` обязателен для PostgreSQL: без него greenlet может
отсутствовать на arm64. Production dependency family остаётся прежним.
Testcontainers 4.15.0 (Apache-2.0), asyncpg-stubs (BSD-3-Clause) и driver для тестов
добавлены только в dev group; версии и hashes зафиксированы в `uv.lock`.
Сервер тестируется через закреплённый multi-platform image PostgreSQL 16.15 и 18.6.
Обычный suite не стартует Docker. `make test-database` и отдельный CI job требуют
Docker и не используют успешный skip при отсутствии prerequisites.

Integration tests используют настоящие constraints/comments/generated columns,
отдельного inspector без SELECT grants, read-only probes даже под owner,
SQL trace, permission/auth failures, budgets, statement/lock/общий timeout,
cancellation и закрытие backend connections. SQLite не имитирует этот behavior.

## Остаточные ограничения

Каталог может содержать чувствительные comments/defaults; он остаётся локальным
и не отправляется LLM. Metadata text не является инструкциями.
PostgreSQL readonly не заменяет least privilege и не является process sandbox:
server memory для deparser ограничивается политикой самой СУБД, query временем.
Версии 15–18 проходят feature gate, реальная integration matrix — 16.15/18.6.
Прикладные SQLAlchemy event listeners остаются доверенной частью host application.

Источники:
[SQLAlchemy PostgreSQL reflection](https://github.com/sqlalchemy/sqlalchemy/blob/rel_2_0_52/lib/sqlalchemy/dialects/postgresql/base.py),
[SQLAlchemy readonly/asyncpg](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#setting-read-only-deferrable),
[PostgreSQL system catalogs](https://www.postgresql.org/docs/16/catalogs-overview.html),
[PostgreSQL 18 constraints](https://www.postgresql.org/docs/18/catalog-pg-constraint.html),
[Testcontainers](https://pypi.org/project/testcontainers/4.15.0/).
