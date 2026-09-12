# Локальная JSON Schema validation

`JsonSchemaValidator` проверяет схему и JSON instance по ограниченному профилю
Draft 2020-12. Он собирает независимые нарушения с codes и JSON paths, сохраняет
входы и не применяет defaults, coercion или repairs.

<!-- example:m12-schema:start -->
```python
import asyncio
from decimal import Decimal

from structuraguard.contracts import JsonSchemaPolicy, JsonSchemaResource
from structuraguard.validation import JsonSchemaValidator


async def main() -> None:
    amount_uri = "urn:structuraguard:schema:amount"
    validator = JsonSchemaValidator(
        policy=JsonSchemaPolicy(allowed_resource_uris=(amount_uri,)),
        resources=(JsonSchemaResource(
            uri=amount_uri,
            document_json='{"type":"number","minimum":0,"multipleOf":0.01}',
        ),),
    )
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["order_number", "total_amount"],
        "properties": {
            "order_number": {"type": "string", "minLength": 1},
            "total_amount": {"$ref": amount_uri},
        },
        "additionalProperties": False,
    }
    source = {"order_number": "A-17", "total_amount": Decimal("1250.50")}
    assert (await validator.validate(source, schema=schema)).accepted
    result = await validator.validate({"total_amount": Decimal("-1.001")}, schema=schema)
    assert result.schema_valid and not result.accepted
    assert {(item.json_pointer, item.code) for item in result.issues} == {
        ("/order_number", "JSON_SCHEMA_REQUIRED"),
        ("/total_amount", "JSON_SCHEMA_MINIMUM"),
        ("/total_amount", "JSON_SCHEMA_MULTIPLE_OF"),
    }
    assert source["total_amount"] == Decimal("1250.50")
    assert validator.cache_info.entries == 1
    assert validator.cache_info.hits == 1


if __name__ == "__main__":
    asyncio.run(main())
```
<!-- example:m12-schema:end -->

## Порядок и контракт

1. Trusted `JsonSchemaPolicy` и явно переданные resources перепроверяются и
   копируются. JSON resources запрещает duplicate keys и non-finite numbers.
2. Schema bundle проходит bounded intake и проверку поддержанных features/refs.
   Затем `Draft202012Validator` проверяет его по bundled meta-schema. При ошибках
   схемы instance не проверяется; invalid schema в cache не попадает.
3. Проверяются локальные targets, anchors и граф refs. Цикл, который не переходит
   к дочернему instance, отклоняется. Guarded recursive trees разрешены.
4. Проверенный schema snapshot помещается в instance-owned LRU. Внутренняя копия
   не выдаётся caller. Instance копируется с собственным бюджетом; `iter_errors`
   собирает нарушения с ограничением работы, глубины и объёма отчёта.

Публичный async port — `structuraguard.ports.JsonSchemaValidator`. Вход `instance`
принимает только native `dict/list/str/int/float/bool/None` и конечный `Decimal`;
`schema` — JSON object либо boolean schema. Subclasses, произвольные Python
объекты, циклические containers и non-string keys отклоняются. Пустой объект
отличается от explicit null. Tagged SDK DTO, dates и topology проецирует caller;
validator не разбирает строковые значения как JSON.

Decimal не преобразуется во float: comparisons и `multipleOf` точны и не зависят
от process Decimal context. Математически целый Decimal соответствует JSON
`integer`, bool — нет. Для входного Python float используется его десятичное
round-trip представление `str(value)`; это не восстанавливает ранее потерянную
точность. Денежные значения передавайте как Decimal.

## Policy refs и regex

По умолчанию разрешены только fragments текущего документа: `#`, JSON Pointer
в `$defs` и других schema positions, локальные `$anchor`. Внешний локальный resource
нужно передать в `resources` и перечислить в `allowed_resource_uris`; допустимы
только ключи вида `urn:structuraguard:schema:name` с ASCII suffix `[A-Za-z0-9._-]`.
`$id` разрешён только у корня документа и должен совпадать с его локальным URI.
Anonymous root использует внутренний `urn:structuraguard:schema:root`.

HTTP(S), file, data, relative file refs и network-path refs запрещены, в том числе
в неиспользованных `$defs`. URI не разрешает загрузку: `referencing.Registry`
содержит только локальные resources и не имеет retrieval callback. Конструктор
при первом использовании импортирует dependencies и читает их bundled
meta-schema package data; вызовы `validate` не выполняют I/O. Импорт SDK сам
по себе эти dependencies не загружает.

Regex в `pattern` и `patternProperties` ограничены до matching. Поддержаны ASCII
литералы, character classes/ranges, экранированная пунктуация и `^`/`$`.
Допустима не более одной variable repetition (`*`, `+`, `?`, `{m,n}`), только
при `^` в начале; fixed `{n}` ограничен 256. Например, `^[A-Z0-9_-]{1,32}$`.
Groups, alternation, dot, lookaround, backrefs, class escapes (`\d`, `\w`),
nested/несколько variable repeats и неоднозначные set operations отклоняются.
Matching использует Python `re` для этого закрытого подмножества; произвольная
ECMAScript regex-семантика не заявляется. Стоимость поддержанного matching
линейна по длине строки при фиксированном ограниченном pattern.

