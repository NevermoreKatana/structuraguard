# M4: аудит критериев приёмки

Дата: 2026-09-09. Основание: AC-01–AC-12 из
[плана M4](M04_technical_parsers.md), уточнения ADR 0004–0007.

## Результат и границы

Аудит добавляет только недостающие tests и точечные исправления четырёх выявленных
дефектов. Это не полная приёмка M4: AC-06, AC-07, AC-11 и deployment-часть AC-12
остаются частично подтверждёнными. Неизменённые старые tests, lint/typecheck и
security controls не отключались. LLM/DB/semantic modules не изменялись.

## Матрица «критерий → наблюдаемый тест»

Все пути `tests/...` ниже относятся к `packages/structuraguard/`.
«Подтверждён» означает наблюдение на проверенном corpus, не доказательство для
всех возможных документов. Для каждой строки также запускается общий suite.

| Критерий | Наблюдаемые tests | Статус |
|---|---|---|
| AC-01 — Parser/registry/facades совместимы | `tests/unit/parsers/test_parser_output_contract.py::test_parser_port_exposes_only_physical_input_and_output_boundary`; `test_registry.py::test_registries_are_instance_local_and_snapshots_are_canonical`; `tests/unit/test_facades.py::test_facade_creates_an_instance_local_default_parser_registry`; общий `assert_parser_contract` используется A–F | Подтверждён |
| AC-02 — DTO, порядок, continuation, legacy | `tests/unit/contracts/test_m04_text_contracts.py::test_legacy_extracted_contract_defaults_remain_accepted`; `test_m04_json_contracts.py::test_manifest_accepts_consecutive_closed_tree_segments`; `tests/unit/parsers/test_m04_stream_integrity.py::test_legacy_schema_1_0_keeps_opaque_fingerprint_compatibility`; `tests/unit/parsers/builtin/test_documents.py::test_docx_order_runs_and_provenance` | Подтверждён в границах fidelity ADR |
| AC-03 — только physical output, без semantic authority | Новый `tests/unit/parsers/builtin/test_m04_acceptance.py::test_parser_implementations_cannot_reference_semantic_plan_or_normalized_types`; `tests/unit/ports/test_m03_parser_static.py::test_m04_parser_implementations_have_no_analysis_or_plugin_execution_path`; `test_parser_output_contract.py::test_registry_rejects_business_object_before_yield`; security corpus inert payloads | Подтверждён наблюдаемыми static/runtime checks; HTTP исключение только ADR 0007 |
| AC-04 — content ownership всех adapters | Новый `tests/unit/parsers/builtin/test_m04_acceptance.py::test_all_core_adapters_select_by_content_independently_of_registration_order`: 13 форматов × 2 порядка регистрации при нейтральных MIME/extension; старые `test_builtin_selection.py`/`test_delimited.py` проверяют misleading hints, conflicts; `test_tika.py::test_tika_does_not_claim_core_formats` | Подтверждён для selection corpus |
| AC-05 — raw values/порядок/duplicates/missing | Новый `tests/property/parsers/test_text_properties.py`; старые `test_delimited_properties.py`, `test_json_properties.py`, `test_markup_properties.py`, `test_document_properties.py`; новый `test_pdf_properties.py`; exact fixtures `tests/unit/parsers/builtin/test_documents.py` | Подтверждён для поддержанной физической модели |
| AC-06 — bounded streaming, measured peak, tracking | `tests/security/parsers/test_text_security.py::test_parser_yields_first_complete_batch_before_eof`; `tests/unit/parsers/builtin/test_markup.py::test_first_batch_does_not_require_full_source`; `test_text.py::test_one_byte_reader_preserves_long_unicode_line_without_fragments`; `tests/unit/parsers/test_m04_stream_integrity.py::test_schema_1_1_caps_indexed_refs_across_batches`; document worker lifecycle tests | Частично: нет RSS/CPU measurements для каждого materialized part/backend call |
| AC-07 — каждый limit, overrides, N±1, no accepted failure | Новый `tests/unit/parsers/builtin/test_m04_limit_options.py`: finite/frozen defaults и invalid runtime overrides A/C/D/E/F; новый `test_m04_document_boundaries.py::test_document_exact_limit_preserves_typed_resource_and_no_terminal_on_failure`: cells/blocks/pages при N−1/N/N+1; старые format limit/security tests и `tests/unit/ports/test_m04_source_context.py` | Частично: не все поля markup/document limits проверены на каждой точной границе |
| AC-08 — различимые typed outcomes и backend defects | `tests/unit/parsers/test_m04_typed_errors.py`; `tests/security/parsers/test_document_security.py::test_pdf_no_text_layer_survives_registry`; новый `test_m04_acceptance_security.py::test_worker_failures_are_not_misreported_as_malformed_source`; `tests/unit/parsers/builtin/test_documents.py::test_spoofed_extension_and_malformed_document`; Tika unavailable/timeout tests | Подтверждён для error corpus, включая crash/frame/RuntimeError |
| AC-09 — malicious files, без execution/XXE/leak | `tests/security/parsers/test_markup_security.py`: XXE/Billion Laughs, YAML objects/alias bomb, HTML XSS; `test_document_security.py`: ZIP traversal/symlink/bomb, macros/actions/relationships; новый `test_m04_acceptance_security.py`: strict OOXML до read, error resource allowlist | Подтверждён для malicious corpus; не OS sandbox certification |
| AC-10 — contract/fixtures/properties A–E | Общий `tests/contract_suites/parser.py::assert_parser_contract` в tests TXT/LOG/MD/CSV/JSON/XML/HTML/YAML/XLSX/PDF/DOCX; новые `test_text_properties.py`, `test_pdf_properties.py`, `test_m04_document_boundaries.py::test_empty_ooxml_container_passes_shared_parser_contract`; старые encoding/malformed/cancellation/security fixtures | Подтверждён в реализованном scope, deferred capabilities отдельно ниже |
| AC-11 — lazy extras, side effects, base/all environments | `tests/packaging/test_metadata.py`; `tests/smoke/test_import_side_effects.py`; `test_markup.py::test_missing_optional_backend_is_typed`; `test_documents.py::test_document_missing_extra_and_record_limits`; `scripts/verify_distribution.py` проверяет isolated base wheel/sdist imports | Частично: dev с parser extras + isolated base, но не отдельное полное `all-extras` окружение |
| AC-12 — optional F gate | `tests/unit/parsers/builtin/test_tika.py`; `tests/property/parsers/test_tika_properties.py`; `tests/security/parsers/test_tika_security.py`; `tests/integration/test_tika_fake_server.py`; новый `test_m04_acceptance_security.py::test_tika_expired_deadline_does_not_cancel_consumer_between_batches` | Client подтверждён; реальный server isolation/deployment не проверен |

