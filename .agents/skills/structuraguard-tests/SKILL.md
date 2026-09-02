---
name: structuraguard-tests
description: Проектирует и пишет тесты StructuraGuard. Используй для unit, contract, integration, property-based и security regression tests, а также при формировании test matrix для нового поведения.
metadata:
  author: structuraguard
  version: "1.0"
---

# Тестирование

## Вход

Критерий приёмки, дефект или protocol contract и границы проверяемого поведения.

## Результат

Минимальный устойчивый набор tests, который подтверждает happy path, ошибки и существенные boundary cases.

## Workflow

1. Преобразуй критерий приёмки в наблюдаемое поведение и выбери минимальный уровень теста.
2. Для дефекта сначала добавь failing regression test.
3. Unit tests оставляй чистыми и быстрыми; mock используй только на внешней границе.
4. Один contract suite должен применяться ко всем implementations одного protocol.
5. PostgreSQL-specific behavior проверяй integration test, а не SQLite imitation.
6. Property-based tests применяй для parsers, normalization, round-trips, limits и invariants.
7. Security test должен содержать безопасный минимальный payload и проверять конкретный control.
8. Default suite не вызывает paid/external LLM; используй `FakeLLMProvider` и controlled clocks/UUIDs.
9. Не используй arbitrary sleep. Для async проверяй события и condition-based completion.
10. Запусти новый test отдельно, затем suite модуля; полный набор — перед завершением.

Выбери уровни по [матрице тестов](references/TEST_MATRIX.md).
