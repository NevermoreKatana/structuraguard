# M14: DB policy, HMAC audit и parser runners

Эти controls подключаются явно владельцем SDK. Они используют существующие
DB permission/catalog checks и parser output validation. Наличие policy DTO
или HMAC подписи само по себе не выдаёт права на данные.

## DB policy

`DatabasePolicy` из `structuraguard.contracts.database_policy` задаёт exact
schema/table/column selectors, denylist и разные inspector/writer principals.
`security.database.bind_database_policy(target, policy)` сужает существующий
`SQLiteTarget`/`PostgreSQLTarget` до пересечения scopes. Пустой allowlist закрыт;
deny всегда сильнее allow. Изменение policy меняет target policy fingerprint.

<!-- example:m14-database-policy:start -->
```python
from structuraguard.contracts.database_policy import DatabasePolicy, DatabaseTableRule

policy = DatabasePolicy(
    allowed_schemas=("app",),
    allowed_tables=(DatabaseTableRule(
        schema_name="app", table_name="orders", columns=("id", "amount"),
    ),),
    denied_columns=(("app", "orders", "private_note"),),
    inspector_principal="schema_inspector",
    writer_principal="data_importer",
)
assert policy.require_signed_audit is True
```
<!-- example:m14-database-policy:end -->

Это создание policy без подключения к БД. Если `orders` действительно содержит
`private_note`, inspection всей таблицы будет отклонён: для полного каталога
нельзя скрывать даже одну колонку. Пример не создаёт таблицы и роли.

Central selectors принимают ASCII identifiers до 63 символов без SQL quoting,
wildcards или whitespace normalization. System schemas, SQLite catalogs и
внутренние staging/audit schemas закрыты как business targets. Metadata adapter
может читать необходимые системные каталоги фиксированными запросами; caller
не получает через это права выбирать их как source/target таблицы.

`authorize_database` допускает только `inspect/select/insert/update`; DDL,
arbitrary SQL и неизвестные operations дают `DATABASE_OPERATION_FORBIDDEN`.
Исполняемый SQL по-прежнему строит adapter из каталога с parameterized values.

Для inspection нужен **полный column scope каждой таблицы**. Сначала adapter
читает bounded список имён колонок. Запрещённая или неизвестная колонка даёт
`DATABASE_COLUMN_NOT_ALLOWED` до definitions/comments/constraints. Частичный
каталог не публикуется: скрытие PK/FK/CHECK могло бы разрешить неверный load.
Старые flat `include_columns/deny_columns` targets сохраняют прежний отказ;
для M14 используются `DatabaseTableRule` и `DatabasePolicy.denied_columns`.
SQLite сравнивает ASCII имена без учёта регистра и не обеспечивает DB users;
там дополнительно действуют read-only connection и authorizer. Реальное
разделение ролей проверяется PostgreSQL integration suite.

PostgreSQL сверяет inspector login и реальные роли до reflection; writer
повторяет scope, principals, grants и schema fingerprint в target transaction.
Central policy по умолчанию требует `require_signed_audit=True`: loader без
audit configuration/signer получает `AUDIT_DURABILITY_REQUIRED` до I/O.
Для совместимости targets без central policy сохраняют M13 поведение.

## Подписанные события

Минимальный пример пишет одно событие в память и проверяет его относительно
полученного head. Ключ создаётся только при запуске примера; его и anchor нужно
хранить отдельно от production audit store.

