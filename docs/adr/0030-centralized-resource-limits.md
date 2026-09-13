# ADR 0030 — Централизованные resource limits M14

Статус: принято. Дата: 2026-09-13. Дополняет ADR 0002, 0012 и 0028.

## Контекст

Parsers, LLM router и DB adapters уже имеют локальные ограничения и безопасные
builders. Независимые экземпляры adapters могут каждый израсходовать собственный
budget. Нужен единый максимум run без переноса parsing, SQL или routing в security.
Текущий срез M14 реализует resource policy; DLP, HMAC chain и sandbox runner
остаются в отдельном плане.

## Решение

`contracts/security.py` содержит frozen `SecurityLimits`, `SecurityPolicy` и
закрытый `ResourceAuditEvent`. `security.SecuritySession` владеет явными часами,
общими reservations и одним terminal event. Глобального состояния и import-time
I/O нет. Domain-компиляторы зависят только от contracts; adapters используют
`ports/resources.py` и существующие local guards.

Эффективные limits вычисляются через minimum. Security layer повторно использует
проверки до parser accumulation, LLM egress и SQL statement creation. Там, где
общего ограничения не было, добавляются cumulative decoded text checks и ширина
JSON/YAML mappings, DOCX/PDF tables. Общие LLM и DB reservations происходят до side effect,
атомарны между экземплярами и не возвращаются после ошибки.

File/stream snapshot проверяет блок до append; транспорт принадлежит host.
Парсинг разрешён только для явно перечисленных builtin implementations с
`parser_trust="trusted"`. Default sandbox requirement приводит к отказу, пока
runner отсутствует. Это не новая реализация sandbox и не замена XXE/ZIP/SQL guards.

LLM policy сужается до approval; DB inspection limits — до target fingerprint.
Старые API без resource guard сохраняются. Host обязан передать один guard всем
adapters run; библиотека не защищает host от намеренного обхода внутри процесса.

Read-only/parser/LLM awaits используют общий deadline с post-return check.
Transactional loader отдельно передаёт оставшееся время в существующий runner
и проверяет его перед COMMIT. После подтверждения COMMIT deadline не меняет
результат; неизвестный COMMIT сохраняет `LOAD_OUTCOME_UNKNOWN`. Cleanup/rollback
не расходуют query reservations и используют собственный bounded budget.

Audit evidence содержит только opaque UUID, policy fingerprint, enum codes,
UTC и counters. Первый terminal event закрывает run; сообщения exceptions и raw
restricted data не сериализуются. Запись в durable sink и HMAC пока не реализованы.

## Последствия и проверки

Budget counters не являются счётом LLM invoice или ограничением RSS. Probe/decoder
имеют bounded scratch; snapshot при завершении временно копирует payload.
Cooperative timeout не прерывает произвольный синхронный/native code. Resource
evidence не доказывает полноту аудита и не предоставляет permission на egress/DB.

Регрессии проверяют N/N+1, отсутствие I/O после отказа, общий budget нескольких
adapters, отмену и safe events. Реальные PostgreSQL 16/18 проверяют rollback после
DML, dry-run budget и неизменность подтверждённого COMMIT при позднем deadline.
