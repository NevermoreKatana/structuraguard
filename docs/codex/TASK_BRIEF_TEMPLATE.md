# Короткий шаблон задачи для Codex

Используй его вместо длинного свободного промпта.

```text
Навык: $structuraguard-<name>
Цель: <одно проверяемое предложение>
Scope: <модули или файлы>
Приёмка:
- <наблюдаемое поведение>
- <ошибка или boundary case>
Не делать: <явный out-of-scope>
Проверки: <команды либо «выбери по AGENTS.md»>
```

## Пример реализации

```text
Навык: $structuraguard-parser
Цель: добавить потоковый NDJSON parser с точным provenance строки.
Scope: parser contract, registry, NDJSON adapter и tests.
Приёмка:
- обрабатывает вход пакетами без полной загрузки в память;
- malformed line возвращает typed error с номером строки;
- действуют max_records и cancellation.
Не делать: не менять MappingPlan и loader.
Проверки: узкие tests, lint, typecheck, затем $structuraguard-review.
```

## Пример исправления дефекта

```text
Навык: $structuraguard-debug
Цель: устранить дублирование записи на границе двух CSV batches.
Приёмка:
- сначала воспроизводящий regression test;
- каждая строка загружается ровно один раз;
- idempotency прежних сценариев не меняется.
Не делать: не рефакторить весь CSV pipeline.
```

Не вставляй в задачу всё ТЗ. Укажи идентификаторы разделов, а Codex извлечёт их через `scripts/extract_spec_sections.py`.
