# Conservative scalar normalization

Используйте `NormalizerRegistry`, чтобы явно преобразовать selected scalar,
сохранить raw value и получить историю каждого изменения. Все операции синхронны
и чисты: в них нет file/network/DB I/O, чтения environment, process locale и часов.

<!-- example:m12-normalization:start -->
```python
from decimal import Decimal

from structuraguard.contracts import (
    DecimalScalar,
    NormalizationPolicy,
    NormalizerSpec,
    StringScalar,
)
from structuraguard.contracts.profiling import LocalePolicy
from structuraguard.normalization import NormalizerRegistry

normalizers = NormalizerRegistry.with_builtins().freeze()
source = StringScalar(value=" 1 250 000,50 руб. ")
result = normalizers.normalize(
    source,
    steps=(
        NormalizerSpec(normalizer_id="trim"),
        NormalizerSpec(normalizer_id="money"),
    ),
    policy=NormalizationPolicy(locale=LocalePolicy.RU_RU, currency="RUB"),
)
assert result.accepted
assert result.raw_value == source
assert result.normalized_value == DecimalScalar(value=Decimal("1250000.50"))
operations = tuple(
    change.operation
    for step in result.steps
    for change in step.output.transformations
)
assert operations == (
    "trim", "remove_currency_symbol", "remove_group_separator",
    "replace_decimal_separator", "parse_decimal",
)
```
<!-- example:m12-normalization:end -->

`snapshot()` фиксирует registrations для run; дальнейшее расширение исходного
registry не меняет snapshot. `freeze()` дополнительно запрещает новые registrations.
ID/version разрешаются точно; одинаковая пара не может быть зарегистрирована дважды.
`fingerprint` snapshot связывает identities, а `result.fingerprint` — входы, policy,
snapshot и историю. Это canonical hashes SDK, не удостоверение автора или plugin code.

## Встроенные normalizers

| ID | Поведение и границы |
|---|---|
| `trim` | Python Unicode `strip()` с краёв строки; не выполняет NFC/NFKC и не меняет внутренние символы. Native non-string — no-op. |
| `empty_to_null` | Точное совпадение с `empty_tokens`, по умолчанию только пустая строка. Для whitespace сначала нужен `trim`. |
| `boolean` | `true/yes/да` и `false/no/нет` через casefold. Наборы `true_tokens`/`false_tokens` настраиваются и не могут пересекаться после casefold; нет numeric truthiness. |
| `integer` | ASCII integer и корректный grouping выбранной locale. Fractional notation, ведущие нули многозначной записи, bool и float отклоняются. Native integer — no-op; native Decimal принимается только с exponent 0. |
| `decimal` | Точное число без exponent notation в строке. Integer → Decimal, native Decimal сохраняет scale/sign. Float и bool отклоняются. Округления нет. |
| `money` | Те же точные числа; currency token требует явного `currency` (`RUB/USD/EUR/GBP`). Несовпадающая currency отклоняется, FX conversion отсутствует. При наличии значения результат всегда `DecimalScalar`; null означает отсутствие значения. |
| `date` | ISO `YYYY-MM-DD`; RU `DD.MM.YYYY`; US `MM/DD/YYYY`; GB `DD/MM/YYYY`. Проверяется реальный календарь. |
| `datetime` | Дата той же locale + `T`/пробел + `HH:MM[:SS[.ffffff]]` + явный `Z`/offset. Результат UTC; naive datetime и неизвестный offset `-00:00` отклоняются. Не подбирает timezone и не исправляет overflow/лишнюю precision. |
| `phone` | Проверяет форму номера с явным `+`, 7–15 ASCII digits и ненулевой первой цифрой; удаляет только пробел, NBSP, narrow NBSP, `-` и сбалансированные скобки. Не добавляет country code, не удаляет extension, не проверяет существование номера. |
| `email` | Консервативный ASCII dot-atom local part и DNS-shaped domain с точкой. Сохраняет local part, case и plus-tag; lowercase только domain. EAI/quoted local/domain literals отклоняются. DNS и доставка не проверяются. |
| `uuid` | 32 hex digits либо стандартные 36 символов с дефисами → lowercase canonical UUID. URN/braces не поддерживаются; исходный текст сохраняется. |

