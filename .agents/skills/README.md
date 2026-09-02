# StructuraGuard Skills

| Skill | Назначение |
|---|---|
| `structuraguard-plan` | План архитектурно значимого изменения |
| `structuraguard-python` | Реализация и рефакторинг Python SDK |
| `structuraguard-debug` | Системная диагностика дефектов |
| `structuraguard-parser` | Parser adapters и source normalization |
| `structuraguard-database` | Inspection, mapping, staging и loading |
| `structuraguard-llm` | Provider-neutral LLM integration |
| `structuraguard-security` | Threat review и security regression |
| `structuraguard-tests` | Unit, contract, integration, property/security tests |
| `structuraguard-review` | Review diff перед завершением |
| `structuraguard-docs` | Русская документация и docstring |

Codex может выбрать навык по `description` либо получить его явно через `$skill-name`.

## Рекомендуемые композиции

```text
Новая крупная функция:
$structuraguard-plan → профильный skill → $structuraguard-tests → $structuraguard-review

Дефект:
$structuraguard-debug → $structuraguard-tests → $structuraguard-review

Изменение trust boundary:
профильный skill + $structuraguard-security + $structuraguard-review
```

Короткая форма задачи находится в `docs/codex/TASK_BRIEF_TEMPLATE.md`.
