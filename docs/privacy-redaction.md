# Классификация и маскирование M14

`ContentProtector` локально сканирует текст, определяет класс данных и заменяет
найденные значения непрозрачными placeholders. Настройки задаёт доверенный host;
данные источника не могут отключить встроенные детекторы или разрешить restore.
Новый API не меняет `LocalPIIClassifier`, parser values и существующие LLM gates.

<!-- example:m14-redaction:start -->
```python
import asyncio
from uuid import uuid4

from structuraguard.security import ContentProtector, CustomPattern, DetectionPolicy

async def main() -> None:
    protector = ContentProtector(DetectionPolicy(
        custom_patterns=(CustomPattern(pattern=r"CLIENT-[0-9]{4}"),),
        custom_secret_fields=("serviceCredential",),
    ))
    result = await protector.redact_fields(
        {"contact": "alice@example.test", "serviceCredential": "synthetic-value"},
        run_id=uuid4(),
    )
    assert result.classification.value == "RESTRICTED"
    assert result.handle is None  # Необратимое маскирование по умолчанию.
    summary = result.safe_summary().canonical_json()
    assert "alice@example.test" not in summary
    assert "synthetic-value" not in summary

asyncio.run(main())
```
<!-- example:m14-redaction:end -->

Имена enum и существующие значения JSON — `PUBLIC`, `INTERNAL`, `CONFIDENTIAL`,
`RESTRICTED`.
Без findings по умолчанию возвращается `INTERNAL`. `PUBLIC` требует явного
`baseline=DataClassification.PUBLIC`. Findings PII повышают класс минимум до
`CONFIDENTIAL`; credentials, private keys и card-like значения — до `RESTRICTED`.
`minimum_classification` сохраняет уже известный класс. `CategoryRule` может
повысить категорию; понижение ниже её минимального класса запрещено. Маскирование
сохраняет класс исходных данных и само по себе не разрешает egress.

## Настройки и границы сканирования

`classify(text)` и `redact(text, run_id=...)` принимают точный `str`, включая
опциональный `field_name`. `classify_fields`/`redact_fields` принимают точный
`dict[str, str]`: проверяются и имена, и значения. Nested values, bytes, произвольные
объекты/Mapping и некорректные Unicode surrogates отклоняются без `str(value)`.
Парсинг JSON, OCR и открытие файла остаются ответственностью существующих adapters.

`classify_chunks` принимает async stream строк, проверяет лимит перед сохранением
каждого chunk, затем объединяет bounded текст. Так совпадение на границе chunks
не теряется. Это конечная буферизация, а не сканер произвольного бесконечного
потока: transient память включает chunks и объединённую строку. Transport и
закрытие внешнего источника принадлежат host; cancellation распространяется в
ожидающий iterator. Синхронно блокирующий сторонний iterator требует isolation.

| `ScanLimits` | Default | Семантика |
|---|---:|---|
| `max_chars` | 65 536 | Сумма символов names и values/chunks; проверка до encoding/копирования |
| `max_bytes` | 262 144 | Сумма UTF-8 bytes; строка предварительно ограничена `max_chars` |
| `max_fields` | 128 | Число полей до обхода dict |
| `max_chunks` | 1 024 | Число принятых chunks, включая пустые |
| `max_findings` | 1 024 | До добавления finding; пересечения считаются отдельно |
| `max_work` | 100 000 000 | Консервативная стоимость regex passes до их запуска |
| `max_time_ms` | 1 000 | Полная операция: cooperative deadline и async timeout |
| `max_output_chars` | 262 144 | Сумма names/values после замены, до сборки output |

Для cardinality/size допускается ровно N, N+1 отклоняется. Для deadline/TTL
`elapsed >= limit` уже означает истечение. Work units — оценка, не CPU-инструкции.
`resources=SecuritySession(...)` дополнительно применяет общий run deadline,
cancellation и безопасный terminal resource event. Effective scan policy может
только ужесточить общие caps текста и времени. `policy` — read-only snapshot.

При исчерпании budget не возвращается частичный `clean`/PUBLIC report. Ошибки:
`SecurityPolicyError` с кодами `SECURITY_LIMIT_EXCEEDED`, `PROCESSING_TIMEOUT`,
`SECURITY_INPUT_REJECTED`, `SECURITY_POLICY_INVALID`, `SECURITY_PATTERN_UNSUPPORTED`,
`SECURITY_SCAN_FAILED`, `SECURITY_REDACTION_DENIED`. Сообщения фиксированы и не
содержат input, regex или credentials; исходный exception chain скрыт. Cancellation
сохраняет `CancelledError` без исходных аргументов. Внешние ports обязаны соблюдать
такой же контракт ошибок.

## Детекторы и precision

Все примеры синтетические. Категории — сигналы; они не подтверждают принадлежность
документа человеку, наличие счёта или работоспособность ключа.