Все built-ins пропускают `NullScalar` как отсутствие nullable значения. Обязательность
поля проверяется отдельно через JSON Schema или DB constraints. Built-ins не принимают step parameters; их
настройки задаются в `NormalizationPolicy`. Custom normalizers получают отдельные
параметры своего шага.

Locale по умолчанию — `unspecified`: выполняются закрытые grammar checks всех
поддерживаемых вариантов. Если допустимых значений несколько, возвращается
`AMBIGUOUS_DATE` или `AMBIGUOUS_NUMBER`; единственный вариант и ISO допускаются.
Явная locale не заменяется другой при ошибке. Hints ParsePlan/M8 автоматически
не применяются. Grouping должен быть корректным; смешанные RU space separators,
Unicode digit lookalikes, currency mismatch и неразрешённые символы отклоняются.

## Raw value, ошибки и повторный вызов

`raw_value` сохраняет исходный scalar. В `normalize_value(existing_value, ...)`
используется `existing_value.normalized_value` после ParsePlan selection;
`source_value` хранит полный исходный `NormalizedValue`, включая raw parent,
origins, selection, transformations и source refs. Эти DTO не изменяются.

Каждый вызванный normalizer отражён в `result.steps`, включая no-op. Каждая
фактическая смена представления содержит operation, input и output в
`step.output.transformations`. При повторной нормализации уже canonical scalar
встроенная операция — no-op; она не изобретает новую transformation. Новый вызов
создаёт новый result с собственным raw input и не склеивает истории разных calls.

Цепочка атомарна для одного scalar. Например, `trim → date` для
`" 01/02/2026 "` без locale возвращает исходную строку и `AMBIGUOUS_DATE`.
Успешный trim остаётся в истории попытки; ни одно изменение этой цепочки не
считается применённым. После первого failed step остальные steps не запускаются.
Все ID/version проверяются до начала цепочки, даже если ранний step мог бы отказать.

Невалидные данные дают `accepted=False` и безопасные `issues` с code/message_key.
Основные codes: `NORMALIZATION_INVALID_VALUE`, `AMBIGUOUS_DATE`, `AMBIGUOUS_NUMBER`,
`NORMALIZATION_TIMEZONE_REQUIRED`, `NORMALIZATION_CURRENCY_REQUIRED`,
`NORMALIZATION_CURRENCY_MISMATCH`, `NORMALIZATION_LOSSY_CONVERSION`,
`NORMALIZATION_PARAMETERS_INVALID`. Custom implementation может вернуть собственный
machine-readable code. Форма `ValidationIssue` не расширена raw сообщениями.

SDK `ValidationError` означает invalid input/policy/adapter, неизвестный normalizer,
дубликат/frozen registry, невалидный plugin output либо resource boundary failure:
`NORMALIZATION_INPUT_INVALID`, `NORMALIZATION_POLICY_INVALID`,
`NORMALIZER_INVALID_ADAPTER`, `NORMALIZER_NOT_FOUND`,
`NORMALIZER_DUPLICATE_REGISTRATION`, `NORMALIZER_REGISTRY_FROZEN`,
`NORMALIZER_OUTPUT_INVALID`, `NORMALIZER_EXECUTION_FAILED`, `SECURITY_LIMIT_EXCEEDED`.
Повреждённые DTO перепроверяются, serialization warnings не выводят raw значения.

Полный result и его serialization чувствительны: там сохранены raw values.
`repr(result)` скрывает payload; в logs используйте counters и только разрешённые
владельцем коды. Произвольный `issue_code` custom normalizer может содержать PII.
Hashes не анонимизируют данные.

## Custom normalizer

Реализуйте синхронный `structuraguard.ports.Normalizer.normalize(value, *, policy,
parameters) -> NormalizerOutput`. Передаются только immutable scalar DTO/config,
без source/DB/LLM handles. Config шага — tuple `NormalizerParameter(name, value)`;
значение параметра также tagged scalar. Реализация проверяет собственные параметры.

Зарегистрируйте instance через `registry.register(NormalizerDescriptor(
normalizer_id="custom.name", version="1.0.0"), instance)`, затем создайте snapshot.
На успех верните `NormalizerOutput(value=..., transformations=...)` со связной
историей `ValueTransformation`. No-op возвращает исходный value без transformations.
На отказ верните исходный value с `issue_code`. Registry проверяет тип, лимиты,
связность истории, отсутствие изменения переданных arguments и копирует output.

