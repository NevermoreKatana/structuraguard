"""PostgreSQL 16/18: настоящий composite PK, generated metadata и drift."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from tests.fakes.mapping_validation import bind_catalog, case, replan

from structuraguard.contracts.common import LoadOperation, ValidationDecision
from structuraguard.contracts.database import DatabaseInspectionRequest
from structuraguard.contracts.mapping import MappingPlanValidationResult
from structuraguard.database import PostgreSQLDatabaseAdapter, PostgreSQLTarget
from structuraguard.mapping import MappingPlanValidator

pytestmark = [
    pytest.mark.integration,
    pytest.mark.database_integration,
    pytest.mark.anyio,
]


@pytest.mark.parametrize("operation", [LoadOperation.INSERT_ONLY, LoadOperation.UPSERT])
async def test_pg_composite_pk_and_real_drift(
    pg_target: PostgreSQLTarget, pg_dsn: str, operation: LoadOperation
) -> None:
    adapter = PostgreSQLDatabaseAdapter(pg_target)
    request = DatabaseInspectionRequest(
        target_id=pg_target.target_id,
        target_policy_fingerprint=pg_target.policy_fingerprint,
    )
    db = await adapter.inspect(request)
    parents = next(t for s in db.schemas for t in s.tables if t.name == "parents")
    plan, manifest, _, _, profile = await case()
    plan, policy = bind_catalog(plan, db, parents, ("a", "b"))
    plan = replan(plan, operation=operation)
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert result.decision is ValidationDecision.ACCEPTED
    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "ALTER TABLE ref.parents ADD COLUMN m11_extra text"
            )
        current = await adapter.inspect(request)
        result = await MappingPlanValidator(policy=policy).validate(
            plan, manifest, current, profile=profile
        )
        assert "DATABASE_SCHEMA_DRIFT" in {i.code for i in result.issues}
    finally:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "ALTER TABLE ref.parents DROP COLUMN IF EXISTS m11_extra"
            )
        await engine.dispose()


@pytest.mark.parametrize(
    ("names", "code"),
    [
        (("by_default", "qty"), None),
        (("id", "qty"), "MAPPING_COLUMN_NOT_WRITABLE"),
        (("total", "qty"), "MAPPING_GENERATED_COLUMN"),
        (("by_default", "parent_a"), "MAPPING_RELATION_UNRESOLVED"),
    ],
)
async def test_pg_reflected_identity_generation_and_composite_fk(
    pg_target: PostgreSQLTarget, names: tuple[str, str], code: str | None
) -> None:
    adapter = PostgreSQLDatabaseAdapter(pg_target)
    db = await adapter.inspect(
        DatabaseInspectionRequest(
            target_id=pg_target.target_id,
            target_policy_fingerprint=pg_target.policy_fingerprint,
        )
    )
    items = next(
        t
        for s in db.schemas
        for t in s.tables
        if t.schema_name == "app" and t.name == "items"
    )
    plan, manifest, _, _, profile = await case()
    plan, policy = bind_catalog(plan, db, items, names)
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert isinstance(result, MappingPlanValidationResult)
    if code is None:
        assert result.decision is ValidationDecision.ACCEPTED
        assert result.validated_plan is not None
        assert result.evidence is not None
        assert result.evidence.identities[0].kind == "database_generated"
    else:
        assert code in {i.code for i in result.issues}
        assert result.decision is ValidationDecision.REJECTED
        assert result.validated_plan is None
