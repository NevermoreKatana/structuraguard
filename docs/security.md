# Безопасность ParsePlan validation и execution

M5-C рассматривает source, profile, decoded plan и serialized validation wrapper
как недоверенные данные. Перед применением выполняются bounded preflight, проверка
закрытой discriminated schema, fingerprints/versions, allowlist operators,
physical replay и runtime selection checks. Подробный контракт:
[ADR 0010](adr/0010-verified-parse-plan-execution.md) и
[публичный API](public-api.md#parseplanvalidator-parseplanexecutor-m5-c).

Security review текущего M5 diff, исправления и regression tests:
[M05 security report](plans/M05_security_review.md).

- `eval`, Python/SQL/shell, callbacks, dynamic expressions и пользовательские
  regex не являются допустимыми plan operations. Source literals не исполняются.
- `ValidatedParsePlan` не заменяет повторную проверку context/source. Изменение
  snapshot, неизвестные refs, выход за ranges, overlapping/incomplete records
  и неоднозначный field path дают отказ.
- Resource limits применяются к input, plan, незавершённому record, tokenizer,
  fan-out, output batch, total records и manifest summaries. Sampling limits
  не используются как разрешение пропустить runtime verification.
  Record budget учитывается инкрементально до удержания следующего value/entity.
- Diagnostics содержат только codes, stages и counters. Raw values, paths с
  пользовательским содержимым и provider exception messages не копируются в errors.
- Non-terminal output требует staging. При source error/cancellation/cleanup
  failure успешного terminal manifest нет; частичные batches нельзя трактовать
  как завершённый dataset.

Profile и normalized output могут сохранять sensitive raw values/labels и origins;
их нельзя безусловно направлять в logs/LLM. Это не журнал безопасности и не redacted report.
Parser isolation/XXE/archive controls остаются на технической parser boundary.
