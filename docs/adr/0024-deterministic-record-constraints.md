# ADR 0024: deterministic DB constraints и закрытый business-rule DSL

- Статус: принято в рамках авторизованной реализации M12-C.
- Дата: 2026-09-12.
- Основание: [план M12](../plans/M12_validation_engine.md),
  [ADR 0022](0022-conservative-normalization-and-validation.md).

## Решение

Поставить независимые async `BusinessRuleValidator` и `DatabaseConstraintValidator`
для bounded immutable `ValidationDataset` после EOF. Caller проецирует нормализованные
записи и сохраняет original/raw/trace; публичные legacy DTO и MappingPlan не меняются.
Уровень D будет связывать эти результаты с physical provenance и общим отчётом.

DSL — discriminated data-only DTO с 16 allowlisted операциями и явными typed field/
literal/product operands. Python/SQL expressions и динамическое исполнение отсутствуют.
Missing, null и wrong type различаются. Сумма children привязана к parent/collection,
точные integer/Decimal вычисления не зависят от decimal context; tolerance отдельно
разрешает владелец. Reference snapshots передаются владельцем и связаны fingerprint.

Local DB predicates опираются на catalog-v1, операции и column/source identity allowlists.
Семантика неизвестных indexes/collations/domain keys остаётся unverified. SQL CHECK
не интерпретируется. Trusted typed CHECK attestation привязана к catalog/table/column/
expression hash; владелец отвечает за эквивалентность, включая NULL UNKNOWN.
Проверки нескольких bindings расходуют общий finite budget.

`ConstraintReader` имеет отдельные права чтения columns и принимает закрытый набор
ordered keys. SQLite/PostgreSQL реализации заново проверяют catalog и читают EXISTS
в одной read-only transaction. PostgreSQL берёт ACCESS SHARE на читаемые таблицы
до первого SELECT/snapshot; DDL с AccessExclusive не может заменить таблицу между
reflection и EXISTS. Locks ограничены deadline и освобождаются rollback. SQL генерирует SQLAlchemy, значения параметризуются.
Нет user SQL, rows materialization, writer API, cache или глобального состояния.
RLS и неподдержанные key semantics вызывают явный отказ, а не ложный pass.

## Уточнения conservative policy

SQLite не enforce `VARCHAR(n)` и numeric precision как PostgreSQL; SDK дополнительно
блокирует loss-prone length, а decimal affinity оставляет unverified.
Default/generated SQL не вычисляется для missing operand в CHECK. Deferred constraints
не получают положительного precheck. Text equality допускается только при доказанной
binary collation, без Python имитации произвольной locale.

Fingerprint reader report удостоверяет связь с request и boolean outcomes одной
завершившейся transaction; он не является серверным snapshot token. Read-only acceptance
не разрешает commit: окончательные constraints и TOCTOU обеспечивает будущий loader.
API, бюджеты, коды и тестируемые ограничения: [руководство](../db-business-validation.md).