| Категория | Проверка и настройка | False positives | False negatives |
|---|---|---|---|
| Email | ASCII address с bounded local/domain; label `email` маскирует всё значение | Пример адреса в документации | `name [at] host`, Unicode/обфускация без label |
| Phone | ASCII digits и ограниченные пробелы/скобки/дефисы; `phone_mode="international"` требует `+` | Длинный номер заказа в default `broad` | Короткие/local телефоны, нестандартные разделители; international пропускает номер без `+` |
| ИНН | 10/12 digits; default `inn_validation="pattern"`, опционально checksum из существующего profiler helper | Номер заказа такой же длины, особенно в pattern mode | Zero-width/fullwidth digits, опечатка при checksum mode |
| СНИЛС | Shape `123-456-789 00` и допустимые варианты; без checksum | Серийный номер сходной формы | Неизвестные разделители и обфускация |
| Паспорт РФ | Shape серии и номера с пробелом, например `45 12 345678`; labels маскируют всё | Номер детали той же формы | Слитная строка вне passport field не получает category passport; может совпасть с INN/phone |
| Card-like | 13–19 ASCII digits, spaces/hyphens; bounded Luhn; одинаковые digits отвергаются | Тестовый PAN и случайный Luhn-valid ID | Unicode PAN, неизвестные separators, неверная checksum без card label |
| Secrets | Известные API-key prefixes, JWT shape, Bearer, password/token assignments, credential URI, private-key blocks | Синтетические ключи, примеры, слова в credential fields | Неизвестный bare token, encoding/split-secret, произвольный формат без label |
| Field labels | Password/secret/token/API-key/DSN/private-key, PII labels и ФИО; полное значение | `password_policy`, `token_count`, `имя` объекта | Непредусмотренный alias без custom field |

Private-key block без closing marker маскируется до конца текста. Значения quoted
password assignments учитывают escape sequences. Credential URI не открывается
и не проверяется по сети. Для PAN нет сетевого запроса, преобразования огромного
числа или попытки проверить владельца. ФИО определяется по label; NLP вне scope.

`custom_patterns` — максимум 32 выражения длиной до 256 ASCII chars. Они только
добавляют findings: минимум `CONFIDENTIAL`, default `RESTRICTED`. Переиспользуется
закрытая grammar JSON Schema validator: literals/classes, fixed repeats до 256,
один variable repeat только в anchored expression. Группы, alternation, dot,
lookaround, backreference и empty-match запрещены. Неподдержанное выражение
отклоняется при создании protector; fallback к произвольному `re` отсутствует.
Custom fields нормализуются как встроенные labels: casefold и alphanumeric.

FP/FN fixtures: `tests/fixtures/privacy/precision.json`. Они закрепляют известные
ограничения конкретного режима, а не оценивают качество на реальных персональных
данных. Отсутствие finding не доказывает отсутствие PII/секретов. Egress policy
должна учитывать исходную классификацию и uncertainty отдельно.

## Отдельная обратимая карта

Для reversible mode установите `structuraguard[security]`. Только этот режим с
конкретным encrypted store использует optional `cryptography`; базовая установка
содержит классификатор и необратимое маскирование без него.

<!-- example:m14-restore:start -->
```python
import asyncio
import secrets
from uuid import uuid4

from structuraguard.security import (
    ContentProtector, DetectionPolicy, EncryptedMemoryPlaceholderStore,
    PlaceholderMapPolicy, restore_text,
)

async def main() -> None:
    run, writer, reader = uuid4(), uuid4(), uuid4()
    store = EncryptedMemoryPlaceholderStore(
        key=secrets.token_bytes(32),
        policy=PlaceholderMapPolicy(
            runs=(str(run),), writers=(str(writer),), readers=(str(reader),),
            max_ttl_seconds=300,
        ),
    )
    try:
        protector = ContentProtector(DetectionPolicy(redaction_mode="reversible"))
        masked = await protector.redact(
            "password=synthetic-value", run_id=run, store=store, principal=writer,
        )
        raw = await restore_text(masked, store=store, run_id=run, principal=reader)
        assert raw == "password=synthetic-value"
        assert masked.handle is not None
        await store.discard(handle=masked.handle, run_id=str(run), principal=str(writer))
    finally:
        store.close()

asyncio.run(main())
```
<!-- example:m14-restore:end -->

`PlaceholderStore` — отдельный port; result содержит только masked fields, safe
findings и opaque handle. Raw map не добавляется к prompt/report/audit. Встроенный
`EncryptedMemoryPlaceholderStore` хранит только ciphertext записей; key/cipher
context живёт в памяти процесса и задаётся явно. Импорт SDK не получает ключ,
не читает окружение и не создаёт store. AES-256-GCM, случайный 96-bit nonce и AAD
с handle/run/expiry обеспечивают authenticated encryption; решение и dependency
описаны в [ADR 0032](adr/0032-content-classification-and-redaction.md).

Пустые `runs`, `writers`, `readers` означают deny. Это allowlists UUID, полученных
от доверенного host authentication; передача известного UUID сама по себе не
аутентифицирует пользователя. Host не должен принимать эти значения и policy из
непроверенного пользовательского запроса. Один store применяет указанные principals
ко всем своим разрешённым runs; для разных ACL нужны отдельные stores. Handle не
является bearer permission. Авторизация проверяется до чтения/дешифрования.