AC-12 читается вместе с позднейшим уточнением F и ADR 0007: изоляцию Tika
обеспечивает caller. Старое требование SDK sandbox нельзя считать выполненным
по результатам fake HTTP tests. При подготовке ручного commit 2026-09-09 checkboxes
плана сверены вручную: AC-01–05/08–10 отмечены в подтверждённом scope,
AC-06/07/11/12 оставлены открытыми. Результаты ниже — исторический срез аудита;
свежие команды и итоговый статус находятся в разделе передачи плана M4.

## Выявленные дефекты и минимальные исправления

1. Aggregate selection: YAML probe пытался читать валидный NDJSON как один YAML
   document и прерывал registry. Полный валидный JSONL sample теперь отклоняется
   YAML probe в пользу JSON adapter; malformed YAML не скрывается fallback.
2. Aggregate selection: text/JSON probes декодировали ZIP bytes до проверки
   применимости и блокировали XLSX/DOCX. Явные ZIP/PDF signatures отклоняются
   textual probes до encoding heuristics; MIME/extension сами ничего не разрешают.
3. Exact limits: worker передавал только код, теряя `resource`/`limit`. Теперь
   transport сохраняет только allowlisted resource и bounded integer; оба конца
   проверяют metadata. Error snippets и чужие fields не переносятся.
4. AC-08 failure injection: crash worker, повреждённый frame и backend RuntimeError
   маскировались как malformed source. Теперь это `PARSER_OUTPUT_INVALID`.
   Известный `PyMuPDF.FileDataError` отдельно сохраняет `PARSER_MALFORMED_INPUT`;
   прежний regression test не ослаблен.

Изменены только соответствующие helpers/probes в `parsers/builtin/`, worker/PDF
error boundary, шесть новых test files и документация. Dependencies и lockfile
этим аудитом не менялись; старые рабочие изменения M4 сохранены.

