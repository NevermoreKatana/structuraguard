# Review checklist

## Critical/High

- Data loss/corruption, unauthorized write, secret/PII leak.
- SQL/identifier injection, XXE, unsafe deserialization, code execution.
- Broken transaction/rollback/idempotency.
- LLM output получает authority или исполняется.

## Medium

- Неверный error contract, schema drift, missing limit.
- Blocking I/O в async path, leaked connection/file, race condition.
- Неполное provenance или неустойчивый fingerprint.
- Поведение не покрыто test на нужном уровне.

## Low

- Локальная сложность, misleading name/docstring, избыточная abstraction.

Не блокируй change из-за личного стиля, если код улучшает систему, соответствует требованиям и quality gates.
