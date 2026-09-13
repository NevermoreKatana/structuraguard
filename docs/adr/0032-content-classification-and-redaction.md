# ADR 0032: ограниченная классификация и отдельная обратимая карта

Статус: принято. Дата: 2026-09-13. Scope: M14 B1–B3 и safe summaries B4.

## Контекст

Profiler уже распознаёт часть PII и сохраняет максимальную классификацию, но его
sampling не является полным DLP scan. LLM adapters уже проверяют egress approvals.
Новый control должен распознавать и маскировать содержимое, сохраняя эти границы.
Raw masking map нельзя помещать в report или provider context. Произвольные regex
и unlimited scan недопустимы на недоверенном входе.

## Решение

1. Ввести frozen `DetectionPolicy`, finite `ScanLimits`, findings только с offsets
   и закрытыми categories. `ContentProtector` сканирует целиком bounded строки/
   string fields/async chunks. Неизвестный тип и незавершённый scan дают typed
   refusal. Custom rules только добавляют findings, classification floors сохраняются.
2. Переиспользовать `DataClassification` и `maximum_classification`. Вынести
   существующие INN checksum и safe ASCII regex grammar в pure domain helpers;
   оставить compatibility imports profiler/validator. Новых parser/DB/LLM controls
   здесь нет. DLP report не является `SecurityApproval`.
3. По умолчанию irreversible placeholders; scan и mask внутри одной операции,
   без доверия caller-provided spans. Маскирование сохраняет classification.
   Reverse map вынести в `PlaceholderStore` с run/principal ACL и TTL. Для
   конкретного bounded memory adapter хранить только AEAD ciphertext; ключ
   передаётся отдельно и не сериализуется. Временный plaintext при scan/encryption/
   authorized restore неизбежен. In-process port не защищает от hostile host code.
4. Exact output binding, nonce namespace и одноразовая подстановка исключают
   restore изменённого контекста и рекурсивные placeholders. Rotation masking key
   отзывает старые handles. Это намеренное уточнение общего текста threat model:
   key_id/verification period для будущей HMAC chain не требуют retaining raw map.
5. Logs/reports используют `PrivacySummary`. Исключения — закрытые коды без raw
   input/adapter messages. Implicit logging и side effects при import отсутствуют.

## Dependency и криптография

Добавлен только optional extra `security = ["cryptography>=46,<51"]` и соответствующий
элемент `all`; base dependencies не меняются. Lock фиксирует `cryptography 50.0.1`
и transitive зависимости. Library нужна для стандартного AEAD; собственная
криптография и plaintext fallback отвергнуты. Import AEAD откладывается до явного
создания store; отсутствие extra даёт `SECURITY_CRYPTO_UNAVAILABLE`.

Используется AESGCM с 32-byte key, случайным 12-byte nonce и AAD handle/run/expiry.
API проверяет authentication tag до возврата plaintext; повтор nonce с тем же
ключом недопустим. Host выдаёт отдельный свежий key каждому store и ограничивает
его срок жизни. Это соответствует контракту [официального AESGCM API](https://cryptography.io/en/stable/hazmat/primitives/aead/).

Проект pyca публикует лицензию `Apache-2.0 OR BSD-3-Clause` в
[metadata проекта](https://github.com/pyca/cryptography/blob/main/pyproject.toml).
Поддержка Python и platform wheels описана в [installation guide](https://cryptography.io/en/latest/installation/).
Диапазон major versions ограничен; версии и hashes закреплены `uv.lock`.
Optional supply-chain/native-code поверхность добавляется только при установке
extra. Это обоснование выбора, а не утверждение об отсутствии уязвимостей.

## Последствия и проверка

API и precision ограничения описаны в [руководстве](../privacy-redaction.md).
Fixtures фиксируют известные FP/FN; properties проверяют Unicode round-trip,
непонижение класса и отклонение single-digit Luhn mutations. Boundary tests
проверяют scan/output/map caps, deadlines, cancellation, ACL/run/TTL/tamper/rotation,
reserved placeholders и отсутствие canaries в summaries/errors/repr.

Конечный Python regex pass и encryption не имеют hard preemption; враждебный host
или внешние blocking adapters требуют отдельной process isolation. Clipboard,
core dumps, swap и caller logging locals не защищаются этим in-process SDK.
Отсутствие finding не подтверждает PUBLIC, masking не выдаёт egress authority.
