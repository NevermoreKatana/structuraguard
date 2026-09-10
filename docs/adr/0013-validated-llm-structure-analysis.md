# ADR 0013 — LLM proposal и обязательная physical validation

Статус: принято для M6-C. Дата: 2026-09-10.

## Контекст

M2 уже определяет ParsePlan и LLMProvider; M4 выдаёт physical source; M5 умеет
проверять закрытые selectors по полному replay. Сетевой adapter M6-B поддерживает
strict schema registry. Сырой ответ модели не доказывает существование refs,
достоверность sample или готовность плана к исполнению. DB mapping не входит в M6.

## Решение

- `LLMStructureAnalyzer` реализует существующий `SemanticStructureAnalyzer` port,
  принимает concrete `LLMProvider`, trusted scanner и обязательный M5 validator.
  `replay` — factory полного physical stream, отдельный аргумент вызова; никакого
  source reader, filesystem handle или DB object внутри LLMRequest.
- Перед egress первый replay сверяет manifest и exact sample values/locations.
  Catalog ограничен policy; source IDs заменяются локальными aliases. Проекция
  observations сохраняет роли/числовые факты, source text передаётся только как
  явно недоверенные bounded samples и literal path facts.
- `LLMStructureSuggestion` — versioned wire grammar в contracts, состоящая из
  `SemanticPlanProposal` и closed selectors/entities. Поля required; metadata и
  final score модель не задаёт. Compiler строит существующий ParsePlan.
  `log_piece` переводится в `LogTokenSelector` внутри SDK: generic secret-key
  veto LLM DTO остаётся неизменным. Это уточняет предварительный план direct
  ParsePlan JSON Schema, сохраняя тот же конечный contract и validator.
- Prompt/version/schema factories регистрируются в HTTP adapter явно. Ответ
  проверяется по JSON/schema, IDs/paths, byte limits и source membership.
  Code/SQL/command/injection veto дополняет закрытую grammar; output не исполняется.
  SecurityApproval относится к точному minimized payload; scanner не заменяется
  фабрикой разрешений. Restricted cloud и unknown locality запрещены до вызова.
- Второй replay проходит настоящий `ParsePlanValidator.validate_source`.
  Accepted wrapper не заменяет будущую executor validation. Неполный finite
  log/document scope, несколько tree roots или обрезанный табличный конец
  не становятся успешным планом. Log event blocks могут дублировать те же строки.
- Self-confidence игнорируется. `candidate_min_v1` переносит minimum bound
  deterministic score; отсутствие кандидата даёт 0, competing candidates или
  blockers дают `NEEDS_REVIEW`. Это консервативная policy C, а не калиброванная
  semantic correctness estimate. Изменение scoring потребует новой policy version.
- Optional `ParsePlan.semantic_analysis` и `ParseField.locale_hint` доступны только
  в schema 1.1.0. Absent fields не сериализуются: legacy JSON/fingerprints прежние.
  В provenance — prompt/schema/request/generation hashes и provider/model;
  raw response, credentials и model self-confidence не сохраняются.
- Один вызов analyzer делает максимум один LLM request. Policy ограничивает
  samples/refs/output/plan; HTTP adapter обеспечивает context/token limits.
  Общий source deadline охватывает scan, generation и оба replay. Ошибки и
  cancellation не скрываются retry/fallback. Никаких новых dependencies.

## Последствия

Ограничения executable grammar M5 сохраняются: locale/type hints не преобразуют
raw values; XML matching, optional fields, JSONL multiple roots и document tables
не поддерживаются автоматически. Документные sections представлены explicit
record groups; отдельный chunk span/entity extraction остаётся M6-D.

Production PII scanner/redaction, router→analyzer composition, режимы Hybrid,
semantic report и facade wiring ещё не реализованы. В C caller явно выбирает
provider и `llm_assisted`/`llm_first`; `deterministic` даёт ноль LLM calls.
Нет новых DB capabilities. Real backend quality не выводится из fake HTTP tests.

Связанные материалы: [API и limits](../llm-semantic-parsing.md),
[план M6](../plans/M06_llm_semantic_parsing.md),
[ADR 0010](0010-verified-parse-plan-execution.md),
[ADR 0012](0012-policy-aware-llm-routing.md).
