---
name: structuraguard-database
description: Разрабатывает Database Inspector, catalog, FK graph, MappingPlan validation, staging или loader StructuraGuard. Используй при SQLAlchemy/PostgreSQL/SQLite, dry-run, upsert и transaction logic.
metadata:
  author: structuraguard
  version: "1.0"
---

# Интеграция с БД

## Вход

DB dialect, операция, allowlist/policies, transaction semantics и ожидаемый contract.

## Результат

Безопасная реализация inspection/catalog/loader, стабильный fingerprint, tests нужного уровня и результаты rollback/dry-run проверок.

## Workflow

1. Определи режим: inspection, catalog/graph, mapping validation, staging либо load.
2. Для inspection используй read-only connection; для записи — отдельный least-privilege user.
3. Ограничивай schemas/tables/columns allowlist до reflection и до исполнения плана.
4. Каталог должен нормализовать dialect-specific types и строить стабильный schema fingerprint.
5. FK graph определяет принадлежность сущностей и порядок загрузки; cycle требует явной стратегии.
6. LLM передаёт только identifiers из candidate list. SQL создаёт исключительно adapter.
7. Все значения параметризуются; identifiers проверяются по каталогу и не принимаются как свободный SQL.
8. `dry_run` не изменяет БД. Load использует staging, atomic transaction по умолчанию и проверяемый rollback.
9. Upsert разрешён только при подтверждённом PK/unique/natural key.
10. Проверяй schema drift перед исполнением сохранённого MappingPlan.
11. Добавь SQLite unit/contract tests и PostgreSQL integration tests для dialect behavior.

Перед реализацией просмотри [DB checklist](references/DATABASE_CHECKLIST.md).
