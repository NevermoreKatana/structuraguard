# Централизованные лимиты M14

`SecurityPolicy` задаёт максимумы, а `SecuritySession` связывает один источник,
парсинг и последующие обращения к LLM/БД. Существующие guards парсеров, router и
SQL adapters продолжают выполнять проверки на своих границах. Эффективный предел
равен минимуму общего и локального ограничения.

## Минимальный пример

<!-- example:m14-resources:start -->
```python
import asyncio
from uuid import uuid4

from structuraguard.contracts.source import SourceArtifact
from structuraguard.parsers.builtin import PlainTextParser
from structuraguard.parsers.runners import InProcessParserRunner
from structuraguard.ports.source import ParseContext
from structuraguard.security import SecurityLimits, SecurityPolicy, SecuritySession


async def main() -> int:
    run = SecuritySession(
        SecurityPolicy(
            allowed_formats=("txt",),
            parser_trust="trusted",
            limits=SecurityLimits(
                max_file_bytes=1024,
                max_stream_bytes=1024,
                max_records=10,
                max_text_chars=1024,
                max_parser_time_ms=1000,
            ),
        ),
        run_id=uuid4(),
    )
    transport = asyncio.StreamReader()
    transport.feed_data(b"first\nsecond\n")
    transport.feed_eof()
    snapshot = await run.snapshot(transport, kind="stream")
    source = SourceArtifact(
        artifact_id="source-1",
        display_name="sample.txt",
        media_type="text/plain",
        size_bytes=snapshot.size_bytes,
        source_fingerprint=snapshot.source_fingerprint,
    )
    context = ParseContext(
        reader=snapshot,
        source_fingerprint=snapshot.source_fingerprint,
        max_bytes=1024,
        max_records=10,
        max_nesting_depth=10,
    )
    count = 0
    runner = InProcessParserRunner(PlainTextParser(), session=run)
    async with runner.parse(source, context) as batches:
        async for batch in batches:
            count += len(batch.lines)
    return count


assert asyncio.run(main()) == 2
```
<!-- example:m14-resources:end -->

Пример запускается после установки `structuraguard` и читает только созданный
в памяти поток. `run.parse(...)` предоставляет тот же in-process lifecycle;
`InProcessParserRunner` удобен для кода, принимающего общий `ParserRunner` port.

`SourceStream.read(size)` возвращает не более `size` bytes; пустой результат
означает EOF. Transport принадлежит приложению: оно открывает и закрывает файл
или поток, проверяет roots, symlinks и сетевой endpoint. SDK не принимает URL
вместо `kind`, не открывает произвольные paths и не считает `display_name` правом
на чтение. `expected_size` позволяет отказать до первого чтения; несовпадение
объявленного размера с EOF также отклоняется.

Snapshot накапливается только после проверки очередного блока. Для различения
точного предела и превышения используется максимум один дополнительный байт.
Одна session допускает не более одного snapshot и одного parse.
Snapshot хранится в памяти; завершающее преобразование в immutable bytes временно
создаёт вторую копию. `max_file_bytes` и `max_stream_bytes` ограничивают payload,
а не RSS процесса. При парсинге неизвестного происхождения snapshot применяется
минимум обоих byte limits.

## Единицы и точки проверки

