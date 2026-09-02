"""Публичный API StructuraGuard SDK без eager runtime imports."""

import typing as _typing
from importlib import import_module as _import_module
from typing import cast as _cast

if _typing.TYPE_CHECKING:
    from .config import SDKConfig
    from .exceptions import (
        DatabaseInspectionError,
        LoadError,
        MappingError,
        OperationNotImplementedError,
        ParserError,
        SecurityPolicyError,
        SourceError,
        StructuraGuardError,
        ValidationError,
    )
    from .sdk import AsyncStructuraGuard
    from .sync_sdk import StructuraGuard

__all__ = (
    "AsyncStructuraGuard",
    "DatabaseInspectionError",
    "LoadError",
    "MappingError",
    "OperationNotImplementedError",
    "ParserError",
    "SDKConfig",
    "SecurityPolicyError",
    "SourceError",
    "StructuraGuard",
    "StructuraGuardError",
    "ValidationError",
)

_EXPORT_MODULES = (
    ("AsyncStructuraGuard", "structuraguard.sdk"),
    ("DatabaseInspectionError", "structuraguard.exceptions"),
    ("LoadError", "structuraguard.exceptions"),
    ("MappingError", "structuraguard.exceptions"),
    ("OperationNotImplementedError", "structuraguard.exceptions"),
    ("ParserError", "structuraguard.exceptions"),
    ("SDKConfig", "structuraguard.config"),
    ("SecurityPolicyError", "structuraguard.exceptions"),
    ("SourceError", "structuraguard.exceptions"),
    ("StructuraGuard", "structuraguard.sync_sdk"),
    ("StructuraGuardError", "structuraguard.exceptions"),
    ("ValidationError", "structuraguard.exceptions"),
)


if not _typing.TYPE_CHECKING:

    def __getattr__(name: str) -> object:
        """Загрузить публичный объект только после явного обращения пользователя."""

        for export_name, module_name in _EXPORT_MODULES:
            if name == export_name:
                module = _import_module(module_name)
                return _cast(object, getattr(module, name))
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Вернуть имена отложенных экспортов для стандартной интроспекции."""

    return sorted(set(globals()) | set(__all__))
