"""Read-only Database Inspector. SQLAlchemy и соединения загружаются при вызове."""

from .constraint_reader import DatabaseConstraintReader
from .postgresql import PostgreSQLDatabaseAdapter
from .sqlite import SQLiteDatabaseAdapter
from .target import InspectionLimits, PostgreSQLTarget, SQLiteTarget

__all__ = [
    "DatabaseConstraintReader",
    "InspectionLimits",
    "PostgreSQLDatabaseAdapter",
    "PostgreSQLTarget",
    "SQLiteDatabaseAdapter",
    "SQLiteTarget",
]