| Настройка | Семантика и enforcement |
| --- | --- |
| `max_file_bytes`, `max_stream_bytes` | Размер одного source snapshot; declared size до I/O, фактический размер перед добавлением блока. |
| `read_chunk_bytes` | Максимум одного запроса к transport/reader. Probe отдельно ограничен 64 KiB и четырьмя byte на разрешённый text character; это bounded scratch, не готовый extraction. |
| `max_records` | Существующая physical record semantics парсера: строки TXT, records JSONL, строки таблиц и document units. Проверка перед добавлением следующей записи. Это не количество будущих бизнес-сущностей. |
| `max_columns` | Ширина CSV/TSV/XLSX/DOCX/PDF таблицы и каждого JSON object/YAML mapping; для XML/HTML ограничивает attributes. Проверка до накопления следующей колонки/ключа; PDF проверяет объявленную ширину до `table.extract()`. |
| `max_nesting_depth` | Глубина дерева по существующим parser правилам; у JSON root имеет depth 1. XML и document containers сохраняют дополнительные локальные guards. |
| `max_text_chars` | Unicode code points. TXT/CSV/JSON учитывают весь декодированный текст, включая синтаксис и переводы строк; markup/documents — извлечённый текст по существующему `Budget`. Счётчик не сбрасывается между batches. |
| `max_chunks` | Число физических `ExtractedBatch` одного parse; локальный batch builder проверяет следующий batch. |
| `max_parser_time_ms` | Общий deadline probe + parse от первого parser await. Проверяется до/после await и при чтении. |
| `max_llm_calls`, `max_llm_tokens` | Суммарный резерв всех подключённых router/provider instances. Перед egress резервируются одна попытка и полный `max_input_tokens + max_output_tokens` capabilities. Fallback расходует новый резерв; ошибок и отмен недостаточно для refund. |
| `max_llm_time_ms` | Общий deadline от первого LLM обращения. Каждый await ограничен минимумом общего и локального оставшегося времени. |
| `max_db_batch_rows`, `max_db_batch_bytes` | Write statement использует существующий batch builder: строки и сумма размеров canonical records. N+1 строка начинает следующий batch; одиночная слишком большая строка отклоняется до построения SQL. Key lookup batches также сужаются по числу строк. |
| `max_db_queries` | Общий счётчик SDK SQLAlchemy statements подключённых target adapters, включая reflection, prechecks и DML. Резерв до driver invocation; N+1 statement не отправляется. |
| `max_processing_time_ms` | Общий deadline от создания session; смена этапа его не сбрасывает. |

Все значения — положительные strict integers с конечными абсолютными caps.
Ноль не отключает проверку. Неверная форма конфигурации даёт Pydantic
`ValidationError`; forged policy на входе session даёт `SecurityPolicyError`
с `SECURITY_POLICY_INVALID`. `policy.narrow(new_limits)` разрешает только
уменьшение всех maxima, включая размер чтения.

## Подключение LLM и БД

Создайте одну session на run и передайте `resources=run` всем участвующим
`PolicyAwareLLMRouter`, `LLMRunProvider`, `SQLiteDatabaseAdapter`,
`PostgreSQLDatabaseAdapter`, `DatabaseConstraintReader`, `PostgreSQLDryRunPlanner`
и `PostgreSQLLoader`. Их resource ports не получают SQL, binds, DSN или payload.
Исключите двойной accounting одного egress: при вложенных LLM wrappers подключайте
общую session к одному владельцу фактических попыток.

`run.llm_policy(local_policy)` сужает `LLMBudget` **до** выпуска routing approval.
Policy fingerprint и privacy destinations уже утверждённого запроса нельзя
менять. `run.inspection_limits(local_limits)` применяется **до** создания DB target
и его fingerprint: это связывает общие columns/text/time caps с catalog requests.
`run.load_policy(local_policy)` используется до staging/load planning; loader
повторно сужает batch/read caps при переданном `resources`. Constraint reader и
dry-run самостоятельно сужают локальный key query budget.

Дополнительная обёртка `run.call(...)` для этих adapters не требуется.
Она предназначена для trusted read-only/отменяемых операций. Не оборачивайте ею
`loader.execute`: loader сам ограничивает подготовку и transaction оставшимся
временем, проверяет deadline перед DML/COMMIT и сохраняет правила
[подтверждённого и неопределённого COMMIT](loader.md).

При отсутствии `resources` прежний API и локальные ограничения остаются доступны.
Такой вызов не участвует в общем run budget. Передача session — обязанность trusted
composition owner; Python process не является границей изоляции своего host.

Миграция: новые поля parser limits и `ParseContext` участвуют в options/extraction
fingerprints. Ранее сохранённые ParsePlan нужно построить заново; старые approvals
не переносятся на изменённую effective policy. Новые локальные defaults также
ограничивают cumulative decoded text 100 млн characters и JSON object 100 тыс.
колонок. Для больших данных задавайте явную поддержанную policy; ошибки лимитов
не должны приводить к загрузке частичного результата.