<!-- example:m14-audit:start -->
```python
import asyncio
import secrets
from datetime import UTC, datetime
from uuid import uuid4

from structuraguard.exceptions import SecurityPolicyError
from structuraguard.security import (
    AuditChain, AuditKind, HMACAuditSigner, MemoryAuditChainStore,
    MemoryAuditKeys, SecurityAuditEvent, SecurityPolicy,
)


async def main() -> None:
    run_id, key_id = uuid4(), uuid4()
    policy = SecurityPolicy()
    chain = AuditChain(
        chain_id=uuid4(), run_id=run_id, key_id=key_id,
        policy_fingerprint=policy.fingerprint,
        signer=HMACAuditSigner(MemoryAuditKeys({key_id: secrets.token_bytes(32)})),
        store=MemoryAuditChainStore(),
    )
    event = SecurityAuditEvent(
        event_id=uuid4(), run_id=run_id, actor_id=uuid4(),
        occurred_at=datetime.now(UTC), kind=AuditKind.RUN_STARTED,
        policy_fingerprint=policy.fingerprint,
    )
    envelope = await chain.append(event)
    assert await chain.append(event) == envelope
    checked = await chain.verify(expected_head=envelope.head)
    assert checked.anchored and checked.count == 1
    damaged = envelope.model_copy(update={"current_hash": "0" * 64})
    try:
        await chain.verify_records((damaged,), expected_head=envelope.head)
    except SecurityPolicyError as error:
        assert error.error_code == "AUDIT_CHAIN_INVALID"
    else:
        raise AssertionError("Изменённая подпись должна быть отклонена")


asyncio.run(main())
```
<!-- example:m14-audit:end -->

Пример не подтверждает завершение run, durability или доставку событий.
`ResourceAuditEvent` и summaries scanner не добавляются в цепочку автоматически;
trusted host формирует соответствующий `SecurityAuditEvent` без raw данных.

`SecurityAuditEvent` — новый закрытый формат из `contracts.audit`.
Он содержит run/event/actor UUID, UTC time, SDK version, kind, status, decision,
artifact fingerprints, validation counts и insert/update counts. LLM evidence
использует provider/model/prompt UUID из отдельного trusted registry. Имена,
prompts, ответы, SQL, binds, paths, error messages и произвольные payloads
не входят в событие. Начальная стадия не требует ещё не вычисленных hashes;
`load_committed` требует target/source/DB/mapping и validation evidence.

Legacy `contracts.reports.AuditEvent` сохраняет свой wire contract. Он не
становится подписанным автоматически. Новый формат не сериализует целые
security/validation reports, которые могут содержать лишние metadata.

`AuditChain` связывает chain UUID, run UUID и policy fingerprint.
`AuditSigner`/`AuditKeyProvider` — явные ports. `HMACAuditSigner` использует stdlib,
`MemoryAuditKeys` принимает immutable ring из 1–32 ключей по 32–64 bytes.
Production KMS/key retention задаёт host; import не читает env и key files.

```text
body = envelope без previous_hash и current_hash
message = bytes.fromhex(previous_hash) || UTF-8(canonical_json(body))
current_hash = HMAC-SHA-256(key_for(key_id), message).hex()
```

Genesis — 32 нулевых bytes. Canonical JSON v1 сортирует keys, использует UTF-8,
UTC `Z`, UUID в lowercase, не допускает NaN. Version, algorithm, key ID,
sequence, chain/run/policy binding защищены подписью. HMAC digest имеет ровно
64 hex символа и не выдаётся за `sha256:` artifact fingerprint.

Append проверяет tail signature и атомарный CAS. Повтор event ID разрешён только
для идентичного события. Concurrent conflicts имеют конечное число retries;
caller получает explicit failure при исчерпании. Verification читает pages до
256 событий, ограничивает общее число, время и размер каждого envelope (16 KiB).
`verify(expected_head=...)` подтверждает предоставленную историю до точного
trusted head; `verify_through(head)` проверяет prefix сохранённого commit.
Пустой store даёт `AUDIT_CHAIN_EMPTY`. Без anchor результат `provided_prefix`
не подтверждает полноту lifecycle или отсутствие удалённого suffix.

Rotation задаётся новым `key_id` при том же run/chain/policy binding. Для проверки
старых событий нужны старые verification keys; неизвестный key — отказ.
`MemoryAuditChainStore` имеет finite capacity и atomic CAS в одном event loop,
но не имеет durable storage или межпроцессной синхронизации.

## PostgreSQL transaction и миграция {#postgresql-transaction}

