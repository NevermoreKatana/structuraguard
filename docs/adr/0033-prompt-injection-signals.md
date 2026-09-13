# ADR 0033: prompt-injection signals как дополнительное ограничение LLM

Статус: принято. Дата: 2026-09-13. Scope: M14 C1, signal часть C2 и review bridge C3.

## Контекст

M6/M10 уже имеют закрытые provider DTO, отдельный untrusted payload, schema и
candidate/source validation, privacy-aware routing и literal active-content veto.
Переносить authority к эвристике опасно: отсутствие signal не доказывает безопасность.
Существующий `SecurityReport` не представлял review отдельно от security rejection;
выделенного signal evidence и ограничения local-only после scan не было.

## Решение

- `InjectionDetector` возвращает deterministic bounded evidence с original offsets,
  severity/action и обязательным `heuristic_not_exhaustive`. Unicode normalization
  идёт по отдельной копии. Sources, metadata и model output остаются данными.
  Existing active-content expression вынесено в pure helper без изменения veto.
- `InjectionAwareSecurityScanner` требует base `SecurityScanner`. Его allow outcome
  необходим; signals только сужают допуск. Run/policy bindings неизменны, original
  observations сохраняют риск после masking. Histories и aggregate signals конечны;
  failure/cancel запрещают повторное использование instance. Автоматическая сборка
  полной source/DLP/egress policy из M14 A/B в этом срезе не добавляется.
- Дополнить `SecurityReport` optional `injection` с исключением None из legacy
  serialization и decision `review`/status `NEEDS_REVIEW`. `SecurityApproval` по-прежнему
  требует `allowed`. Старый `prompt_injection_detected=True` без evidence не может
  стать allowed. Новое heuristic evidence с action observe/local-only допускает
  allowed только после base checks; исходный class не понижается.
- Проверять signal locality в существующем router и общей provider boundary,
  включая direct HTTP и single-provider run. Confidential/restricted правила
  остаются независимыми и действуют при каждом fallback. Tool capability запрещена.
- В M6/M10 обработать review как остановку generation; в semantic parsing исключить
  deterministic fallback с records после security review. Такой fallback ранее
  использовался для обычной semantic ambiguity и не должен обходить security outcome.
- Safe summaries отделены от payload/evidence locations. Report provenance включает
  fingerprints base decision и signal policy; fingerprints не заменяют host trust
  или будущую HMAC chain.

## Совместимость и альтернативы

Legacy reports без injection сохраняют прежнюю JSON shape; новая decision требует
от downstream consumers обработки `review` как отсутствия approval. Публичный
`require_request_binding` сверяет все outcomes до их использования. Legacy scanner
composition сохраняется; дополнительный control host подключает явно. Base scanner
с уже вложенным injection evidence отклоняется, чтобы избежать неявного ослабления
policy при наложении wrappers.

Отклонены auto-approval по отсутствию keywords, автоматическое исключение quoted
инструкций, очистка source перед проверкой и новый parallel router. Для произвольных
языков/encoding нельзя обещать полноту. Политика высокого риска допускает local-only,
review или block; critical exfiltration всегда block. Нет новых dependencies,
сетевых запросов или LLM-вызовов для самого detection.

## Последствия и проверка

Есть документированные false positives на учебных цитатах и false negatives на
обфускации/неизвестных формулировках. Cooperative timeout не является OS sandbox.
Host отвечает за регистрацию base scanner, authentication, deployment locality и
передачу original observations до masking. Полный threat-model milestone остаётся
частично реализованным: audit/HMAC и OS sandbox — отдельные controls.

Tests проверяют multilingual direct/indirect fixtures, original-location property,
N/N+1, bounded decode/normalization, cancellation, foreign evidence, risk после
masking, restricted/confidential fallback, direct HTTP gate, no-tools/system
separation и реальные M6/M10 review outcomes. Детали API и риска —
в [руководстве](../prompt-injection.md).
