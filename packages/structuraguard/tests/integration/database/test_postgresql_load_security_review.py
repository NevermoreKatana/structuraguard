"""Security regressions M13 ledger на реальной writer connection."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.database.test_postgresql_dry_run import dry_db as dry_db
from tests.integration.database.test_postgresql_load_outcomes import (
    LedgerDatabase,
    parent_case,
)
from tests.integration.database.test_postgresql_load_outcomes import (
    ledger_db as ledger_db,
)
from tests.integration.database.test_postgresql_staging_security import (
    observe_decoded_text,
)

from structuraguard.database import _load_transaction
from structuraguard.database.target import PostgreSQLTarget
from structuraguard.database.writer_target import writer_engine
from structuraguard.exceptions import LoadError

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.integration,
    pytest.mark.database_integration,
]


async def test_ledger_schema_fingerprint_is_bounded_before_transfer(
    ledger_db: LedgerDatabase, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = await parent_case(ledger_db, pg_dsn)
    async with ledger_db.db.engine.begin() as connection:
        await connection.execute(
            text(
                f'UPDATE "{ledger_db.policy.schema_name}".schema_info SET fingerprint=:value'
            ),
            {"value": "sensitive-ledger-fingerprint" * 10000},
        )
    sizes: list[int] = []

    def engine(target: PostgreSQLTarget) -> AsyncEngine:
        result = writer_engine(target)
        observe_decoded_text(result, sizes)
        return result

    monkeypatch.setattr(_load_transaction, "writer_engine", engine)
    before = await ledger_db.unchanged()
    with pytest.raises(LoadError, match="LOAD_LEDGER_SCHEMA_INVALID"):
        await case.loader.execute(case.request)
    assert sizes and max(sizes) <= 65536
    assert await ledger_db.unchanged() == before
