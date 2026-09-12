"""Scope, injection, immutable request bindings и DB reader resource budgets."""

import sqlite3
from pathlib import Path

import pytest
from tests.fakes.mapping import refs
from tests.unit.validation.test_db_constraints import data, policy

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.constraint_validation import (
    ConstraintMatch,
    ConstraintReadPolicy,
    ConstraintReadRequest,
    ConstraintReadResult,
)
from structuraguard.contracts.database import DatabaseCatalog, DatabaseInspectionRequest
from structuraguard.database import SQLiteDatabaseAdapter, SQLiteTarget
from structuraguard.database.constraint_reader import DatabaseConstraintReader
from structuraguard.exceptions import DatabaseInspectionError, ValidationError
from structuraguard.validation import DatabaseConstraintValidator


async def fixture(
    tmp_path: Path,
) -> tuple[SQLiteTarget, DatabaseCatalog, ConstraintReadRequest]:
    path = tmp_path / "injection.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            'CREATE TABLE "semi;table"("key;column" TEXT UNIQUE); CREATE TABLE secret(x TEXT);'
        )
        connection.execute(
            'INSERT INTO "semi;table" VALUES(?)', ("'; DROP TABLE secret; --",)
        )
    target = SQLiteTarget(
        path=path, target_id="boundary", include_tables=("semi;table",)
    )
    db = await SQLiteDatabaseAdapter(target).inspect(
        DatabaseInspectionRequest(
            target_id=target.target_id,
            target_policy_fingerprint=target.policy_fingerprint,
        )
    )
    table = db.schemas[0].tables[0]
    request = ConstraintReadRequest.model_validate(
        {
            "target_id": target.target_id,
            "target_policy_fingerprint": target.policy_fingerprint,
            "database_fingerprint": db.database_fingerprint,
            "lookups": (
                {
                    "lookup_id": "k",
                    "table_id": table.table_id,
                    "column_ids": (table.columns[0].column_id,),
                    "values": (
                        {"kind": "string", "value": "'; DROP TABLE secret; --"},
                    ),
                },
            ),
        }
    )
    return target, db, request


@pytest.mark.anyio
async def test_identifiers_are_quoted_values_are_bound(tmp_path: Path) -> None:
    target, db, request = await fixture(tmp_path)
    reader = DatabaseConstraintReader(
        target, policy=ConstraintReadPolicy(allow_columns=refs(db))
    )
    report = await reader.read(request, catalog=db)
    assert report.matches[0].exists and report.matches[0].conflicts
    with sqlite3.connect(target.path) as connection:
        assert connection.execute("SELECT count(*) FROM secret").fetchone() == (0,)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "attack", ["table", "column", "allowlist", "policy", "target", "key_type"]
)
async def test_reader_rejects_scope_before_connection(
    tmp_path: Path, attack: str
) -> None:
    target, db, request = await fixture(tmp_path)
    raw = request.model_dump()
    configured = ConstraintReadPolicy(allow_columns=refs(db))
    if attack == "table":
        raw["lookups"][0]["table_id"] = "secret"
    elif attack == "column":
        raw["lookups"][0]["column_ids"] = ("secret",)
    elif attack == "allowlist":
        configured = ConstraintReadPolicy(allow_columns=())
    elif attack == "policy":
        raw["target_policy_fingerprint"] = "sha256:" + "0" * 64
    elif attack == "target":
        raw["target_id"] = "other"
    else:
        raw["lookups"][0]["values"] = ({"kind": "integer", "value": 1},)
    target.path.unlink()
    with pytest.raises(DatabaseInspectionError) as error:
        await DatabaseConstraintReader(target, policy=configured).read(
            ConstraintReadRequest.model_validate(raw), catalog=db
        )
    assert error.value.error_code in {
        "DB_CONSTRAINT_UNVERIFIED",
        "TARGET_NOT_ALLOWED",
        "DATABASE_POLICY_MISMATCH",
        "DATABASE_TARGET_MISMATCH",
    }
    assert str(target.path) not in str(error.value)


@pytest.mark.anyio
async def test_drift_is_detected_inside_read_transaction(tmp_path: Path) -> None:
    target, db, request = await fixture(tmp_path)
    with sqlite3.connect(target.path) as connection:
        connection.execute('ALTER TABLE "semi;table" ADD COLUMN extra INTEGER')
    with pytest.raises(DatabaseInspectionError) as error:
        await DatabaseConstraintReader(
            target, policy=ConstraintReadPolicy(allow_columns=refs(db))
        ).read(request, catalog=db)
    assert error.value.error_code == "DATABASE_SCHEMA_DRIFT"


@pytest.mark.anyio
async def test_forged_reader_response_cannot_authorize_acceptance(
    tmp_path: Path,
) -> None:
    _, db, request = await fixture(tmp_path)

    class ForgedReader:
        async def read(
            self, request: ConstraintReadRequest, *, catalog: DatabaseCatalog
        ) -> ConstraintReadResult:
            matches = (
                ConstraintMatch(lookup_id="wrong", exists=False, conflicts=False),
            )
            return ConstraintReadResult(
                request_fingerprint=request.fingerprint,
                snapshot_fingerprint=canonical_sha256_value(
                    (request.fingerprint, tuple(m.canonical_json() for m in matches))
                ),
                matches=matches,
            )

    key = request.lookups[0]
    source = data((key.table_id, {key.column_ids[0]: key.values[0]}))
    with pytest.raises(ValidationError) as error:
        await DatabaseConstraintValidator(policy(db), reader=ForgedReader()).validate(
            source, catalog=db
        )
    assert error.value.error_code == "DB_READER_RESULT_INVALID"


@pytest.mark.anyio
async def test_query_budget_before_io(tmp_path: Path) -> None:
    target, db, request = await fixture(tmp_path)
    request = request.model_copy(
        update={
            "lookups": tuple(
                request.lookups[0].model_copy(update={"lookup_id": str(i)})
                for i in range(2)
            )
        }
    )
    target.path.unlink()
    with pytest.raises(DatabaseInspectionError) as error:
        await DatabaseConstraintReader(
            target,
            policy=ConstraintReadPolicy(
                allow_columns=refs(db), chunk_size=1, max_queries=1
            ),
        ).read(request, catalog=db)
    assert error.value.error_code == "SECURITY_LIMIT_EXCEEDED"


@pytest.mark.anyio
async def test_timeout_is_a_failure_not_an_empty_database(tmp_path: Path) -> None:
    from structuraguard.database.target import InspectionLimits

    target, db, request = await fixture(tmp_path)
    target = target.model_copy(
        update={"limits": InspectionLimits(timeout_seconds=0.000000001)}
    )
    db = db.model_copy(update={"target_policy_fingerprint": target.policy_fingerprint})
    request = request.model_copy(
        update={"target_policy_fingerprint": target.policy_fingerprint}
    )
    with pytest.raises(DatabaseInspectionError) as error:
        await DatabaseConstraintReader(
            target, policy=ConstraintReadPolicy(allow_columns=refs(db))
        ).read(request, catalog=db)
    assert error.value.error_code == "PROCESSING_TIMEOUT"