`PostgreSQLLoadPolicy.audit=PostgreSQLAuditPolicy(...)` и
`PostgreSQLLoader(..., audit_signer=...)` включают обязательный signed audit.
`PostgreSQLAuditChainStore` получает **тот же** connection/transaction, что DML.
До DML проверяются layout, append-only grants и key availability; перед commit
записываются signed event и immutable `delivery_intents` row. Ошибка приводит
к rollback target, audit и intent. Даже без idempotency включённый audit обязателен.

Для установки владелец вызывает отдельный `bootstrap_postgresql_audit` с
`PostgreSQLStagingTarget(purpose="bootstrap")`, admin credentials и согласованным
`PostgreSQLAuditPolicy`. Допустима только выделенная `sg_staging_audit_*` schema.
Bootstrap создаёт три таблицы и version/layout fingerprint; существующая
несовместимая schema отвергается. Loader не вызывает bootstrap или ALTER/DROP.
Writer получает USAGE schema, SELECT таблиц и INSERT только `chain_events` и
`delivery_intents`; UPDATE/DELETE/TRUNCATE/TRIGGER запрещены. Admin connection
не передаётся ingest. Layout/grant guard общий с M13 ledger.

Receipt содержит `audit_head`. Если включён M13 idempotency, marker и его
`execution_audit` ссылаются на этот head; повтор проверяет подписанный prefix
до выдачи replay result. Legacy v1 receipts остаются читаемыми без retroactive
подписи. Включение audit меняет policy/idempotency binding, поэтому старый key
не может незаметно разрешить новую семантику; rollout требует нового binding.
Если marker уже доказывает прошлый COMMIT, но chain не проходит verification,
replay отклоняется с `AUDIT_COMMITTED_UNVERIFIED`; SDK не объявляет rollback
ранее committed данных и не повторяет DML.

После подтверждённого rollback SDK пытается добавить `run_failed` отдельной
bounded transaction. Отказ даёт `details.audit_gap=True`; cancellation сохраняет
тип `CancelledError` и безопасную заметку `AUDIT_GAP`. При ambiguous commit
сохраняется `LOAD_OUTCOME_UNKNOWN`, rollback не утверждается. Post-commit
cleanup не отменяет уже подтверждённый result.

Intent — durable обязательство доставки, не внешний listener. Host dispatcher
читает его и доставляет at-least-once; consumer deduplicates `(chain_id,event_id)`.
Delivery acknowledgements и retries хранятся отдельно от immutable chain.
SDK не обещает exactly-once внешнюю доставку. DB/audit/runner срез не добавляет
production dependencies; отдельный DLP extra `security` использует `cryptography`.

## Parser runners

`ports.sandbox.ParserRunner` — общий protocol. `parsers.runners.InProcessParserRunner`
делегирует `SecuritySession.parse`: явно разрешённые builtins и недоверенные данные,
existing registry validation и cooperative timeout. При
`SecurityPolicy(strict_mode=True)` форматы из `risky_formats` требуют sandbox.
Default `parser_trust="sandbox_required"` закрывает любой in-process parse.

`SandboxParserRunner` принимает immutable `SandboxParserSpec`, explicit
`SandboxPolicy.allowed_specs`, session и trusted `SandboxBackend`.
Spec связывает artifact fingerprint, descriptor metadata/entry point, adapter
version и format. Host не вызывает `EntryPoint.load`, import или constructor
plugin. Backend проверяет pinned artifact и разрешает entry point внутри sandbox.

Все `SandboxCapabilities` обязательны: non-root, read-only filesystem, no network,
CPU/memory/pid limits, isolated temp, no host secrets/Docker socket, bounded IPC,
terminate/reap. Declaration принадлежит владельцу backend. Без backend, allowlist
или любого capability — `SECURITY_SANDBOX_REQUIRED` до чтения source.

Request содержит bounded source reference/reader lease и limits; paths и
DB/LLM/audit handles не передаются worker. Backend обязан ограничивать source,
stdout/stderr/frame accumulation и принимать argv без shell interpolation.
Core проверяет frame/total bytes и JSON depth до DTO decode, source/probe/parser
identity, physical provenance, batch order, records, manifests и EOF через
существующий registry. Timeout/cancel/early exit вызывают `aclose`; cleanup ack
обязателен. Backend отвечает за cleanup также при отмене `start` до возврата handle.

