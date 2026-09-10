"""Policy failures до подключения; без PostgreSQL imitation."""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from structuraguard.contracts import DatabaseInspectionRequest
from structuraguard.database import (
    InspectionLimits,
    PostgreSQLDatabaseAdapter,
    PostgreSQLTarget,
)
from structuraguard.exceptions import DatabaseInspectionError


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"include_tables": ()}, "TARGET_NOT_ALLOWED"),
        ({"deny_schemas": ("app",)}, "TARGET_NOT_ALLOWED"),
        ({"deny_tables": (("app", "items"),)}, "TARGET_NOT_ALLOWED"),
        ({"include_schemas": ("pg_catalog",)}, "TARGET_NOT_ALLOWED"),
        ({"include_columns": ("id",)}, "DATABASE_METADATA_UNSUPPORTED"),
        ({"deny_columns": ("id",)}, "DATABASE_METADATA_UNSUPPORTED"),
        (
            {
                "include_tables": (("app", "items"), ("app", "other")),
                "limits": InspectionLimits(max_tables=1),
            },
            "SECURITY_LIMIT_EXCEEDED",
        ),
    ],
)
async def test_rejects_scope_before_opening_connection(
    updates: dict[str, object],
    code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = PostgreSQLTarget.model_validate(
        {
            "dsn": SecretStr("postgresql+asyncpg://inspector:canary@localhost/db"),
            "target_id": "test",
            "include_schemas": ("app",),
            "include_tables": (("app", "items"),),
            **updates,
        }
    )

    def never_open(adapter: PostgreSQLDatabaseAdapter) -> AsyncEngine:
        pytest.fail("policy должна применяться до открытия соединения")

    monkeypatch.setattr(PostgreSQLDatabaseAdapter, "_engine", never_open)
    request = DatabaseInspectionRequest(
        target_id=target.target_id, target_policy_fingerprint=target.policy_fingerprint
    )
    with pytest.raises(DatabaseInspectionError) as caught:
        await PostgreSQLDatabaseAdapter(target).inspect_metadata(request)
    assert caught.value.error_code == code


def test_policy_hash_excludes_credentials_and_selector_order() -> None:
    target = PostgreSQLTarget(
        dsn=SecretStr("postgresql+asyncpg://inspector:canary@localhost/db"),
        target_id="test",
        include_schemas=("app", "ref"),
        include_tables=(("app", "items"), ("ref", "parents")),
    )
    reordered = target.model_copy(
        update={
            "dsn": SecretStr("postgresql+asyncpg://other:rotated@localhost/db"),
            "include_schemas": tuple(reversed(target.include_schemas)),
            "include_tables": tuple(reversed(target.include_tables)),
        }
    )
    assert target.policy_fingerprint == reordered.policy_fingerprint
    assert (
        target.policy_fingerprint
        != target.model_copy(
            update={"deny_tables": (("app", "items"),)}
        ).policy_fingerprint
    )
    assert "canary" not in repr(target) + target.model_dump_json()
