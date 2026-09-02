# Краткий контекст StructuraGuard SDK

## Продукт

StructuraGuard — встраиваемый Python SDK для безопасного импорта разнородных не-медиа данных в существующие реляционные БД.

Поддерживаемое ядро: TXT, LOG, CSV, TSV, JSON, JSONL, XML, HTML, XLSX, PDF с текстовым слоем, DOCX, YAML, Python `dict/list`, bytes и streams. Новые форматы подключаются как parser plugins.

## Главный pipeline

```text
Source → detection → parser → normalized model → source profiling
       → database inspection → candidate mapping → optional LLM mapping
       → MappingPlan validation → normalization → record validation
       → staging → transactional load → report + audit
```

## Собственная инженерная часть

- Parser protocol и registry.
- Unified normalized source model с provenance.
- Database catalog, schema fingerprint и FK graph.
- Deterministic mapper и LLM-assisted semantic mapper.
- Строгий декларативный `MappingPlan` без SQL.
- Multi-layer validation и normalization.
- Staging, `dry_run`, `insert/upsert`, idempotency и rollback.
- Security policy: file limits, XXE/YAML/HTML safety, prompt injection, PII routing, audit.

## Границы

- Изображения, видео, аудио и OCR не входят в MVP.
- Сканированный PDF без текстового слоя возвращает контролируемую ошибку.
- LLM — сменный адаптер, а не источник истины.
- LLM не видит credentials, не выполняет tools и не формирует SQL для исполнения.
- Изменение production schema запрещено; допустимо только предложение схемы для ручного подтверждения.

## Архитектура

```text
contracts ← domain ← pipeline
    ↑                    ↓
parsers / database / llm / validation / security / stores
```

Зависимости направлены к contracts/domain. Framework integrations располагаются в `apps/` или `examples/` и не импортируются ядром.

## Основной стек

- Python 3.12+, uv, `pyproject.toml`.
- Pydantic v2, SQLAlchemy 2.x, PostgreSQL, SQLite tests.
- `jsonschema`, orjson, HTTPX.
- pytest, pytest-anyio, Hypothesis, Testcontainers.
- Ruff, mypy, MkDocs.

## Правила результата

Каждый публичный результат должен быть типизирован и содержать достаточно metadata для воспроизводимости: source fingerprint, DB fingerprint, mapping plan version, provider/model, transformations, validation issues, provenance и security events.

## Где искать требования

Используй `docs/codex/SPEC_INDEX.md`. Полное ТЗ читай только выбранными разделами через:

```bash
python scripts/extract_spec_sections.py --list
python scripts/extract_spec_sections.py "FR-003" "M3. Parser Registry"
```
