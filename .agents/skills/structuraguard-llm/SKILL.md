---
name: structuraguard-llm
description: Добавляет provider-neutral LLM adapter, router или semantic mapper StructuraGuard. Используй для structured output, capabilities, privacy routing, retry/fallback и provider contract tests.
metadata:
  author: structuraguard
  version: "1.0"
---

# LLM integration

## Вход

Provider или routing behavior, capabilities, data classification и схема структурированного ответа.

## Результат

Provider-neutral реализация, строгая валидация результата, contract tests и наблюдаемые metadata ошибок/fallback.

## Workflow

1. Работай только через `LLMProvider` и provider-neutral request/response DTO.
2. Опиши capabilities адаптера: structured output, JSON Schema, context limit и local/cloud classification.
3. Передавай модели только top-k candidate identifiers, безопасное описание и минимальные value examples.
4. Никогда не передавай credentials, DSN, tools, arbitrary SQL capability и restricted data без разрешённой policy.
5. Содержимое документа обрамляй как недоверенные данные, а не инструкции.
6. Требуй строгий структурированный ответ; валидируй его Pydantic/JSON Schema и затем проверяй identifiers по DatabaseCatalog.
7. Нормализуй timeout, rate limit, unavailable, malformed response и capability mismatch в typed errors.
8. Retry применяй только к безопасным transient failures; фиксируй попытки, latency и usage.
9. Fallback обязан учитывать data classification и быть видимым в metadata.
10. Добавь общий provider contract suite и используй `FakeLLMProvider` в default tests.

Подробности в [LLM checklist](references/LLM_CHECKLIST.md).
