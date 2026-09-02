---
name: structuraguard-python
description: Реализует или рефакторит Python-код ядра StructuraGuard. Используй для DTO, protocols, pipeline, services и публичного SDK API; совмещай с профильным skill для parser, DB или LLM.
metadata:
  author: structuraguard
  version: "1.0"
---

# Качественная реализация Python

## Вход

Задача, критерии приёмки, релевантный публичный contract и существующие tests.

## Результат

Минимальная типизированная реализация, подтверждающие tests, обновлённая документация при изменении API и список выполненных проверок.

## Workflow

1. Уточни контракт по существующим types, tests и релевантному разделу ТЗ.
2. Сначала добавь тест нового поведения или characterization test для рефакторинга.
3. Внеси минимальное связное изменение без speculative abstractions.
4. Сохраняй направление зависимостей: contracts/domain не знают об infrastructure.
5. Делай зависимости явными; I/O отделяй от чистой логики.
6. Публичные функции полностью типизируй. Не расширяй `Any` за границу адаптера.
7. Ошибки представляй typed exceptions с устойчивым error code и полезным контекстом без secrets.
8. Комментарии и docstring пиши на русском только для контракта, причины или неочевидного ограничения.
9. После зелёного теста упрости имена, control flow и дублирование, не меняя поведение.
10. Запусти узкие тесты, lint и typecheck; перед завершением используй `$structuraguard-review`.

## Запреты

- Не добавляй hidden fallback, global state и import-time side effects.
- Не используй broad `except`, mutable defaults, boolean traps и необоснованные casts/ignores.
- Не меняй публичный контракт случайно.

При сомнении используй [чек-лист качества](references/PYTHON_QUALITY.md).
