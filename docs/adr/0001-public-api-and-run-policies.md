# ADR 0001. Public API и политики run

Статус: принято для design M0.

Дата: 2026-09-02.

## Контекст

ТЗ показывает combined methods `analyze` и `ingest`, пошаговый async flow и два
разных смысла параметра `mode`: orchestration `auto_safe` и обработку load errors.
Также source может быть path, raw text, stream или one-shot iterator. Без единого
решения это создаёт неоднозначные side effects, повторное чтение изменившегося
source и несовместимые API signatures.

## Решение

- Каноническим является пошаговый async flow `inspect_source` →
  `inspect_database` → `create_plan` → `validate_plan` → `execute`.
- `analyze` и `ingest` являются convenience orchestration и не получают
  дополнительных полномочий.
- `str` трактуется как filesystem path; raw text передаётся явным `TextSource`.
  Path разрешён только immutable `SourcePathPolicy`, заданной trusted
  composition owner; per-run caller не расширяет allowed roots.
- `inspect_source` возвращает async-closeable `SourceAnalysis` с bounded
  snapshot lease и `source_fingerprint`. Lease живёт между staged calls до
  `aclose()`/context exit/expiry; combined `ingest` закрывает его сам. Reuse plan
  требует нового inspection исходного source и сверки fingerprint.
- `safety_policy`, `error_policy` и `dry_run` являются независимыми axes.
- `auto_safe` run-level plan/security/identity/system gates всегда veto.
  `atomic` — default; `quarantine_invalid` и `best_effort` выбираются явно и
  применяются только к recoverable record-level failures.
- Dry run не оставляет persistent source/target/staging data-plane mutations.
  Разрешены in-memory/rollback-only staging с cleanup и redacted control-plane
  audit/events.
- `MappingPlan` immutable и versioned; изменение создаёт новый fingerprint.
- Каждый run получает immutable snapshot instance registry. Регистрация между
  runs допустима, но не меняет уже начавшийся run.
- До commit configured timeout даёт `FAILED`/`PROCESSING_TIMEOUT`, caller
  cancellation — `CANCELLED`; rollback/cleanup предшествуют terminal outcome.
  После commit timeout/cancellation даёт post-commit warning, не ложный rollback.

## Последствия

API становится длиннее одного универсального `mode`, зато side effects и policy
можно проверять независимо. Реализация обязана управлять bounded snapshots и
cleanup; забытый caller lease ограничивается expiry. Path без trusted policy и
закрытый lease завершаются явным отказом. Любое отклонение от этих правил требует
нового ADR и оценки совместимости.

Связанные документы: [public API](../public-api.md),
[архитектура](../architecture.md), [требования](../requirements.md).
