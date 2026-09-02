# ADR 0002. Security defaults на внешних границах

Статус: принято для design M0.

Дата: 2026-09-02.

## Контекст

Source, parser/plugin output, metadata БД, mapping templates и LLM output
недоверенны. SDK встраивается в разные приложения и не может предполагать
наличие конкретного sandbox, secret store или audit backend. Неявный cloud
fallback либо in-process запуск опасного parser увеличивают blast radius.
ТЗ допускает для LLM более широкие advisory задачи, но утверждённый M0-plan
ограничивает её переданным candidate set.

## Решение

- Deterministic mapping работает без LLM и выполняется первым.
- Вызов LLM требует configured provider и разрешающей data-routing policy.
  Классификация и masking выполняются до primary call и fallback.
- `RESTRICTED` data по умолчанию не покидают process/trust domain. Отдельное
  разрешение не даёт LLM tools, credentials, DB connection или права выполнить
  output.
- LLM только ранжирует или выбирает из переданных candidates. Любой output
  соответствует strict schema, ссылается на candidate set и проходит
  независимую deterministic validation.
- In-process parser разрешён только для явно trusted input/adapter. Strict или
  untrusted run требует `SandboxParserRunner`; иначе run завершается
  `SECURITY_SANDBOX_REQUIRED`.
- Object registration означает trusted executable host code для parser,
  normalizer, validator, rule, listener, DB и LLM adapters. Untrusted parser
  передаётся декларативным descriptor; artifact resolution, import и constructor
  происходят только внутри sandbox. Другие untrusted executable extension types
  без sandbox protocol отклоняются.
- Security limits bounded и имеют безопасные defaults. Strict mode может вводить
  абсолютные caps, которые caller не повышает.
- Audit store, masking map, encryption/HMAC keys и retention задаёт caller через
  ports. Для real load durable core audit/outbox record участвует в target
  transaction; external listeners работают post-commit и не заменяют эту
  capability. Secrets и restricted raw values не входят в logs, errors или
  audit.
- DB inspection и write выполняются разными principals. Writer ограничен
  maximum allowlist из instance registry trusted composition owner; per-run
  selectors могут только сузить scope, а denylist имеет приоритет. Обе sessions
  подтверждают один immutable `target_identity` с
  `target_policy_fingerprint`; DDL и произвольные identifiers/SQL запрещены.

## Последствия

Некоторые deployments должны предоставить sandbox, transactional audit/outbox и
secure stores до обработки недоверенных файлов. Cloud LLM может не получить
достаточно context, но privacy policy имеет приоритет над mapping coverage.
Отсутствие обязательного control приводит к explicit failure, а не к
небезопасному fallback. SDK не защищает host от executable adapter, который
composition owner импортировал до регистрации.
Classification, explanation, normalization и schema proposal через LLM не входят
в M0; их добавление требует нового ADR и security review.

Связанные документы: [модель угроз](../threat-model.md),
[public API](../public-api.md), [архитектура](../architecture.md).