Default TTL — 300 секунд, configurable до 3600, одновременно соблюдается cap store.
Истечение проверяется UTC и monotonic clocks; rollback часов закрывает доступ.
Default store caps: 100 maps, 1024 entries/map, 4 000 000 ciphertext bytes суммарно.
Размер JSON UTF-8 и AEAD tag проверяется до plaintext serialization/encryption.
Map capacity и unique replacements ограничены до вставки. Встроенный `put` атомарен
и не имеет await внутри; внешний adapter обязан обеспечить atomicity при cancel.

`get` удаляет запрошенный истёкший map; `purge_expired()` удаляет все истёкшие карты.
Host вызывает purge для idle store и `discard`/`close` по окончании работы. Скрытого
фонового timer нет. `rotate_key` отзывает все handles и удаляет ciphertext; старые
masking keys не удерживаются ради восстановления. [Audit key rotation](security-controls.md)
работает отдельно: новый `key_id` продолжает цепочку, старые verification keys
нужны для проверки истории. Используйте отдельный свежий ключ для каждого
store; не разделяйте ключ с audit или другими applications.

Одинаковое исходное значение в одной операции получает один placeholder; между
операциями namespace новый. Intersecting spans объединяются перед заменой. Вход с
зарезервированным `[SGR:` отклоняется. Restore требует точный исходный masked result:
чужой run, неизвестный token, изменённый текст/имя/порядок полей, tamper, истёкший
или отозванный handle дают отказ. Подстановка выполняется один раз, с проверкой
размера расширения до сборки. Произвольный ответ LLM через этот API не восстанавливается.
`restore_fields(..., max_chars=...)` допускает меньший cap результата.

Ошибки store: `SECURITY_MAP_ACCESS_DENIED`, `SECURITY_MAP_INVALID`,
`SECURITY_MAP_EXPIRED`, `SECURITY_MAP_KEY_INVALID`, `SECURITY_CRYPTO_UNAVAILABLE` и
общие budget/redaction codes. Raw secrets не входят в сообщения и repr payload.

## Reports, tests и остаточный риск

Для logging/report/audit sinks используется только `safe_summary()`:
classification, complete, scanned_chars, finding_count и закрытые category counts.
Нет raw values, labels, spans, raw hashes, pattern text и map handles. `repr(result)`
также использует summary. Masked content не является безопасным логом: он может
содержать нераспознанные данные. Явный restore возвращает raw restricted data,
которое caller обязан защищать; traceback capture с locals также остаётся у host.

| Control / trust boundary | Default refusal | Regression evidence | Residual risk |
|---|---|---|---|
| Raw input → scanner (`TB-01/07`) | Неподдержанные types, size/work/deadline overflow → typed error | `privacy/test_privacy_boundaries.py`, `test_privacy_policy.py`: N/N+1, chunk splits, timeout/cancel | Ограниченный Python regex pass не прерывается посреди native matching |
| Detectors → classification (`TB-07`) | Нельзя понизить PII/secret floor или выдать partial clean | `test_privacy_detection.py`, precision fixtures, property non-downgrade/Luhn mutation | Обфускация и неизвестные форматы; документированные FP/FN |
| Raw → masked result (`TB-07/08`) | Reserved tokens, output overflow, reversible без store → deny | Overlaps, repeated values, expansion limits и Unicode round-trip | Маскирование не убирает косвенные признаки и не разрешает egress |
| Result → protected map (`TB-08`) | Пустые ACL, foreign run, expiry, tamper, modified result → deny | `test_privacy_reversible.py`, `test_privacy_policy.py`: AES tamper, rotation, TTL, capacity | Host compromise, swap/core dumps; Python не гарантирует zeroization |
| Diagnostics → sinks (`TB-09`) | Raw отсутствует в summary/error/repr | Secret canaries, traceback и cancellation tests | Caller может самостоятельно раскрыть явные raw fields или logging locals |

Этот срез M14 реализует B1–B3 и safe summary часть B4. [Prompt-injection signals](prompt-injection.md)
добавлены отдельным control поверх обязательного host scanner. Полная DLP/egress
composition и safe HTML/CSV/XLSX exporters остаются отдельными controls [плана](plans/M14_security_layer.md).
[HMAC audit и runner boundary](security-controls.md) реализованы в M14 D;
OS isolation требует host backend.

Оба примера выполняются отдельно, без сети и реальных credentials. Копируйте
весь Python block; для второго нужен extra `security`. Regression tests извлекают
код непосредственно из этой страницы и проверяют masked class, safe summary,
round-trip и отзыв handle. Ошибки внешнего chunk iterator и map store также
санитизируются: `SECURITY_SCAN_FAILED` и `SECURITY_MAP_INVALID`, без raw diagnostics.

Канонические требования: [§20.10][spec-pii], [§20.11][spec-redaction] и [M14][spec-m14].
Распознавание ФИО из ТЗ не реализовано встроенными правилами; enum `PERSON_NAME`
не означает наличия NLP detector.

[spec-pii]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#2010-конфиденциальные-данные
[spec-redaction]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#2011-маскирование
[spec-m14]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m14-security
