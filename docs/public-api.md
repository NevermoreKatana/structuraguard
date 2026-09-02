# Публичный API StructuraGuard

Статус: подтверждённое поведение package `structuraguard` версии `0.1.0` в
milestone M1. Операции pipeline в эту поставку не входят.

Целевой API следующих milestone описан в каноническом разделе
[23. Публичный API SDK][spec-public-api]. Он не считается доступным, пока
соответствующий vertical slice не имеет реализации и тестового evidence.

## Копируемый пример M1 {#m1-copyable-example}

Основной facade асинхронный. В M1 его можно создать с явной конфигурацией, но
предметные операции всегда завершаются типизированной ошибкой. Пример не читает
источник и не возвращает фиктивный результат.

<!-- example:m01-async:start -->
```python
import asyncio

from structuraguard import (
    AsyncStructuraGuard,
    OperationNotImplementedError,
    SDKConfig,
)


async def main() -> None:
    sdk = AsyncStructuraGuard(config=SDKConfig())
    try:
        await sdk.inspect_source()
    except OperationNotImplementedError as error:
        print(error.error_code)


asyncio.run(main())
```
<!-- example:m01-async:end -->

Ожидаемый вывод:

```text
SDK_OPERATION_NOT_IMPLEMENTED
```

`asyncio.run()` вызывается приложением явно. Сам `import structuraguard` и
создание facade event loop не создают.

## Экспорты M1

Top-level module `structuraguard` экспортирует только:

- `SDKConfig`;
- основной `AsyncStructuraGuard` и отдельный `StructuraGuard`;
- `StructuraGuardError`, `SourceError`, `ParserError`,
  `DatabaseInspectionError`, `MappingError`, `ValidationError`,
  `SecurityPolicyError`, `LoadError` и `OperationNotImplementedError`.

Экспорты разрешаются лениво. Обычный `import structuraguard` не загружает
Pydantic; он загружается при первом явном обращении к `SDKConfig` или facade.

## SDKConfig

`SDKConfig` — пустая immutable Pydantic model для composition root M1:

- `SDKConfig.model_fields == {}`;
- значения принимаются только явно;
- неизвестное поле отклоняется `pydantic.ValidationError`;
- изменение созданного объекта отклоняется;
- environment не является источником конфигурации;
- `BaseSettings` и speculative operational fields отсутствуют.

Если `config` не передан facade, для каждого экземпляра создаётся отдельный
`SDKConfig`. Переданный объект возвращается через свойство `config` без замены.

## Facades и недоступные операции

`AsyncStructuraGuard` является canonical async-first точкой входа.
`StructuraGuard` — отдельная composition-based sync-оболочка, а не subclass
async facade.

Обе facade объявляют имена будущих операций:

- `inspect_source`;
- `inspect_database`;
- `create_plan`;
- `validate_plan`;
- `execute`;
- `analyze`;
- `ingest`;
- `propose_schema`.

Их предметные параметры и результаты не являются public contract M1.
Позиционные и именованные аргументы в M1 не интерпретируются. Async-вызов всегда
возбуждает `OperationNotImplementedError` с
`error_code="SDK_OPERATION_NOT_IMPLEMENTED"` и canonical operation name в
`details["operation"]`.

Sync facade передаёт аргументы async facade без изменения. При явном вызове вне
активного event loop он использует краткоживущий loop через `asyncio.run()` и
получает тот же `SDK_OPERATION_NOT_IMPLEMENTED`. Внутри активного event loop
вызов отклоняется до делегирования:

```text
SYNC_API_IN_ASYNC_CONTEXT
```

В async-приложении необходимо использовать `AsyncStructuraGuard`.

## Исключения

Все публичные категории наследуют `StructuraGuardError`. Наличие категорийных
classes не означает, что parser, DB inspector или loader уже реализованы.

Публичные поля базовой ошибки:

| Поле | Семантика M1 |
| --- | --- |
| `error_code` | Непустой код формата `[A-Z][A-Z0-9_]*` |
| `message` | Ограниченное и санитизированное описание |
| `details` | Отсоединённая от input, рекурсивно immutable mapping |
| `run_id` | Необязательный санитизированный идентификатор |
| `retryable` | Явный признак допустимости повтора |
| `cause` | Только имя класса исходного исключения либо `None` |

Alias `code` отсутствует. Невалидный `error_code` отклоняется встроенным
`ValueError`. Facades M1 самостоятельно возбуждают только два стабильных кода:

- `SDK_OPERATION_NOT_IMPLEMENTED`;
- `SYNC_API_IN_ASYNC_CONTEXT`.

### Security semantics ошибок

Перед сохранением ошибки реализация:

- удаляет распространённые password, token, API key, authorization, DSN,
  private-key и cookie credentials из известных key/header/assignment forms;
- скрывает URI userinfo, Basic/Bearer credentials и PEM private keys;
- не сохраняет текст исходного `cause`, санитизирует имя его класса и скрывает
  неявный exception context из стандартного traceback;
- заменяет циклы, превышение depth/item budget и non-finite numbers безопасными
  маркерами;
- сохраняет не более первых 4096 исходных символов и при усечении добавляет
  marker `[TRUNCATED]`; nesting ограничен 16 уровнями, общий обход `details` —
  256 узлами, включая корневой container;
- преобразует вложенные mappings и sequences в неизменяемые значения.

Redaction является defense-in-depth, а не универсальным detector secrets.
Произвольный credential под нейтральным именем может быть не распознан. Caller
обязан передавать в `message`, `details` и `run_id` только данные, безопасные для
отображения и логирования. Явное `raise error from cause` снова включает `cause`
в traceback и запрещено, если исходное исключение может содержать secret.
Неизменяемы `details`, но не весь объект исключения.

## Side effects

Проверенный import contract M1 запрещает package code при
`import structuraguard`:

- читать или менять environment;
- читать/создавать файлы, подключаться к сети или запускать process;
- создавать thread или event loop;
- менять event-loop policy, signal handlers и logging state;
- подключаться к БД или создавать tables;
- создавать global mutable registry.

После разрешения lazy exports тела constructors `SDKConfig` и обеих facade не
выполняют перечисленных действий через код StructuraGuard. Первый явный доступ к
`SDKConfig` или facade инициализирует стороннюю dependency Pydantic; эта граница
не входит в гарантию обычного `import structuraguard`. Исключение для sync facade
относится только к явному вызову операции вне активного loop: тогда приложение
создаёт краткоживущий loop, после чего получает typed failure.

## Зависимости и ограничения M1

- Требуется Python 3.12 или новее.
- Единственная unconditional runtime dependency — Pydantic v2.
- Web frameworks отсутствуют в core dependencies.
- Extras `postgres`, `pdf`, `excel`, `office`, `litellm`, `tika` и `all`
  резервируют dependency bundles. Установка extra не добавляет готовый adapter.
- Parsers, DB inspection/load, MappingPlan, validation pipeline и LLM operations
  не реализованы.
- M1 не выполняет предметный I/O и не имеет успешного ingest-сценария.

Требования scaffold определены разделами [M1][spec-m1],
[NFR-001][spec-nfr-001], [NFR-003][spec-nfr-003] и
[NFR-012][spec-nfr-012] канонического ТЗ. Локальная трассировка реализации и
evidence находится в [плане M1](plans/M01_sdk_scaffold.md).

[spec-m1]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m1-каркас-python-пакета
[spec-nfr-001]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#nfr-001-встраиваемость
[spec-nfr-003]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#nfr-003-async-first
[spec-nfr-012]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#nfr-012-ошибки
[spec-public-api]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#23-публичный-api-sdk
