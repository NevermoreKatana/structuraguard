# ADR 0014 — Hybrid parsing, document spans и terminal report

Статус: принято для M6-D. Дата: 2026-09-10.

## Решение

`HybridStructureAnalyzer` собирает M5 evidence полным physical replay. Default
`llm_assisted` принимает достаточный structural plan; неоднозначность вызывает один
bounded structural request. `llm_first` сохраняет обязательный deterministic pass,
а `deterministic` исключает LLM. Raw document-copy score M5 не удостоверяет entity
extraction; prose/text-layer documents используют bounded chunks в обоих LLM modes.

`LLMStructureAnalyzer.propose` сохраняет все source/security/validator checks C,
но передаёт окончательную оценку Hybrid. Прежний `analyze` и `candidate_min_v1`
не меняются. Новая `hybrid_v1` объединяет deterministic evidence, physical validation,
agreement и penalties. Model self-confidence не является evidence.

Document schema возвращает exact quotes/offsets, а не произвольные values.
Compiler создаёт `DocumentSpanSelector`/`DocumentSpanGrouping` в ParsePlan 1.2.0.
Anchor связывает occurrence; exact overlap дедуплицируется, conflicts/parent cycles
попадают в review. Каждый anchor/field span проверяется общим validator/executor.
Значения собираются из source, origins сохраняют refs/locations/spans. JOIN использует
один пробел (уточнение раннего плана с LF). Normalized spans требуют schema 1.2.0;
legacy serialization/hash остаются прежними.

XML scalar text с XPath проходит тот же bounded span flow. Это уточняет раннее
отложенное XML matching: executable XPath и универсальная element grammar не
добавляются. JSON collections по-прежнему используют закрытые tree steps M5.

`SemanticParsingSession` — отдельный публичный API над immutable replay. Validator
и executor повторно читают полный snapshot. Частичный accepted plan разрешает preview,
но issues запрещают terminal manifest и normalized fingerprint. `SemanticParseReport`
1.1.0 явно допускает отсутствие плана и содержит safe attempts/usage/coverage/issues.
Ошибки/отмена не выдумывают completed artifact. Saved plans не запускают LLM.

Concrete provider оборачивается run-local calls/tokens/time budget; provider lifecycle
остаётся у caller. Router B сохраняет отдельный contract coordinator согласно ADR 0012;
его автоматическая композиция с session и общая SDK ingest facade отложены. Новых
production dependencies, DB mapping, SQL/tools authority нет. Production PII scanner
не заменяется тестовой фабрикой approval.

## Последствия

Physical replay повторяется несколько раз, зато bounded memory, source identity и
terminal-only completion проверяются независимо от модели. Document plan хранит до
32 occurrences; связи вне доступного overlap и неподдержанный scope требуют review.
Score остаётся эвристикой. Actual defaults, public API и residual limits описаны
в [semantic parsing](../semantic-parsing.md); ранние proposed budgets M06 не являются
runtime configuration. Fake tests не оценивают качество реальной модели.
