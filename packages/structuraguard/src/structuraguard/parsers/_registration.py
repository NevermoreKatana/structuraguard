"""Immutable identity и внутренние snapshots зарегистрированных parsers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from structuraguard.ports.parser import Parser


@dataclass(frozen=True, slots=True, kw_only=True)
class ParserIdentity:
    """Стабильная identity parser без ссылки на runtime object.

    Args:
        adapter_id: Канонический идентификатор adapter.
        version: Версия adapter, снятая при регистрации.
        priority: Стабильный приоритет deterministic selection.
        origin: Источник регистрации; в текущем milestone доступен ``manual``.

    Объект предназначен для чтения из ``ParserRegistrySnapshot``. Экземпляр,
    созданный вручную, не проходит registration validation и не подтверждает
    доверенность parser.
    """

    adapter_id: str
    version: str
    priority: int
    origin: Literal["manual"] = "manual"


@dataclass(frozen=True, slots=True, kw_only=True)
class RegisteredParser:
    """Trusted parser object и единожды снятая identity."""

    identity: ParserIdentity
    parser: Parser
    parser_class_target: tuple[str, str] | None
