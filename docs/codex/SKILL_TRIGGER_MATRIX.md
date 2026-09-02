# Матрица активации Skills

Используется для ручной проверки описаний навыков через `/skills` и тестовые запросы.

| Пример запроса | Ожидаемый Skill |
|---|---|
| «Спроектируй реализацию M5 и разложи по шагам» | `structuraguard-plan` |
| «Реализуй доменные DTO и async facade» | `structuraguard-python` |
| «Ошибка появляется только на втором batch, найди причину» | `structuraguard-debug` |
| «Добавь parser для NDJSON» | `structuraguard-parser` |
| «Сделай reflection FK и порядок вставки» | `structuraguard-database` |
| «Подключи OpenAI-compatible provider» | `structuraguard-llm` |
| «Проверь XXE, prompt injection и утечки PII» | `structuraguard-security` |
| «Добавь contract и property-based tests» | `structuraguard-tests` |
| «Проведи review текущего diff» | `structuraguard-review` |
| «Обнови русскую документацию публичного API» | `structuraguard-docs` |

## Негативные случаи

- Исправление опечатки не должно автоматически запускать подробное планирование.
- Изменение README без API-контракта не должно запускать database или LLM skills.
- Форматирование кода не должно запускать security review, если trust boundary не изменился.
- Parser skill не применяется к разработке demo UI.
