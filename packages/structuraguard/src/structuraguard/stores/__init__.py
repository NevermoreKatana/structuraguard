"""Staging adapters без import-time I/O и обязательного SQLAlchemy import."""

from .memory import MemoryStagingStore
from .postgresql import PostgreSQLStagingStore, PostgreSQLStagingTarget

__all__ = ("MemoryStagingStore", "PostgreSQLStagingStore", "PostgreSQLStagingTarget")
