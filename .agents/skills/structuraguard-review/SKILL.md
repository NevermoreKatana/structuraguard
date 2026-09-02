---
name: structuraguard-review
description: Проверяет diff StructuraGuard перед завершением или merge. Используй после реализации, bugfix или refactor; оцени correctness, architecture, security, compatibility, tests и performance без стилевых придирок.
metadata:
  author: structuraguard
  version: "1.0"
---

# Code review

## Вход

Текущая задача, критерии приёмки, diff и фактически выполненные проверки.

## Результат

Findings по severity с `path:line`, доказательством и исправлением; затем residual risks и непроверенные suites.

## Workflow

1. Прочитай задачу, критерии приёмки и текущий diff. Не анализируй весь репозиторий без связи с изменением.
2. Сначала проверь корректность и риск потери/искажения данных.
3. Затем проверь архитектурные инварианты, публичный API, backward compatibility и hidden side effects.
4. Для trust-boundary change примени `$structuraguard-security`.
5. Проверь error paths, cancellation/timeouts, resource lifecycle, batch boundaries и concurrency.
6. Оцени тесты: покрывают ли они новый контракт, отрицательные случаи и реальную regression.
7. Не перечисляй форматирование, которое надёжно ловят Ruff/mypy, если оно не скрывает дефект.
8. Findings выводи первыми по severity с `path:line`, доказательством последствий и минимальным исправлением.
9. Если существенных findings нет, сообщи это явно и перечисли только реальные residual risks или непроверенные suites.
10. Не утверждай, что код идеален. Утверждай только то, что подтверждено diff и командами.

Используй [review checklist](references/REVIEW_CHECKLIST.md).
