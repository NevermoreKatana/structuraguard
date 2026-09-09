"""Явно разрешённый Tika fallback вне default factories и core ranking."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import ipaddress
import math
import re
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from typing import cast
from urllib.parse import urlsplit
from xml.etree.ElementTree import ParseError, XMLParser

from structuraguard.contracts._base import CanonicalInput, canonical_sha256_value
from structuraguard.contracts.common import DataClassification
from structuraguard.contracts.source import (
    ExtensionLocation,
    ExtensionMetadataEntry,
    ExtractedBatch,
    ExtractedTreeNode,
    ProbeResult,
    ProbeSignal,
    ProbeSignalKind,
    ProbeSignalOutcome,
    SourceArtifact,
    XPathLocation,
)
from structuraguard.exceptions import ParserError
from structuraguard.ports.source import (
    ParseContext,
    ProbeContext,
    _validate_fingerprint,
)

from ._tika_http import exchange, malformed_response
from .builtin._common import _read_checked, advisory_signals
from .builtin._markup import (
    Budget,
    MarkupDocument,
    MarkupUnit,
    markup_batches,
    missing_extra,
    rejected,
)
from .builtin.xml import XmlParser, XmlParserLimits

_FALLBACK_SIGNATURES = (
    ("application/rtf", b"{\\rtf", ".rtf"),
    ("application/postscript", b"%!PS-Adobe-", ".ps"),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class TikaConfig:
    """Явная конфигурация endpoint; fallback выключен по умолчанию.

    ``enabled=True`` требует endpoint с путём ``/tika``, непустой allowlist
    RTF/PostScript и ``expected_server_version``. HTTP разрешён только для numeric
    loopback, иначе требуется HTTPS; credentials/query/fragment запрещены.
    Версия сервера заявляется caller и записывается в provenance, но по сети
    не проверяется. Создание config не выполняет HTTP и не разрешает egress.

    Raises:
        ValueError: Конфигурация неполна или нарушает ограничения endpoint/формата.
    """

    enabled: bool = False
    endpoint: str | None = field(default=None, repr=False)
    allowed_media_types: frozenset[str] = frozenset()
    expected_server_version: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.enabled) is not bool
            or type(self.allowed_media_types) is not frozenset
        ):
            raise ValueError("Некорректная Tika configuration")
        if not self.allowed_media_types <= {entry[0] for entry in _FALLBACK_SIGNATURES}:
            raise ValueError("Разрешены только поддерживаемые non-core Tika formats")
        if self.expected_server_version is not None and (
            type(self.expected_server_version) is not str
            or re.fullmatch(
                r"[0-9]{1,3}(?:\.[0-9]{1,3}){1,3}", self.expected_server_version
            )
            is None
        ):
            raise ValueError("Требуется явная версия Tika server")
        if self.endpoint is not None:
            self._validate_endpoint()
        if self.enabled and (
            self.endpoint is None
            or self.expected_server_version is None
            or not self.allowed_media_types
        ):
            raise ValueError(
                "Включённый Tika требует endpoint, version и media allowlist"
            )

    def _validate_endpoint(self) -> None:
        endpoint = self.endpoint
        if (
            type(endpoint) is not str
            or len(endpoint) > 1024
            or any(c in endpoint for c in "@?#%\\")
            or not endpoint.isascii()
            or any(ord(c) <= 32 or ord(c) == 127 for c in endpoint)
        ):
            raise ValueError("Некорректный Tika endpoint; credentials запрещены")
        try:
            url = urlsplit(endpoint)
            if (
                url.scheme not in {"https", "http"}
                or not url.hostname
                or url.path != "/tika"
                or url.port == 0
            ):
                raise ValueError
            if re.fullmatch(r"[a-zA-Z0-9.\[\]:-]+", url.netloc) is None:
                raise ValueError
            if (
                url.scheme == "http"
                and not ipaddress.ip_address(url.hostname).is_loopback
            ):
                raise ValueError
        except ValueError:
            raise ValueError(
                "Требуется HTTPS /tika либо HTTP numeric loopback /tika"
            ) from None


@dataclass(frozen=True, slots=True, kw_only=True)
class TikaEgressApproval:
    """Результат внешней DLP-проверки конкретного snapshot, не scanner.

    Caller выдаёт approval для ``source_fingerprint`` после проверки исходных
    bytes. Egress допускается только для PUBLIC, ``secrets_checked=True`` и
    ``contains_secrets=False``; defaults запрещают отправку. Само создание DTO
    не проверяет содержимое. Не формируйте положительное approval из input файла.

    Raises:
        ValueError: Fingerprint либо типы полей не соответствуют контракту.
    """

    source_fingerprint: str
    classification: DataClassification = DataClassification.RESTRICTED
    secrets_checked: bool = False
    contains_secrets: bool = True

    def __post_init__(self) -> None:
        _validate_fingerprint(self.source_fingerprint)
        if (
            type(self.classification) is not DataClassification
            or type(self.secrets_checked) is not bool
            or type(self.contains_secrets) is not bool
        ):
            raise ValueError("Некорректный результат egress review")


@dataclass(frozen=True, slots=True, kw_only=True)
class TikaParserLimits:
    """Конечные transport budgets и независимые XML limits для XHTML ответа.

    ``*_bytes`` ограничивают request, response, headers и размер чтения.
    ``timeout_seconds`` — локальный deadline transfer/извлечения; время между
    запросами следующего batch тоже учитывается. Остановка удалённого Tika job
    этим timeout не гарантируется. ``xml`` ограничивает response tree.

    Raises:
        ValueError: Предел вне hard cap, не конечен либо неверного типа.
    """

    max_request_bytes: int = 8 * 1024 * 1024
    max_response_bytes: int = 2 * 1024 * 1024
    max_header_bytes: int = 8192
    read_chunk_bytes: int = 4096
    timeout_seconds: float = 30.0
    xml: XmlParserLimits = field(default_factory=XmlParserLimits)

    def __post_init__(self) -> None:
        for name, cap in (
            ("max_request_bytes", 128 * 1024 * 1024),
            ("max_response_bytes", 16 * 1024 * 1024),
            ("max_header_bytes", 65536),
            ("read_chunk_bytes", 4096),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= cap:
                raise ValueError(f"{name} вне диапазона 1..{cap}")
        if (
            type(self.timeout_seconds) not in {int, float}
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 300
        ):
            raise ValueError("timeout_seconds вне диапазона (0, 300]")
        if type(self.xml) is not XmlParserLimits:
            raise ValueError("xml должен быть XmlParserLimits")


class _ResponseReader:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.source_fingerprint = "sha256:" + hashlib.sha256(content).hexdigest()

    async def read(self, *, offset: int, size: int) -> bytes:
        return self.content[offset : offset + size]


class _XhtmlGuard:
    """Проверить весь XML response до первой выдачи, не создавая второй DOM."""

    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        self.depth = self.text_size = 0
        self.root_seen = False

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        if not self.root_seen:
            if tag != "{http://www.w3.org/1999/xhtml}html":
                raise malformed_response()
            self.root_seen = True
        self.depth += 1
        self.text_size = 0
        self.budget.node(depth=self.depth, name=tag)
        for key, value in attrs.items():
            self.budget.node(depth=self.depth, name=key)
            self.budget.text(len(value))

    def end(self, tag: str) -> None:
        self.depth -= 1
        self.text_size = 0

    def data(self, text: str) -> None:
        self.budget.text(len(text), existing=self.text_size)
        self.text_size += len(text)

    def close(self) -> None:
        if not self.root_seen:
            raise malformed_response()


async def _validate_xhtml(content: bytes, budget: Budget) -> None:
    from defusedxml.common import DefusedXmlException
    from defusedxml.ElementTree import DefusedXMLParser

    try:
        content.decode("utf-8-sig", errors="strict")
        declaration = re.match(
            rb"\s*<\?xml\b([^?]*)\?>", content.removeprefix(b"\xef\xbb\xbf")
        )
        if declaration:
            encoding = re.search(rb"encoding\s*=\s*['\"]([^'\"]+)", declaration[1])
            if encoding and encoding[1].lower() not in {b"utf-8", b"utf8", b"us-ascii"}:
                raise malformed_response()
        parser: XMLParser = DefusedXMLParser(
            target=_XhtmlGuard(budget),
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
        for offset in range(0, len(content), budget.limits.read_chunk_bytes):
            await asyncio.sleep(0)
            parser.feed(content[offset : offset + budget.limits.read_chunk_bytes])
        parser.close()
    except DefusedXmlException:
        raise rejected("tika_response_dtd") from None
    except (UnicodeError, ParseError, LookupError):
        raise malformed_response() from None


class TikaParserAdapter:
    """Optional fallback; caller обеспечивает server isolation и проверку secrets.

    Args:
        config: Явный endpoint/allowlist; ``None`` оставляет adapter выключенным.
        limits: Transport/XML budgets; ``None`` выбирает TikaParserLimits().
        approval: Внешний source-bound результат DLP, без него egress запрещён.

    Требуется extra ``tika``. Поддержаны только allowlisted RTF/PostScript;
    ответ — XHTML tree с response-relative provenance, не исходные page/rows.
    Регистрируйте отдельно после unsupported core selection, не после security
    или malformed errors. Caller изолирует сеть/container и отключает OCR на
    сервере; клиент не устанавливает и не аттестует Tika deployment.

    Raises:
        ValueError: Параметры конструктора неверного типа.
        ParserError: Fallback выключен (``PARSER_UNSUPPORTED_FEATURE``), нет extra
            (``PARSER_DEPENDENCY_UNAVAILABLE``), endpoint недоступен
            (``PARSER_TIKA_UNAVAILABLE``), timeout (``PROCESSING_TIMEOUT``) либо
            повреждён ответ (``PARSER_MALFORMED_INPUT``).
        SecurityPolicyError: Нет допуска или нарушена policy
            (``SECURITY_INPUT_REJECTED``), превышен бюджет
            (``SECURITY_LIMIT_EXCEEDED``).
    """

    adapter_id = "optional.tika"
    version = "1.0.0"
    priority = -1000

    def __init__(
        self,
        *,
        config: TikaConfig | None = None,
        limits: TikaParserLimits | None = None,
        approval: TikaEgressApproval | None = None,
    ) -> None:
        self._config = config if config is not None else TikaConfig()
        self._limits = limits if limits is not None else TikaParserLimits()
        self._approval = approval
        if (
            type(self.config) is not TikaConfig
            or type(self.limits) is not TikaParserLimits
            or (approval is not None and type(approval) is not TikaEgressApproval)
        ):
            raise ValueError("Некорректные параметры Tika adapter")

    @property
    def config(self) -> TikaConfig:
        """Вернуть неизменяемую конфигурацию без сетевого обращения."""

        return self._config

    @property
    def limits(self) -> TikaParserLimits:
        """Вернуть неизменяемые пределы transport и XML ответа."""

        return self._limits

    @property
    def approval(self) -> TikaEgressApproval | None:
        """Вернуть внешний egress review; наличие DTO ещё не означает разрешение."""

        return self._approval

    async def probe(self, source: SourceArtifact, context: ProbeContext) -> ProbeResult:
        """Вернуть ProbeResult по локальным первым 16 байтам source из context.

        HTTP не вызывается, approval не проверяется. Выключенный adapter не
        читает source; совпадение сигнатуры не является разрешением на upload.
        """

        match: tuple[str, bytes, str] | None = None
        if self.config.enabled:
            sample = bytearray()
            target = min(source.size_bytes, context.max_probe_bytes, 16)
            while len(sample) < target:
                await asyncio.sleep(0)
                chunk = await _read_checked(
                    context.reader, offset=len(sample), size=target - len(sample)
                )
                if not chunk:
                    break
                sample.extend(chunk)
            match = next(
                (
                    entry
                    for entry in _FALLBACK_SIGNATURES
                    if entry[0] in self.config.allowed_media_types
                    and sample.startswith(entry[1])
                ),
                None,
            )
        supported = match is not None
        if supported and (
            importlib.util.find_spec("httpx") is None
            or importlib.util.find_spec("defusedxml") is None
        ):
            raise missing_extra("tika")
        return ProbeResult(
            source=source.ref,
            adapter_id=self.adapter_id,
            adapter_version=self.version,
            supported=supported,
            confidence=Decimal("0.75") if supported else Decimal(0),
            format_id="tika" if supported else None,
            detected_media_type=match[0] if match else None,
            warnings=(
                "TIKA_CALLER_ISOLATION_REQUIRED",
                "TIKA_RESPONSE_RELATIVE_PROVENANCE",
            ),
            signals=(
                ProbeSignal(
                    kind=ProbeSignalKind.SIGNATURE,
                    outcome=ProbeSignalOutcome.MATCH
                    if supported
                    else ProbeSignalOutcome.INCONCLUSIVE,
                ),
                *advisory_signals(
                    source,
                    media_types=frozenset({match[0]}) if match else frozenset(),
                    extensions=frozenset({match[2]}) if match else frozenset(),
                ),
            ),
        )

    def _authorize(self, source: SourceArtifact) -> None:
        if not self.config.enabled:
            raise ParserError(
                error_code="PARSER_UNSUPPORTED_FEATURE",
                message="Tika fallback выключен.",
                details={"reason": "feature_disabled"},
            )
        approval = self.approval
        if (
            approval is None
            or approval.source_fingerprint != source.source_fingerprint
            or approval.classification is not DataClassification.PUBLIC
            or not approval.secrets_checked
            or approval.contains_secrets
        ):
            raise rejected("tika_egress_not_approved")

    def _rebase(
        self, node: ExtractedTreeNode, source: SourceArtifact, response_fingerprint: str
    ) -> ExtractedTreeNode:
        location = node.location
        if not isinstance(location, XPathLocation):
            raise malformed_response()
        provenance = ExtensionLocation(
            source=source.ref,
            namespace="tika:xhtml-v1",
            metadata=(
                ExtensionMetadataEntry(
                    key="response_fingerprint", value=response_fingerprint
                ),
                ExtensionMetadataEntry(key="xpath", value=location.xpath),
                ExtensionMetadataEntry(
                    key="namespace_prefix", value=location.namespace_prefix
                ),
                ExtensionMetadataEntry(
                    key="server_version_configured",
                    value=self.config.expected_server_version,
                ),
                ExtensionMetadataEntry(key="fidelity", value="response_relative"),
            ),
        )
        return node.model_copy(
            update={
                "location": provenance,
                "value": node.value.model_copy(update={"location": provenance})
                if node.value
                else None,
            }
        )

    async def _content(
        self, source: SourceArtifact, context: ParseContext
    ) -> bytes | None:
        self._authorize(source)
        Budget(self.adapter_id, self.limits.xml, context).check(
            "request_bytes",
            source.size_bytes,
            min(context.max_bytes, self.limits.max_request_bytes),
        )
        result = await self.probe(
            source,
            ProbeContext(
                reader=context.reader,
                source_fingerprint=context.source_fingerprint,
                max_probe_bytes=16,
            ),
        )
        if not result.supported:
            raise ParserError(
                error_code="PARSER_UNSUPPORTED_FEATURE",
                message="Формат не разрешён для Tika fallback.",
            )
        assert (
            self.config.endpoint is not None and result.detected_media_type is not None
        )
        content = await exchange(
            source,
            context,
            endpoint=self.config.endpoint,
            media_type=result.detected_media_type,
            expected_signature=next(
                signature
                for media_type, signature, _ in _FALLBACK_SIGNATURES
                if media_type == result.detected_media_type
            ),
            limits=self.limits,
        )
        if content is None:
            return None
        await _validate_xhtml(
            content, Budget(self.adapter_id, self.limits.xml, context)
        )
        return content

    async def _units(
        self, source: SourceArtifact, context: ParseContext
    ) -> AsyncGenerator[MarkupUnit]:
        deadline = asyncio.get_running_loop().time() + self.limits.timeout_seconds
        try:
            async with asyncio.timeout_at(deadline):
                content = await self._content(source, context)
        except TimeoutError:
            raise ParserError(
                error_code="PROCESSING_TIMEOUT", message="Истёк timeout Tika adapter."
            ) from None
        if content is None:
            return
        reader = _ResponseReader(content)
        response_source = SourceArtifact(
            artifact_id="tika-response",
            display_name="response.xhtml",
            media_type="application/xhtml+xml",
            size_bytes=len(content),
            source_fingerprint=reader.source_fingerprint,
        )
        response_context = replace(
            context,
            reader=reader,
            source_fingerprint=reader.source_fingerprint,
            max_bytes=self.limits.max_response_bytes,
        )
        stream = XmlParser(limits=self.limits.xml).parse(
            response_source, response_context
        )
        assert isinstance(stream, AsyncGenerator)
        try:
            while True:
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError
                async with asyncio.timeout_at(deadline):
                    batch = await anext(stream, None)
                if batch is None:
                    break
                yield MarkupUnit(
                    tuple(
                        self._rebase(node, source, reader.source_fingerprint)
                        for node in batch.trees
                    ),
                    records=batch.record_count or 0,
                )
        except TimeoutError:
            raise ParserError(
                error_code="PROCESSING_TIMEOUT", message="Истёк timeout Tika adapter."
            ) from None
        finally:
            await stream.aclose()

    def parse(
        self, source: SourceArtifact, context: ParseContext
    ) -> AsyncIterator[ExtractedBatch]:
        """Вернуть поток ExtractedBatch из XHTML для source и reader/context.

        При итерации проверяются approval, SHA и сигнатура одного bounded
        snapshot, затем выполняется HTTP PUT без credentials, redirects и retries.
        Весь bounded response проверяется до первого batch; raw values остаются
        недоверенными. Typed errors и cancellation не дают успешного manifest.
        """

        options = canonical_sha256_value(
            cast(
                CanonicalInput,
                {
                    "limits": asdict(self.limits),
                    "endpoint": self.config.endpoint,
                    "server_version_configured": self.config.expected_server_version,
                    "allowed_media_types": sorted(self.config.allowed_media_types),
                },
            )
        )
        return markup_batches(
            source,
            context,
            Budget(self.adapter_id, self.limits.xml, context),
            MarkupDocument(),
            self._units(source, context),
            unit_batch_size=1,
            adapter_options_fingerprint=options,
        )


__all__ = ("TikaConfig", "TikaEgressApproval", "TikaParserAdapter", "TikaParserLimits")