Custom Python implementation доверена composition owner: она обязана быть
детерминированной, без I/O, hidden state и мутаций. Registry не является sandbox
для произвольного Python. Snapshot фиксирует bound method и ID/version, но не
замораживает произвольный closure. Async implementations не соответствуют port.
Ожидаемые ошибки implementation оформляются SDK `ValidationError`; стандартные
ошибки на этой границе получают безопасный `NORMALIZER_EXECUTION_FAILED`.

Пример удаляет только явно заданный literal prefix. Никакие строки конфигурации
не интерпретируются как код; ведущие нули оставшейся строки сохраняются.

<!-- example:m12-custom-normalizer:start -->
```python
from structuraguard.contracts.common import RawScalar, StringScalar
from structuraguard.contracts.normalization import (
    NormalizationPolicy, NormalizerDescriptor, NormalizerOutput,
    NormalizerParameter, NormalizerSpec, ValueTransformation,
)
from structuraguard.normalization import NormalizerRegistry


class RemovePrefix:
    def normalize(self, value: RawScalar, *, policy: NormalizationPolicy,
                  parameters: tuple[NormalizerParameter, ...]) -> NormalizerOutput:
        if (len(parameters) != 1 or parameters[0].name != "prefix"
                or not isinstance(parameters[0].value, StringScalar)
                or not parameters[0].value.value or not isinstance(value, StringScalar)):
            return NormalizerOutput(value=value, issue_code="CUSTOM_INPUT_INVALID")
        prefix = parameters[0].value.value
        if not value.value.startswith(prefix):
            return NormalizerOutput(value=value)
        output = StringScalar(value=value.value[len(prefix):])
        return NormalizerOutput(value=output, transformations=(ValueTransformation(
            operation="remove_literal_prefix", input_value=value, output_value=output,
        ),))


registry = NormalizerRegistry()
registry.register(NormalizerDescriptor(normalizer_id="custom.remove_prefix"), RemovePrefix())
result = registry.freeze().normalize(
    StringScalar(value="SG-001"),
    steps=(NormalizerSpec(normalizer_id="custom.remove_prefix", parameters=(
        NormalizerParameter(name="prefix", value=StringScalar(value="SG-")),
    )),),
)
assert result.accepted
assert result.raw_value == StringScalar(value="SG-001")
assert result.normalized_value == StringScalar(value="001")
assert result.steps[0].output.transformations[0].operation == "remove_literal_prefix"
```
<!-- example:m12-custom-normalizer:end -->

## Лимиты и интеграция

По умолчанию: 4096 символов scalar, 128 numeric digits, абсолютный Decimal exponent
до 1024, 32 steps и 256 KiB на result/trace. Policy позволяет уменьшить или повысить
их до hard caps DTO. Registry ограничен 128 registrations; параметры — 32 на step,
изменения — 32 на output. Intake имеет bounded depth/items. Budget failure никогда
не выдаёт успешную частичную цепочку.

На входе данных принимаются точные SDK contracts и native scalars; Python subclasses,
чужие enums и подменённое хранилище DTO отклоняются до serializers и пользовательских
hooks. Проверка identity класса также не вызывает его metaclass `__hash__`/`__eq__`.
Это защита intake, а не sandbox для зарегистрированной custom implementation.

Реализован самостоятельный scalar registry из A в [плане M12](plans/M12_validation_engine.md).
Batch normalization/schema 1.3.0 и автоматический coordinator ещё не реализованы.
Отдельно доступны [JSON Schema validator](json-schema-validation.md),
[DB constraints и business rules](db-business-validation.md),
[provenance validation и сборка отчёта](provenance-validation.md).
`NormalizationResult` не является новым `NormalizedBatch` и не доказывает physical
provenance. Перед загрузкой преобразованных данных нужны новая lineage и повторные
M8–M11 проверки. Архитектурная граница описана в [ADR 0022](adr/0022-conservative-normalization-and-validation.md).

Канонические требования: [ТЗ, §15 «Нормализация»](https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#15-нормализация).