Nested `$id`, `$dynamicRef`/`$dynamicAnchor`, custom `$vocabulary`, другие drafts
и неизвестные keywords дают `JSON_SCHEMA_FEATURE_UNSUPPORTED`. Это явные границы
профиля, а не молчаливый пропуск проверок. `format` и content keywords — annotations;
Format-Assertion vocabulary не поддержан. UUID/email/date semantic checks относятся
к отдельному уровню; automatic normalization не выполняется.

## Issues, budgets и cache

`JsonSchemaResult.schema_valid` отделяет отказ схемы от нарушения instance;
`accepted` истинно только для valid schema без issues. Порядок issues стабилен.
`JsonSchemaIssue` содержит переиспользуемый `ValidationIssue`, `phase`,
`instance_path`, `schema_path`, `resource_uri`, `code`, RFC 6901 `json_pointer`
и bracket-notation `json_path`. При `phase="schema"` instance path указывает
на дефект в схеме; schema path — на meta-constraint. Для compositions отчёт
включает parent issue и причины из неуспешных branches. Ошибки successful branches
не выдаются. Required/additional property violations имеют путь конкретного ключа;
агрегатные constraints могут указывать на весь object/array.

Основные codes: `JSON_SCHEMA_INVALID_SCHEMA`, `JSON_SCHEMA_REF_FORBIDDEN`,
`JSON_SCHEMA_REF_UNRESOLVED`, `JSON_SCHEMA_REF_CYCLE`, `JSON_SCHEMA_RESOURCE_INVALID`,
`JSON_SCHEMA_FEATURE_UNSUPPORTED`, а для instance — `JSON_SCHEMA_TYPE`,
`JSON_SCHEMA_REQUIRED`, `JSON_SCHEMA_MINIMUM`, `JSON_SCHEMA_MULTIPLE_OF`,
`JSON_SCHEMA_ADDITIONAL_PROPERTIES` и аналогичные uppercase keyword codes.
Library messages и значения в issues не копируются. Имена ключей/paths чувствительны:
payload скрыт из repr; в обычные logs передавайте только codes и counters.

Лимиты по умолчанию: depth 24, nodes 4096, properties на object 256, string chars
4096, numeric digits 128, абсолютный Decimal exponent 512, regex chars 128,
evaluation depth 64, work units 100000 и issues 256. `max_bytes=262144` применяется
отдельно к совокупному schema bundle, instance и консервативной оценке report.
Policy допускает настройку только до конечных hard caps DTO. Work units учитывают
вызовы keywords и верхнюю оценку дорогих операций; это не wall-clock timeout.

Превышение любого бюджета даёт SDK `ValidationError` с
`JSON_SCHEMA_LIMIT_EXCEEDED`, без partial report/pass. Некорректные Python inputs,
policy и resource config дают соответственно `JSON_SCHEMA_INPUT_INVALID`,
`JSON_SCHEMA_POLICY_INVALID` и `JSON_SCHEMA_RESOURCE_INVALID`. Конфигурационные
исключения содержат фиксированный текст без raw context.

LRU по умолчанию ограничен 16 schemas и 4 MiB logical intake size; один слишком
большой для cache элемент проверяется без сохранения. Ноль entries/bytes выключает
cache. `cache_info` показывает entries, bytes, hits, misses, evictions. Policy и
resources принадлежат instance; `schema_fingerprint` включает root и все локальные
resources с type-tagged numeric encoding. Hash не анонимизирует и не удостоверяет
автора, report не является разрешением на загрузку. Один validator используйте
в одном event loop/thread; concurrent OS-thread sharing не гарантируется.

## Граница M12-B и зависимости

Поставлен standalone schema validator. [DB/business rules](db-business-validation.md)
и [provenance validation с report builder](provenance-validation.md) также доступны
как отдельные сервисы. Автоматическая batch/topology projection и общий coordinator
ещё не реализованы. `accepted` этого результата не подтверждает остальные уровни.
См. [план M12](plans/M12_validation_engine.md),
[ADR 0023](adr/0023-local-json-schema-validation.md) и
[отчёт проверки](plans/M12_B_schema_acceptance.md).

Канонические требования: [ТЗ, §16.3 «JSON Schema»](https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#163-json-schema).

Runtime dependencies `jsonschema>=4.26,<5` и `referencing>=0.37,<0.38` уже входили
в workspace lock транзитивно; теперь объявлены прямо. Проверены installed версии
4.26.0/0.37.0, MIT и Python >=3.10, совместимые с SDK Python 3.12+. Lock фиксирует
версии/hashes, dev `types-jsonschema` обеспечивает mypy. Мотив: standard draft
evaluator/meta-schema и immutable in-memory resolution вместо собственной
реализации стандарта. Источники: [validator API](https://python-jsonschema.readthedocs.io/en/stable/validate/),
[local registry API](https://python-jsonschema.readthedocs.io/en/stable/referencing/),
[MIT jsonschema](https://github.com/python-jsonschema/jsonschema/blob/main/COPYING),
[MIT referencing](https://github.com/python-jsonschema/referencing/blob/main/COPYING).