Production files относительно `packages/structuraguard/src/structuraguard/`:
`parsers/builtin/_common.py`, `_json.py`, `text.py`, `log.py`, `markdown.py`,
`delimited.py`, `yaml.py`, `_documents.py`, `_document_worker.py`, `pdf.py`.
Документация: этот отчёт, план M4, ADR 0006, `docs/public-api.md`,
`docs/codex/PROJECT_STATE.md` и навигация `mkdocs.yml`.

## Команды и фактические результаты

Новые tests запускались отдельно до module suites. Первые прогоны воспроизвели
selection failures, потерю limit details и три ошибки классификации worker.
При создании tests также исправлены два неверных ожидания нового harness:
реальный adapter ID — `builtin.json`, whole-line provenance не обещает columns.

Новые файлы и команда отдельного прогона:

```bash
.venv/bin/pytest -q \
  packages/structuraguard/tests/unit/parsers/builtin/test_m04_acceptance.py \
  packages/structuraguard/tests/unit/parsers/builtin/test_m04_limit_options.py \
  packages/structuraguard/tests/unit/parsers/builtin/test_m04_document_boundaries.py \
  packages/structuraguard/tests/property/parsers/test_text_properties.py \
  packages/structuraguard/tests/property/parsers/test_pdf_properties.py \
  packages/structuraguard/tests/security/parsers/test_m04_acceptance_security.py
```

Один совместный прогон первых 95 новых cases: `95 passed`; добавленный затем
static semantic-boundary case отдельно: `1 passed`. Повтор узких suites после
исправлений: `556 passed in 39.52s`. Финальный отдельный прогон всех шести новых
файлов: `96 passed in 10.42s`.

```bash
.venv/bin/pytest -q packages/structuraguard/tests/unit/parsers/builtin \
  packages/structuraguard/tests/property/parsers \
  packages/structuraguard/tests/security/parsers
make check test-integration test-security
git diff --check
```

Итог повторного `make check test-integration test-security` — exit code 0:

- Lock check успешен; Ruff format — `131 files already formatted`, lint —
  `All checks passed`; strict mypy — `129 source files`, без ошибок.
- Полный pytest — `1550 passed, 5 warnings in 46.63s`.
- Strict MkDocs — успешно. Первый прогон остановился на неверном anchor нового
  отчёта; ссылка исправлена, strict policy не менялась.
- Offline wheel/sdist build, rebuild из sdist и isolated base import —
  `distribution verification OK`.
- Integration — `9 passed, 1541 deselected, 5 warnings in 1.82s`.
- Security — `199 passed in 13.22s`.
- `git diff --check` — exit code 0.

Пять warnings — существующие upstream PyMuPDF SWIG deprecations; они не подавлены.
Review по parser/security checklist не потребовал несвязанного рефакторинга:
error transport ограничен закрытым набором resource names, старые malformed
regressions сохранены, нет новых import side effects или semantic authority.

Первый PDF property run в ограниченной среде вернул `SECURITY_SANDBOX_REQUIRED`:
был недоступен `/bin/ps`. Повтор с разрешённым RSS watchdog прошёл (`1 passed`),
контроль памяти не отключался. Тесты не используют реальные LLM API или внешний Tika.

## Непроверенные сценарии и причины

- Полный measured peak/RSS/CPU для каждого container part/native call и больших
  adversarial inputs: нужен отдельный reproducible performance corpus/runner.
  Малые fixtures, caps и lifecycle не заменяют такое измерение (AC-06).
- Все exact hard-cap/runtime N±1 сочетания markup/document limits: этот аудит
  проверил отсутствовавшую валидацию overrides и критичные document boundaries,
  но не строил исчерпывающий ресурсный corpus (AC-07).
- Отдельная установка всех `all` extras вместе с DB/LLM bundles не выполнялась:
  использовались locked dev parser dependencies и isolated base verification;
  это не all-extras compatibility evidence (AC-11).
- Реальный Tika/JVM, deployment pinning/fidelity, network/container isolation,
  DNS rebinding и DLP caller: нет caller deployment; tests используют fake server
  и не сертифицируют удалённую среду (AC-12).
- M12 sandbox, Windows document worker, OCR, LOG user regex и общий deadline A
  остаются deferred ранее утверждённым scope; этим аудитом capabilities не добавлялись.
- PDF property corpus использует воспроизводимый ASCII text layer; произвольные
  embedded fonts/ligatures/reading order и все OOXML layout extensions не обещаются.
- Remote CI и другая OS/Python matrix не запускались; результаты локальны для
  текущего Python 3.12/macOS окружения.
