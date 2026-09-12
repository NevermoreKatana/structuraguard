# ADR 0022 — conservative normalization и validation evidence

Статус: предложено для реализации M12. Дата: 2026-09-12.

Уточнение scope 2026-09-12: текущая задача реализует самостоятельный scalar
registry из A. Для него принято отдельное immutable `NormalizationResult`, которое
хранит raw/input/output, per-step trace и optional исходный `NormalizedValue`.
Batch schema 1.3.0 и record-level Validation Engine остаются предложением ниже.
Это позволяет сохранить wire/hash M2/M5/M8/M11 до отдельной задачи интеграции.
Подробности реализованного API: [scalar normalization](../normalization.md).

## Контекст

`NormalizedValue.transformations` при наличии `selection` описывает только
операцию ParsePlan. Добавление normalizers в этот tuple нарушит существующий
контракт. M11 требует преобразованные значения до проверки MappingPlan;
сам MappingPlan не содержит исполняемых transformations. Общий `ValidationReport`
не имеет bound manifest и поэтому запрещает physical refs в issues.

§15–16 ТЗ требуют conservative normalization, JSON Schema Draft 2020-12,
DB/business constraints, проверяемое происхождение и полный отчёт ошибок.
CHECK metadata, schemas, rules и сохранённые wrappers остаются недоверенными.

## Предлагаемое решение

- Выполнять normalizers после verified ParsePlan selection и до M8–M11.
  Raw values, origins и selection неизменяемы. Derived normalized schema 1.3.0
  добавляет отдельную ordered normalization trace и upstream/policy/registry
  bindings. Legacy versions сохраняют wire/hash и прежние invariants.
- Instance-owned registry фиксируется на run. Разрешён только deterministic
  pure вызов по известному ID/version с typed config. Locale выбирает trusted
  policy; hints и confidence не разрешают неоднозначную конверсию. Неудачная
  цепочка не применяется. Смена policy создаёт новые downstream artifacts.
- Уровни: bounded intake → normalization → types → JSON Schema → DB constraints
  → business rules → финальная provenance/aggregation. Предварительная проверка
  lineage и ownership выполняется до использования каждого недоверенного значения.
  Normalization и record engine остаются отдельными сервисами; record validation
  не меняет значения и не вызывает LLM.
- Создать `RecordValidationReport` с named bindings, per-check outcomes,
  безопасными ordinal locations и provenance refs только из проверенного replay.
  Старый `ValidationIssue` переиспользуется; старый report получает явную summary
  проекцию без refs. All-errors учитывает зависимости и конечные budgets.
- JSON Schema adapter использует Draft 2020-12 и локальный registry без retrieval;
  непроверяемые features отклоняются явно. Schema defaults не изменяют instance.
  Business rules — закрытый typed DSL с прямым dispatch. Нет `eval`, `exec`,
  expression strings, dynamic imports, arbitrary callbacks или SQL из metadata.
- Local DB predicates отделены от read-only `ConstraintReader`. Только DB adapter
  формирует параметризованные запросы по allowlisted refs. Неизвестный CHECK или
  отсутствие доказательств UNIQUE/FK означает unverified, а не pass. Preflight
  snapshots не отменяют повторные проверки и enforcement внутри load transaction.
- Provenance доказывается bounded physical replay и повторным применением
  selection/normalization. Самосогласованные hashes/wrappers не являются authority.
  Для DB lookup keys эта цепочка проверяется до read-only I/O; финальное сведение
  provenance закрывает coverage всего потока.
  ACCEPTED возможен только после EOF/cleanup без ошибок и unverified prerequisites.

## Альтернативы и последствия

На этапе scalar registry pure locale helpers выделены из M8 в
`domain/scalar_grammar.py`; прежняя inference semantics сохранена. Domain использует
только stdlib `datetime`, `decimal`, `re` и contracts. Normalizers вводят более
строгие conversion checks поверх общих candidates, не меняя ranked inference.

1. Дописать операции в прежний `transformations` или добавить их в MappingPlan:
   проще по количеству полей, но смешивает selection/normalization/mapping и
   нарушает ADR 0003/0021. Выбрана отдельная versioned trace.
2. Изменять значения во время record validation: меньше проходов, но результаты
   зависят от порядка правил и устаревает M11 evidence. Выбран отдельный этап L1.
3. Ослабить запрет refs в общем report: меньше DTO, но нет доказанной provenance
   boundary. Выбран отдельный bound report с совместимой summary-проекцией.
4. Исполнять CHECK text или передать DB handle правилам: шире покрытие, но
   недоверенные данные получают полномочия. Выбраны typed predicates, отдельный
   read-only port и явные неподтверждённые условия.

Цена решения — новые artifact versions, replay и ограничение автоматического
acceptance при неподдержанных DB/schema features. Пользовательская БД не
мигрирует; staging, writer, transactional enforcement и facade остаются отдельной
работой. Для scalar registry действует принятое уточнение scope выше; остальные
предложения ещё не меняют runtime поведение.

Связано: [исполняемый план M12](../plans/M12_validation_engine.md),
[ADR 0003](0003-two-stage-parsing-contracts.md),
[ADR 0010](0010-verified-parse-plan-execution.md),
[ADR 0018](0018-bounded-normalized-profiling.md),
[ADR 0021](0021-mapping-plan-validation.md).
