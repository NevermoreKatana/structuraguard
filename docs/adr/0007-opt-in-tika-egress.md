# ADR 0007: opt-in Tika client и явная egress boundary

Статус: принято для optional M4-F. Дата: 2026-09-09.

## Решение и изменение прежнего плана

По текущему требованию пользователя network/container isolation Tika обеспечивает
вызывающий проект. Поэтому прежнее условие плана F «ждать SDK sandbox M12»
заменяется внешним, управляемым caller сервисом. Это узкое исключение запрета
сетевого доступа parsers: только `parsers/_tika_http.py` отправляет разрешённый
snapshot на явно заданный endpoint. Специализированные adapters A–E, их factories,
registry ranking и orchestrator не изменяются. Adapter не является sandbox runner
и не проверяет фактическую изоляцию deployment.

`TikaParserAdapter` импортируется из `structuraguard.parsers.tika`, выключен по
умолчанию и не регистрируется автоматически. Fallback registry создаёт caller,
отдельно от core registry, после `PARSER_UNSUPPORTED_FORMAT`. Ошибки безопасности,
malformed input, unavailable dependency и format conflict нельзя превращать в
повод отправить файл Tika. Низкий priority не заменяет отдельную fallback session.

Локальный probe читает до 16 байт и не выполняет HTTP. Реализованы явные allowlists
`application/rtf` (`{\rtf`) и `application/postscript` (`%!PS-Adobe-`). Форматы A–E
не подтверждаются. Это не обещание всех форматов Apache Tika; расширение signatures
требует отдельного review. MIME/extension — advisory, не основание egress.

## Конфигурация и зависимости

Extra `tika` теперь содержит HTTPX, закреплённый httpcore и defusedxml, а не `tika-python`, способный
управлять Java runtime. Это намеренное изменение ранее резервировавшегося bundle;
приложения, самостоятельно импортирующие сторонний `tika`, должны отдельно
управлять этой зависимостью. Ничего не скачивает JAR, не запускает Java/container
и не делает healthcheck/discovery при import, construction или probe. `all`
устанавливает dependencies, но не включает adapter.

