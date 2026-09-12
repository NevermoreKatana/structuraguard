"""Позиции недоверенных descriptors, ограниченность отчёта и отсутствие полномочий I/O."""

import builtins
import json
import socket
import sqlite3
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.sql.compiler import SQLCompiler
from tests.fakes.mapping import catalog, column, table
from tests.fakes.mapping_validation import case, replan

from structuraguard.contracts.common import ValidationDecision
from structuraguard.contracts.mapping import MappingPlan
from structuraguard.contracts.mapping_validation import (
    MappingPlanInputReport,
    MappingValidationOptions,
)
from structuraguard.exceptions import ValidationError
from structuraguard.mapping import MappingPlanValidator

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("section", ["identities", "relations"])
async def test_malformed_descriptor_does_not_shift_later_issue_location(
    section: str,
) -> None:
    plan, manifest, db, policy, _ = await case()
    payload = plan.model_dump(mode="json")
    payload.pop("fingerprint")
    payload["schema_version"] = "1.1.0"
    descriptor: dict[str, object]
    if section == "identities":
        descriptor = {"table_id": "absent", "kind": "explicit", "column_ids": ["id"]}
        expected_code = "MAPPING_UPSERT_KEY_INVALID"
    else:
        descriptor = {
            "foreign_key_id": "absent",
            "child_table_id": "public.orders",
            "parent_table_id": "public.orders",
            "child_column_ids": ["id"],
            "parent_column_ids": ["id"],
            "child_sources": [plan.mappings[0].source.model_dump(mode="json")],
            "strategy": "source_values",
        }
        expected_code = "MAPPING_FK_NOT_FOUND"
    payload[section] = [{"sql": "payload-canary"}, descriptor]
    result = await MappingPlanValidator(policy=policy).validate_json(
        json.dumps(payload).encode(), manifest, db
    )
    assert isinstance(result, MappingPlanInputReport)
    assert result.complete
    located = {
        (i.code, loc.section, loc.index)
        for i, loc in zip(result.issues, result.locations, strict=True)
    }
    assert ("MAPPING_PLAN_INVALID", section, 0) in located
    assert (expected_code, section, 1) in located
    assert "payload-canary" not in result.canonical_json()


@pytest.mark.parametrize(
    "budget",
    ["max_columns", "max_tables", "max_issues", "max_result_bytes", "max_operations"],
)
async def test_catalog_report_and_work_limits_cannot_return_acceptance(
    budget: str,
) -> None:
    plan, manifest, db, policy, profile = await case()
    if budget == "max_tables":
        db = catalog(db.schemas[0].tables[0], table("other", column("x")))
        plan = replan(plan, database_fingerprint=db.database_fingerprint)
    if budget == "max_issues":
        plan = replan(
            plan,
            mappings=tuple(
                m.model_copy(update={"confidence": Decimal("0.1")})
                for m in plan.mappings
            ),
        )
    options = MappingValidationOptions.model_validate({budget: 1})
    with pytest.raises(ValidationError) as caught:
        await MappingPlanValidator(policy=policy, options=options).validate(
            plan, manifest, db, profile=profile
        )
    assert caught.value.error_code == "MAPPING_LIMIT_EXCEEDED"


@pytest.mark.parametrize("oversized", ["identifier", "decimal"])
async def test_typed_preflight_runs_before_model_dump(
    oversized: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, manifest, db, policy, _ = await case()
    changed = (
        {"plan_id": "x" * 65537}
        if oversized == "identifier"
        else {"confidence": Decimal("1e1025")}
    )
    plan = plan.model_copy(update=changed)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("serialization ran before input budget")

    monkeypatch.setattr(MappingPlan, "model_dump", forbidden)
    with pytest.raises(ValidationError) as caught:
        await MappingPlanValidator(policy=policy).validate(plan, manifest, db)
    assert caught.value.error_code == "MAPPING_LIMIT_EXCEEDED"


async def test_json_byte_limit_runs_before_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manifest, db, policy, _ = await case()
    validator = MappingPlanValidator(
        policy=policy, options=MappingValidationOptions(max_input_bytes=65536)
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("JSON decoded before input budget")

    monkeypatch.setattr(json, "loads", forbidden)
    with pytest.raises(ValidationError) as caught:
        await validator.validate_json(b" " * 65537, manifest, db)
    assert caught.value.error_code == "MAPPING_LIMIT_EXCEEDED"


async def test_validation_uses_no_io_or_sql_compilation_and_preserves_all_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, manifest, db, policy, profile = await case()
    inputs = (plan, manifest, db, policy, profile)
    before = tuple(value.canonical_json() for value in inputs)
    payload = plan.canonical_json().encode()

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("validator crossed a forbidden I/O or SQL boundary")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(Path, "open", forbidden)
        patch.setattr(socket.socket, "connect", forbidden)
        patch.setattr(sqlite3, "connect", forbidden)
        patch.setattr(Connection, "execute", forbidden)
        patch.setattr(SQLCompiler, "__init__", forbidden)
        patch.setattr(httpx.Client, "send", forbidden)
        patch.setattr(httpx.AsyncClient, "send", forbidden)
        validator = MappingPlanValidator(policy=policy)
        typed = await validator.validate(plan, manifest, db, profile=profile)
        wire = await validator.validate_json(payload, manifest, db, profile=profile)
    assert typed.decision is wire.decision is ValidationDecision.ACCEPTED
    assert tuple(value.canonical_json() for value in inputs) == before


@pytest.mark.parametrize(
    "operation",
    [
        "append",
        "update_only",
        "merge",
        "DELETE",
        "CREATE",
        "ALTER",
        "DROP",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
    ],
)
async def test_closed_operations_reject_dml_and_ddl(operation: str) -> None:
    plan, manifest, db, policy, _ = await case()
    payload = plan.model_dump(mode="json")
    payload.pop("fingerprint")
    payload["operation"] = operation
    result = await MappingPlanValidator(policy=policy).validate_json(
        json.dumps(payload).encode(), manifest, db
    )
    assert isinstance(result, MappingPlanInputReport)
    assert "MAPPING_OPERATION_FORBIDDEN" in {i.code for i in result.issues}
    if operation in {"CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT", "REVOKE"}:
        assert "MAPPING_DDL_FORBIDDEN" in {i.code for i in result.issues}
