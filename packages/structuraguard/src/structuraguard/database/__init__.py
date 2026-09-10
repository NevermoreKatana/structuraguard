"""Read-only Database Inspector. SQLAlchemy и соединения загружаются при вызове."""

from .postgresql import PostgreSQLDatabaseAdapter
from .sqlite import SQLiteDatabaseAdapter
from .target import InspectionLimits, PostgreSQLTarget, SQLiteTarget

__all__ = [
    "InspectionLimits",
    "PostgreSQLDatabaseAdapter",
    "PostgreSQLTarget",
    "SQLiteDatabaseAdapter",
    "SQLiteTarget",
]
