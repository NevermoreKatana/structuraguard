# ADR 0029 — Ledger, quarantine и повторное исполнение M13-D

Статус: принято. Дата: 2026-09-13. Дополняет ADR 0028.

## Решение

Atomic остаётся режимом по умолчанию. Явный `quarantine_invalid` использует
неделимые группы source records и FK/parent/related dependencies. Global gates
не ослабляются. Только известные SQL constraint errors допускают rollback
savepoint группы; остальные ошибки отменяют всю transaction.

Для durable idempotency приложение настраивает PostgreSQL ledger той же БД и
передаёт key. Bootstrap выделенной `sg_staging_load_*` schema выполняется явно
администратором; runtime имеет только SELECT/INSERT, без DDL, UPDATE и DELETE.
Старый API без ledger сохраняется для atomic runs, но не обещает durable replay.
Quarantine требует ledger и key, чтобы outcome не зависел от staging finalize.

Transaction advisory lock по hash namespace/target/key сериализует одинаковые
запросы перед staging CAS; уникальный marker дополнительно защищает commit.
Hash binding содержит source/extraction/parse/normalized/mapping/database,
effective policy и версию исполнения. Transport batch size и run ID исключены.
Другой binding даёт IDEMPOTENCY_KEY_CONFLICT. Конкурент после rollback может
исполнить свежий sealed run; после commit возвращает исходный результат с
`replayed=True`, не выполняя target DML. Повтор того же rollback run требует
нового staging attempt. Namespace — trusted configuration, не граница DB tenants.

Marker, quarantine references и redacted audit записываются в T-load. Сбой их
записи откатывает target. Quarantine содержит hash ссылок и закрытые codes;
исходные данные/ошибки остаются в staging artifacts по retention policy.
Commit ledger не очищается автоматически вместе с payload: tombstone сохраняет
idempotency. Полный recovery result — чувствительный artifact, не log message.
Audit содержит только hashes, counts, mode/outcome и UTC время; secrets и values
не копируются. Неуспешные transactions оставляют staging status, а не ложный
committed audit event.

Schema fingerprint проверяется после планирования непосредственно перед DML и
повторно перед commit на той же connection. Table locks сохраняются; schema
metadata вне table locks может измениться и должна вызвать rollback. Deployment
по-прежнему согласует изменения functions/types с imports.

Потерянный ответ COMMIT остаётся UNKNOWN. Повтор с тем же key разрешается только
через primary ledger и тот же transactional lock. Сохранённый marker позволяет
восстановить committed result и завершить исходный UNKNOWN staging attempt.
Отсутствие marker не разрешает повторно писать из UNKNOWN/EXECUTING staging.

## Проверки

PostgreSQL 16/18: success, validation/FK/late SQL failure, cancellation, duplicate
и concurrent key, конфликт binding, rollback первой конкурирующей transaction,
schema drift перед DML/commit, quarantine dependency groups, audit failure и
recovery после потерянного COMMIT. Pure tests проверяют binding и group closure.