HTTPX используется для bounded async upload/raw response streaming. Его
[лицензия BSD-3-Clause](https://github.com/encode/httpx/blob/master/LICENSE.md)
проверена; lock фиксирует тестируемый HTTPX 0.28.1 и defusedxml 0.7.1 (PSF).
Unconditional dependencies ядра не расширены.

Security review M4 закрепляет `httpcore==1.0.9` в `tika`/`all`: request-local
trace callback редактирует diagnostic payload до upstream DEBUG serialization.
Это уже существовавшая transitive dependency, не новый transport backend;
[лицензия BSD-3-Clause](https://github.com/encode/httpcore/blob/1.0.9/LICENSE.md).
Upgrade pin требует повторения real HTTP security regression: порядок callback
относительно logging не является универсальной гарантией будущих версий.

`TikaConfig` требует при `enabled=True` endpoint, media allowlist и
`expected_server_version`. Разрешён полный HTTPS URL с точным path `/tika`;
HTTP допускается только для numeric loopback, не `localhost`/remote hostname.
Userinfo, query, fragment, escapes, control characters и произвольные paths
запрещены. Endpoint не берётся из файла/metadata/environment. Версия — декларация
caller и часть fingerprint конфигурации, **не проверенная версия удалённого JVM**.
Caller обязан pin-ить реальный image/artifact и своевременно обновлять его.

## Secrets и egress

До любого upload нужен `TikaEgressApproval`, привязанный к точному SHA-256 source:
`classification=PUBLIC`, `secrets_checked=True`, `contains_secrets=False`.
Defaults запрещают отправку. Отсутствующее/чужое/отрицательное разрешение,
включая INTERNAL/CONFIDENTIAL/RESTRICTED, отклоняется до чтения source.
Это результат проверки data owner/DLP caller, а не способ автоматически
объявить произвольный файл безопасным. SDK сам не меняет classification.

Затем весь bounded snapshot проверяется **до первого сетевого байта**:
измерение размера, пересчёт SHA-256 и существующий credential-canary detector
на overlapping decoded chunks. Upload читает этот же временный snapshot,
не потенциально изменившийся SourceReader. Известные password/token/DSN/private-key
маркеры даже в конце файла приводят к отказу без partial egress.

Сигнатура разрешённого формата повторно проверяется на этом же temporary snapshot
после SHA проверки и до создания HTTP client. Несоответствие probe и upload
возвращает `SECURITY_INPUT_REJECTED` (`tika_snapshot_format`), без запроса в сеть.

Canary detector — дополнительный контроль, не полноценная DLP для binary,
RTF escapes, обфускации и неизвестных secrets. Нельзя ставить approval flags без
реальной проверки всего документа. Неверное разрешение caller может привести
к утечке; adapter не может математически распознать произвольный secret.

Ни filenames/SourceArtifact metadata, ни credentials, cookies, Authorization,
proxy auth, netrc или окружение не переносятся в запрос. Client создаётся на один
transfer: `trust_env=False`, `follow_redirects=False`, retries=0; auth/hooks/custom
headers не предоставляются публичным API. Endpoint скрыт из repr конфигурации,
в manifest попадает только hash опций. TLS verification не отключается.

HTTPcore `.complete`/`.failed` trace metadata удаляется до DEBUG logging;
server-controlled reason phrase удаляется до HTTPX INFO logging. Headers/body
при этом не меняются и проходят прежнюю policy. Logger levels/handlers/filters
caller не изменяются глобально; method/status/endpoint diagnostics сохраняются.
Не передавайте secrets в hostname endpoint. Нельзя отключать этот control
monkeypatch-ем transport в production; fake transport — только test boundary.

## HTTP, limits и errors

Протокол: один `PUT /tika`, raw source body, фиксированный `Accept: text/xml`,
`Accept-Encoding: identity`, `X-Tika-Skip-Embedded: true`, OCR strategy `no_ocr`.
[Tika REST API](https://cwiki.apache.org/confluence/spaces/TIKA/pages/148639291/TikaServer)
поддерживает PUT исходного файла, HTML output и skip-embedded header.
Adapter выбирает XML-сериализацию XHTML: у
[TikaResource 3.2.3](https://github.com/apache/tika/blob/3.2.3/tika-server/tika-server-core/src/main/java/org/apache/tika/server/core/resource/TikaResource.java)
`text/html` использует HTML serializer, тогда как `text/xml` — XML serializer.
Это исключает зависимость от HTML void-tag serialization; XML 1.1 constructs,
не поддержанные Expat, отклоняются как malformed, а не исправляются.
PDF OCR header описан в
[parse-time configuration](https://cwiki.apache.org/confluence/spaces/TIKA/pages/240883999/Configuring+Parsers+At+Parse+Time+in+tika-server).
Headers являются запросом клиентской policy, не доказательством исполнения
политики сервером. Caller должен отключить OCR, external programs/resources,
attachments, macros/actions в своей server configuration и проверять её.

`TikaParserLimits`: request 8 MiB (hard cap 128 MiB), response 2 MiB (16 MiB),
headers 8192 bytes (65536), chunk 4096 bytes, общий deadline 30 s (300 s).
Request копируется в автоматически удаляемый TemporaryFile; весь файл не
удерживается в RAM. Response читается bounded raw chunks; declared Content-Length
проверяется до body, фактический размер — до накопления очередного chunk.
Предел headers применяется после bounded HTTP backend parsing; это не
побайтовый лимит TCP буфера. Gzip/другая content compression запрещена.

200 требует единственный `text/xml` или `application/xhtml+xml`, UTF-8 charset
и well-formed XHTML. JSON/plain text/tag soup не используются как silent fallback.
204 означает успешный пустой результат. Redirect не выполняется; status/body/URL
не включаются в errors. HTTP failure не запускает retry или другой endpoint.

| Ситуация | Typed outcome |
|---|---|
| Выключен / формат вне allowlist | `PARSER_UNSUPPORTED_FEATURE` |
| Отсутствует extra | `PARSER_DEPENDENCY_UNAVAILABLE` |
| Connection/DNS/HTTP service unavailable | `PARSER_TIKA_UNAVAILABLE` |
| Deadline / transport timeout | `PROCESSING_TIMEOUT` |
| HTTP 422, malformed framing/UTF-8/XHTML | `PARSER_MALFORMED_INPUT` |
| Лимит request/response/headers/XML | `SECURITY_LIMIT_EXCEEDED` |
| Нет approval, secret, redirect, compression, forbidden MIME/DTD | `SECURITY_INPUT_REJECTED` |
| Подмена snapshot | `SOURCE_FINGERPRINT_MISMATCH` |

Network response полностью получен и XHTML проверен без DOM до первой выдачи.
Transport failure/malformed XHTML не оставляют partial batches. Parser limits
при дальнейшей сборке могут прервать provisional stream: terminal manifest
по-прежнему обязателен. Cancellation закрывает HTTP response/client и snapshot;
deadline не отменяет consumer между `anext` calls. Backpressure входит в deadline.
Остановка клиента не гарантирует остановку удалённого server job.

## Физическая модель и provenance

Ответ проверяется defusedxml с запретом DTD/entities/external resolver. XML adapter
сохраняет XHTML tree: headings, paragraphs, lists, tables/cells и metadata остаются
элементами/атрибутами/текстом с исходной иерархией, не semantic fields. Никаких
ParsePlan, LLM, DB или destructive flatten. JS/CSS/URLs остаются inert values;
consumer не должен вставлять их в `innerHTML` без escaping.

`ExtensionLocation(namespace="tika:xhtml-v1")` связывает исходный SourceArtifactRef,
SHA-256 полного response, XPath, namespace prefix и configured server version.
`fidelity=response_relative`: XPath указывает в XHTML ответа, **не в исходный
RTF/PostScript**. Source page/bbox/row offsets не выдумываются. XML infoset
normalization и ограничения markup adapter применимы также здесь.

`limits.xml` — существующий `XmlParserLimits`; также действуют records/depth/
physical objects/max batches из ParseContext. Batching использует complete
direct-child units XHTML root и continuation envelope. Большой неделимый body
отклоняется по subtree caps, не flatten-ится. Fingerprint опций включает endpoint
hash context, declared version, allowlist, transport/XML limits и batch options.

## Ответственность caller и проверки

Caller обеспечивает server egress denylist/allowlist, запрет network fetch,
non-root/read-only container, CPU/memory/pid limits, отсутствие secrets и Docker
socket, temp cleanup/retention, server timeout/cancellation и безопасный ingress.
Это включает защиту от SSRF/DNS rebinding на инфраструктурном уровне; в client
нет network namespace или attestation удалённой среды. M12 SDK runner не реализован.

Default tests используют fake HTTP transport; integration test — ephemeral
loopback fake server. Реального Tika/JVM, внешнего endpoint, OCR, JAR download
или Docker в tests нет. Проверяются registry contract, negative core probes,
Unicode/chunk/batch invariance, request fingerprint/secrets, malformed/oversized
HTTP/XML, XXE, redirects, proxy/cookie policy, unavailable/timeout/cancellation.
Фактическая версия, форматная точность и изоляция production Tika этими tests
не сертифицируются — deployment acceptance остаётся у вызывающего проекта.