Ошибка runner или cleanup закрывает общий security run и сохраняет safe terminal
resource event; последующие DB/LLM resource operations отказывают. Исходный runner
error code сохраняется. Собственный `ParserResourceGuard` должен реализовать
`record_failure(code)` с terminal semantics, как штатный `SecuritySession`.

Поставляются protocol и orchestration implementation, **не OS sandbox backend**.
Contract tests с fake backend не доказывают network/kernel isolation. Для strict
production host должен предоставить и отдельно проверить реальную изоляцию.
In-process Python и непослушный trusted backend нельзя принудительно изолировать
из core SDK; обычный subprocess document parsers не считается sandbox.

## Ограничения доверия

- HMAC key holder может переписать события и пересчитать цепочку; non-repudiation нет.
- Store без ключа может удалять/усекать history или мешать записи. Полное удаление
  и suffix truncation обнаруживаются только относительно внешнего trusted anchor.
- Компрометация key и store позволяет согласованно переписать локальную историю;
  нужны независимые anchors/retention/ACL. Key bytes остаются в process memory,
  zeroization не гарантируется.
- Hashes/UUID и timing остаются коррелируемыми metadata; raw secret hashes не
  считаются redaction. HMAC не проверяет истинность утверждений trusted producer.
- DBA grants/triggers, OS/kernel и trustworthiness backend требуют deployment
  controls. M14 не завершает будущую M15 orchestration всех стадий SDK.

## Ошибки и совместимость

Создание некорректного DTO даёт Pydantic `ValidationError`; такой объект ошибки
может содержать входные значения, поэтому его полный текст не является safe log.
Resource/DLP/runner/audit API используют `SecurityPolicyError` с фиксированными
messages. DB adapters и loader сохраняют свои `DatabaseInspectionError`/`LoadError`
contracts; например, loader сообщает `AUDIT_DURABILITY_REQUIRED` через `LoadError`.
DB отказывает кодами `DATABASE_OPERATION_FORBIDDEN`, `DATABASE_COLUMN_NOT_ALLOWED`
и scope/role codes. Audit отличает `AUDIT_CHAIN_INVALID`, `AUDIT_CHAIN_EMPTY`,
`AUDIT_KEY_UNAVAILABLE`, `AUDIT_LIMIT_EXCEEDED`, `AUDIT_OPERATION_FAILED` и
`AUDIT_DURABILITY_REQUIRED`; неизвестный backend text наружу не переносится.
Timeout/error не означает, что внешний store точно не успел записать событие:
повтор использует тот же event ID. Store atomicity и transaction outcome остаются
обязанностью adapter/host. Полная матрица — в [приёмке](plans/M14_acceptance.md).

Runner возвращает `SECURITY_SANDBOX_REQUIRED` без разрешённого backend/spec,
`PARSER_OUTPUT_INVALID` для неверного результата и
`SECURITY_SANDBOX_CLEANUP_FAILED` без cleanup acknowledgement; действуют также
resource/time codes. Cancellation распространяется как `CancelledError` после
попытки ограниченного cleanup. Custom `ParserResourceGuard` теперь обязан иметь
`record_failure(code)` и закрывать run; no-op реализация не соответствует контракту.
Штатный `SecuritySession` уже реализует этот метод. Старые parser fingerprints и
signed-load bindings требуют миграции, описанной в [лимитах](resource-policy.md)
и разделе [PostgreSQL transaction](#postgresql-transaction).

Канонические требования: [§20.3][spec-roles], [§20.4][spec-ddl],
[§20.12][spec-audit], [§20.13][spec-sandbox] и [M14][spec-m14]. Долгоживущие
решения уже закреплены [ADR 0031](adr/0031-security-audit-and-parser-boundaries.md);
документационный шаг не вводит нового архитектурного решения.

[spec-roles]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#203-разделение-db-users
[spec-ddl]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#204-запрет-ddl
[spec-audit]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#2012-audit
[spec-sandbox]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#2013-изоляция-парсеров
[spec-m14]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m14-security