## Отказ, отмена и аудит

По умолчанию `allowed_formats=()` и `parser_trust="sandbox_required"` запрещают
запуск. Реализованы только явно разрешённые builtin adapters. Неизвестные plugins,
подклассы и неподдержанный формат отклоняются; наличие parser object не выдаёт
ему authority. `parser_trust="trusted"` — явное разрешение встроенной реализации,
а не доверие входным данным. XXE, макросы, active content и прочие ограничения
по-прежнему блокируют существующие parser guards. Native worker не является
полноценным sandbox; отсутствующий runner не заменяется in-process fallback.

Превышение ресурса даёт `SecurityPolicyError` с `SECURITY_LIMIT_EXCEEDED`,
истечение времени — `PROCESSING_TIMEOUT`; другие закрытые коды:
`SECURITY_INPUT_REJECTED`, `SECURITY_SANDBOX_REQUIRED`, `SECURITY_POLICY_INVALID`,
`SECURITY_RUN_CLOSED`, `SECURITY_OPERATION_FAILED`. Численный предел включителен;
для deadline операция разрешена только пока остаётся положительное время.

Ошибки source transport и getter `source_fingerprint` проходят закрытый error
barrier: чужой текст не попадает в публичное исключение или стандартный traceback.
Первый уже принятый resource code сохраняется; прочие transport failures дают
`SECURITY_OPERATION_FAILED`. Это не защита от logging locals самим host.

`asyncio.CancelledError` распространяется после adapter cleanup, с пустым текстом.
Парсер использует async context manager: ранний выход закрывает stream. Таймер
окружает parser awaits и не отменяет consumer между batches. Время consumer
учитывается при следующем обращении; частичные batches нельзя считать завершённым
extraction без EOF manifest. Cleanup сохраняет собственный конечный бюджет и
может закончиться позже общего deadline. Синхронный CPU/native code требует
кооперативных checkpoints или внешнего sandbox для жёсткого прерывания.

Первый отказ/ошибка/отмена закрывает session. `run.events` содержит максимум один
`ResourceAuditEvent`: UUID, policy fingerprint, UTC время, закрытый outcome/code,
resource и безопасные численные limit/observed, если известны. Никаких исходных
значений, source hashes, paths, SQL, DSN, prompts, exception messages и произвольных
caller IDs в event нет. Последующие отказы не вытесняют исходную причину.
Session не создаёт success event и не выполняет скрытых logging или audit I/O.
Host передаёт evidence в свой audit sink. [HMAC chain](security-controls.md) и
[redaction/classification](privacy-redaction.md) реализованы отдельными срезами;
policy прав источников остаётся частью [плана M14](plans/M14_security_layer.md).

DB query accounting не включает driver handshake, rollback/close и операции
самостоятельного staging/audit store. Эти адаптеры сохраняют собственные budgets;
исчерпание target query budget не должно мешать откату и фиксации outcome.
Cancellation после подтверждённого COMMIT отражается в load warnings; timeout
после известного COMMIT не переписывает успешный результат.

## Проверки

Security regressions проверяют точную границу и N+1 для bytes, records, columns,
nesting, text, chunks, LLM reservations и DB batches/queries; отдельно — deadline,
safe events, отказ до I/O и отмену reader/provider. PostgreSQL 16/18 проверяет
реальные DML/rollback, staging outcome, общий dry-run budget и поздний deadline
после подтверждённого COMMIT. Архитектурное решение: [ADR 0030](adr/0030-centralized-resource-limits.md).

Канонические требования: [§20.9 ТЗ][spec-limits] и [M14][spec-m14]. Единицы и
названия параметров работающего API приведены выше; пример настроек из ТЗ
описывает целевой дизайн и не заменяет текущую сигнатуру.

[spec-limits]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#209-ограничение-ресурсов
[spec-m14]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m14-security
