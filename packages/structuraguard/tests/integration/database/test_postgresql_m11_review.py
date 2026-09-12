"""Реальная metadata PostgreSQL 16/18 для findings финального review M11."""

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from tests.fakes.mapping import scope_for
from tests.fakes.mapping_validation import bind_catalog, case, replan

from structuraguard.contracts import (
    CatalogColumnRef,
    DatabaseCatalog,
    DatabaseInspectionRequest,
    IntegerScalar,
    LoadOperation,
    MappingPlanValidationResult,
    MappingRelation,
    MappingValidationPolicy,
    NumberScalar,
    ValidationDecision,
)
from structuraguard.database import PostgreSQLDatabaseAdapter, PostgreSQLTarget
from structuraguard.mapping import MappingPlanValidator

pytestmark = [
    pytest.mark.integration,
    pytest.mark.database_integration,
    pytest.mark.anyio,
]


@pytest.fixture
async def pg_review_catalog(
    pg_target: PostgreSQLTarget, pg_dsn: str
) -> AsyncIterator[DatabaseCatalog]:
    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            for statement in (
                "CREATE TABLE app.review_keys (id integer NOT NULL, amount integer NOT NULL)",
                "CREATE UNIQUE INDEX review_duplicate ON app.review_keys (amount, amount)",
                "CREATE TABLE app.review_real (id integer PRIMARY KEY, amount real)",
                "CREATE TABLE app.review_double (id integer PRIMARY KEY, amount double precision)",
                "CREATE TABLE app.review_parent (id bigint PRIMARY KEY)",
                "CREATE TABLE app.review_child (id integer PRIMARY KEY REFERENCES app.review_parent(id))",
            ):
                await connection.exec_driver_sql(statement)
        target = PostgreSQLTarget(
            dsn=pg_target.dsn,
            target_id="m11-review",
            include_schemas=("app",),
            include_tables=tuple(
                ("app", name)
                for name in (
                    "review_keys",
                    "review_real",
                    "review_double",
                    "review_parent",
                    "review_child",
                )
            ),
        )
        yield await PostgreSQLDatabaseAdapter(target).inspect(
            DatabaseInspectionRequest(
                target_id=target.target_id,
                target_policy_fingerprint=target.policy_fingerprint,
            )
        )
    finally:
        try:
            async with engine.begin() as connection:
                for statement in (
                    "DROP TABLE IF EXISTS app.review_child",
                    "DROP TABLE IF EXISTS app.review_parent",
                    "DROP TABLE IF EXISTS app.review_double",
                    "DROP TABLE IF EXISTS app.review_real",
                    "DROP TABLE IF EXISTS app.review_keys",
                ):
                    await connection.exec_driver_sql(statement)
        finally:
            await engine.dispose()


@pytest.mark.parametrize("overflow", [True, False])
async def test_pg_float_overflow_matches_server_rejection(
    pg_review_catalog: DatabaseCatalog, pg_dsn: str, overflow: bool
) -> None:
    db = pg_review_catalog
    name = "review_real" if overflow else "review_double"
    table = next(t for s in db.schemas for t in s.tables if t.name == name)
    plan, manifest, _, _, profile = await case(
        amount=NumberScalar(value=1e100), semantic_type="unresolved"
    )
    plan, policy = bind_catalog(plan, db, table)
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert result.decision is (
        ValidationDecision.REJECTED if overflow else ValidationDecision.ACCEPTED
    )
    assert isinstance(result, MappingPlanValidationResult)
    assert (result.validated_plan is None) is overflow
    assert {i.code for i in result.issues} == (
        {"MAPPING_NUMERIC_OVERFLOW"} if overflow else set()
    )
    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            if overflow:
                with pytest.raises(DBAPIError) as caught:
                    await connection.exec_driver_sql(
                        "INSERT INTO app.review_real VALUES (1, 1e100)"
                    )
                assert getattr(caught.value.orig, "sqlstate", None) == "22003"
            else:
                result_row = await connection.exec_driver_sql(
                    "INSERT INTO app.review_double VALUES (1, 1e100) RETURNING amount"
                )
                assert result_row.scalar_one() == 1e100
    finally:
        await engine.dispose()


async def test_pg_duplicate_index_returns_all_issues(
    pg_review_catalog: DatabaseCatalog,
) -> None:
    db = pg_review_catalog
    table = next(t for s in db.schemas for t in s.tables if t.name == "review_keys")
    plan, manifest, _, _, profile = await case()
    plan, policy = bind_catalog(plan, db, table)
    result = await MappingPlanValidator(policy=policy).validate(
        replan(plan, operation=LoadOperation.UPSERT, confidence=Decimal("0.1")),
        manifest,
        db,
        profile=profile,
    )
    assert isinstance(result, MappingPlanValidationResult)
    assert result.decision is ValidationDecision.REJECTED
    assert result.validated_plan is None
    assert {i.code for i in result.issues} == {
        "MAPPING_IDENTITY_REQUIRED",
        "MAPPING_CONFIDENCE_BELOW_THRESHOLD",
    }


async def test_pg_integer_fk_accepts_different_widths(
    pg_review_catalog: DatabaseCatalog,
) -> None:
    db = pg_review_catalog
    tables = {t.name: t for s in db.schemas for t in s.tables}
    parent, child = tables["review_parent"], tables["review_child"]
    fk = child.foreign_keys[0]
    plan, manifest, _, _, profile = await case(amount=IntegerScalar(value=1))
    mappings = tuple(
        m.model_copy(
            update={
                "target": CatalogColumnRef(
                    table_id=t.table_id, column_id=t.primary_key[0]
                )
            }
        )
        for m, t in zip(plan.mappings, (parent, child), strict=True)
    )
    policy = MappingValidationPolicy(
        policy_id="m11-review",
        scope=scope_for(db),
        allow_schemas=("app",),
        allow_tables=tuple((t.schema_name, t.name) for t in tables.values()),
        source_identity_allow=tuple(m.target for m in mappings),
    )
    plan = replan(
        plan,
        schema_version="1.1.0",
        mappings=mappings,
        database_fingerprint=db.database_fingerprint,
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        relations=(
            MappingRelation(
                foreign_key_id=fk.foreign_key_id,
                child_table_id=child.table_id,
                parent_table_id=parent.table_id,
                child_column_ids=fk.column_ids,
                parent_column_ids=fk.referenced_column_ids,
                child_sources=(mappings[1].source,),
                parent_sources=(mappings[0].source,),
                strategy="mapped_parent",
            ),
        ),
    )
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert isinstance(result, MappingPlanValidationResult)
    assert result.decision is ValidationDecision.ACCEPTED
    assert not result.issues
    assert result.validated_plan is not None
    assert result.evidence is not None and result.evidence.load_order == (
        parent.table_id,
        child.table_id,
    )
