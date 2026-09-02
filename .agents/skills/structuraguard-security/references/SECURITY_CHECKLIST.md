# Security checklist

## Files/parsers

- MIME/signature conflict, archive bomb, path traversal, symlink, malformed container.
- XML XXE/DTD, YAML object construction/alias bomb, HTML external fetch/script.
- Limits: bytes, records, columns, nesting, line length, pages, time, memory.
- Dangerous parser требует sandbox в strict mode.

## LLM

- Документные инструкции не получают authority.
- Нет tools, credentials и прямого DB access.
- Data classification действует до cloud call и до fallback.
- Output строго валидируется и не исполняется.

## Database

- Separate inspection/writer principals.
- Schema/table/column allowlist и запрет system schemas/DDL.
- Parameterized values; identifiers берутся из reflected catalog.
- Staging, atomic default, rollback и idempotency.

## Supply chain и observability

- Новая dependency обоснована, закреплена и проверена.
- Логи и traces редактируют secrets/PII.
- Audit фиксирует hashes/decisions, но не sensitive payload.
