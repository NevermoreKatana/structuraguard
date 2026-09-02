---
name: structuraguard-parser
description: Добавляет или изменяет parser adapter StructuraGuard для TXT, LOG, CSV, JSON, XML, HTML, XLSX, PDF, DOCX и plugins. Используй при detection, parsing, batching, limits и provenance.
metadata:
  author: structuraguard
  version: "1.0"
---

# Parser adapter

## Вход

Формат или parser behavior, обязательные limits, provenance и критерии приёмки.

## Результат

Parser adapter без влияния на orchestrator, регистрация, contract/security tests и документированные ограничения.

## Workflow

1. Прочитай `Parser` contract, registry behavior и нужный FR-раздел через `docs/codex/SPEC_INDEX.md`.
2. Определи probe signals: content signature, MIME, extension и conflict behavior.
3. Преобразуй источник в канонические batches; downstream не должен зависеть от исходного формата.
4. Сохраняй точный provenance: line, row/column, cell, JSON Pointer, XPath, CSS selector или PDF page/block.
5. Для потенциально больших форматов используй streaming/batching и enforce limits до накопления данных.
6. Разделяй parse error, unsupported feature, limit violation и security rejection.
7. Не исполняй macros/JavaScript/formulas/code и не загружай внешние ресурсы.
8. Добавь contract tests: valid, malformed, empty, encoding, boundary, oversized, cancellation и malicious input.
9. Убедись, что parser регистрируется без изменения orchestrator.

Форматные требования находятся в [чек-листе parser](references/PARSER_CHECKLIST.md).
