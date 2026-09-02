# Database checklist

## Inspection

- Schemas, tables, views, columns, comments и native/canonical types.
- PK, FK, unique, check, indexes, generated/default/identity fields.
- Стабильная canonical serialization перед fingerprint.

## Load

- Allowlist проверена повторно непосредственно перед SQL.
- Нет DDL и доступа к system schemas.
- Identity strategy не выдумана из статистической уникальности.
- Parent rows загружены до dependent rows.
- Partial failure соответствует выбранному atomic/quarantine mode.
- Idempotency защищает от повторного запуска того же source/plan.

## Риски

- SQL injection через identifiers.
- TOCTOU/schema drift.
- Deadlock и неверный transaction scope.
- Потеря данных при ошибочном upsert key.
- Утечка sampled DB values в prompts/logs.
