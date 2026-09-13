# ADR 0031: DB maximum, canonical HMAC audit и sandbox boundary

Статус: принято. Дата: 2026-09-13. Scope: M14 D1–D5.

## Контекст

M7–M13 уже проверяют scope, разные DB principals, parameterized SQL, отсутствие
runtime DDL, schema drift, транзакцию и idempotency. M3/M4 валидируют physical
parser stream, но не предоставляют OS isolation. M13 ledger содержит hashes
и counts, его unsigned fingerprint не является HMAC chain.

## Решение

1. Central `DatabasePolicy` сужает existing target и включается в policy
   fingerprint. Allow/deny schemas/tables/columns проверяются до full reflection
   и повторно writer transaction. Полный column scope обязателен: частичный
   каталог мог бы скрыть constraints. Старые flat selectors остаются fail-closed.
2. Новый закрытый `SecurityAuditEvent` отделён от legacy reports DTO. Provider,
   model, prompt и actor используют UUID references. Нет free-form payload,
   сообщений exceptions, raw values либо hashing коротких secrets.
3. HMAC-SHA-256 подписывает canonical versioned body и предыдущие 32 digest
   bytes. Explicit key/signature/store ports; per-chain CAS, bounded verification,
   immutable event ID, rotation и external expected head. Memory store имеет
   только локальную atomicity, PostgreSQL store использует target transaction.
4. Отдельная admin bootstrap создаёт versioned audit schema; runtime reuse
   M13 layout/grant checks, без DDL/auto-upgrade. Commit включает target event
   и durable delivery intent независимо от idempotency. M13 marker хранит head
   reference; replay проверяет signed prefix. Legacy receipts не переписываются.
   Rollback audit — отдельная операция, её failure становится наблюдаемым gap.
5. `ParserRunner` объединяет in-process implementation и `SandboxParserRunner`,
   который оркестрирует trusted `SandboxBackend`. Backend declaration должна
   содержать весь profile; strict execution без него закрыт. Descriptor/pinned
   artifact передаются как данные; host plugin import и shell execution отсутствуют.
   Existing registry повторяет output validation, core ограничивает wire/result.

## Совместимость и границы

План M14 D уточнён без обещания полного будущего orchestrator: central DB policy
по умолчанию требует signed audit; legacy target без policy сохраняет M13 API.
Loader audit включается явной configuration/signer dependency, а не listener.
`PostgreSQLLoadResult.audit_head` отсутствует в legacy serialization. Изменение
audit policy меняет idempotency binding и требует нового ключа/binding при rollout.
Общий canonical serializer и bounded preflight дополнены fixed-size UUID.
Architecture allowlists дополнены только pure stdlib UUID/context-manager types
и новыми contracts/ports. `ParserResourceGuard` сохраняет направление зависимости:
runner не импортирует security facade. HMAC implementation загружается при
операции, поэтому root import/export resolution не выполняет eager crypto import.

Внешняя outbox delivery/ack и OS sandbox backend принадлежат host. M14 поставляет
durable intent, verification и interfaces; не обещает exactly-once delivery,
container isolation или key/store protection, которых embedding library не
может самостоятельно обеспечить. Полный stage lifecycle и auto-composition
появятся в M15; legacy audit events не подписываются задним числом.

## Проверки и риски

SQLite и PostgreSQL tests подтверждают deny precedence и column preflight.
Audit regression покрывает key rotation, mutation/reorder/delete/replay,
concurrent append, bounds и secret-safe failures. PostgreSQL suite проверяет
атомарность target/audit/outbox и ошибку terminal audit. Runner contract suite
проверяет capabilities, no-start при deny, bounded IPC, provenance, timeout и cleanup.

HMAC не даёт non-repudiation. Без внешнего anchor не доказать полноту истории;
key holder может переподписать изменения, compromised store может скрыть события.
Fake sandbox tests доказывают только интерфейс и fail-closed admission.
