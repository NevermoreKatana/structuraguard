---
name: structuraguard-security
description: Проводит security review изменений StructuraGuard на границах файлов, parser, LLM, БД, staging, logs и plugins. Используй при недоверенном вводе, PII, prompt injection, XXE, SQL и resource limits.
metadata:
  author: structuraguard
  version: "1.0"
---

# Security review

## Вход

Компонент или diff, attacker-controlled inputs, активы и trust boundaries.

## Результат

Findings по severity либо подтверждение отсутствия существенных findings, конкретные controls и security regression tests.

## Workflow

1. Определи активы, attacker-controlled input, trust boundaries и полномочия компонента.
2. Проверь abuse cases до happy path: malformed file, resource exhaustion, parser exploit, prompt injection, PII leak, SQL/identifier injection, schema drift и unauthorized table access.
3. Защиту размещай на детерминированной границе. Prompt сам по себе не считается достаточным контролем.
4. Используй deny-by-default, least privilege, allowlists, bounded resources и explicit failure.
5. Убедись, что error/log/audit context не содержит secrets и restricted raw values.
6. Для найденной проблемы добавь минимальный exploit/reproduction test и security regression test.
7. Findings сообщай по severity: Critical, High, Medium, Low. Для каждого укажи location, impact, exploit path и конкретное исправление.
8. Не исправляй несвязанный код во время review без запроса.

Проверь [модель угроз](references/SECURITY_CHECKLIST.md).
