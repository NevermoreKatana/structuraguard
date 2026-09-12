"""Regression tests четырёх findings финального review M11."""

from decimal import Decimal

import pytest
from tests.fakes.mapping import catalog, scope_for
from tests.fakes.mapping_validation import case, replan

from structuraguard.contracts import (
    CatalogColumnRef,
    DatabaseType,
    ForeignKeyCatalog,
    IndexCatalog,
    IndexKeyCatalog,
    IntegerScalar,
    LoadOperation,
    MappingPlanValidationResult,
    MappingRelation,
    NumberScalar,
    TableCatalog,
    ValidationDecision,
)
from structuraguard.mapping import MappingPlanValidator

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("native", "value", "overflow"),
    [
        ("real", 1e100, True),
        ("real", -1e100, True),
        ("float4", 1e100, True),
        ("real", float.fromhex("0x1.fffffep+127"), False),
        ("real", -float.fromhex("0x1.fffffep+127"), False),
        ("real", 0.0, False),
        ("double precision", 1e100, False),
        ("float8", -1e100, False),
    ],
)
async def test_float_native_range_controls_acceptance(
    native: str, value: float, overflow: bool
) -> None:
    plan, manifest, db, policy, profile = await case(
        amount=NumberScalar(value=value), semantic_type="unresolved"
    )
    target = db.schemas[0].tables[0]
    pk, amount = target.columns
    assert amount.inspection is not None
    amount = amount.model_copy(
        update={
            "type_name": "float",
            "inspection": amount.inspection.model_copy(
                update={
                    "data_type": DatabaseType(
                        native_type=native, canonical_type="float", type_kind="builtin"
                    )
                }
            ),
        }
    )
    db = catalog(target.model_copy(update={"columns": (pk, amount)}))
    plan = replan(plan, database_fingerprint=db.database_fingerprint)
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert isinstance(result, MappingPlanValidationResult)
    assert result.decision is (
        ValidationDecision.REJECTED if overflow else ValidationDecision.ACCEPTED
    )
    assert (result.validated_plan is None) is overflow
    assert {i.code for i in result.issues} == (
        {"MAPPING_NUMERIC_OVERFLOW"} if overflow else set()
    )


@pytest.mark.parametrize("duplicate", [True, False])
async def test_repeated_unique_index_component_does_not_interrupt_report(
    duplicate: bool,
) -> None:
    plan, manifest, db, policy, profile = await case()
    target = db.schemas[0].tables[0]
    assert target.inspection is not None
    index = IndexCatalog(
        index_id="unique_amount",
        name="unique_amount",
        keys=(IndexKeyCatalog(column_id="amount"),) * (2 if duplicate else 1),
        unique=True,
        origin="index",
    )
    db = catalog(
        target.model_copy(
            update={
                "primary_key": (),
                "columns": tuple(
                    c.model_copy(update={"primary_key": False, "nullable": False})
                    for c in target.columns
                ),
                "inspection": target.inspection.model_copy(
                    update={"indexes": (index,)}
                ),
            }
        )
    )
    plan = replan(
        plan,
        database_fingerprint=db.database_fingerprint,
        operation=LoadOperation.UPSERT,
        confidence=Decimal("0.1") if duplicate else Decimal(1),
    )
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert isinstance(result, MappingPlanValidationResult)
    assert result.decision is (
        ValidationDecision.REJECTED if duplicate else ValidationDecision.ACCEPTED
    )
    assert (result.validated_plan is None) is duplicate
    assert {i.code for i in result.issues} == (
        {"MAPPING_IDENTITY_REQUIRED", "MAPPING_CONFIDENCE_BELOW_THRESHOLD"}
        if duplicate
        else set()
    )


@pytest.mark.parametrize(
    ("parent_type", "child_type", "value", "code"),
    [
        ("bigint", "integer", 1, None),
        ("integer", "bigint", 1, None),
        ("int8", "int4", 1, None),
        ("smallint", "integer", 1, None),
        ("bigint", "integer", 2**31, "MAPPING_NUMERIC_OVERFLOW"),
        ("integer", "bigint", 2**31, "MAPPING_NUMERIC_OVERFLOW"),
        ("unknown_integer", "integer", 1, "MAPPING_TYPE_INCOMPATIBLE"),
    ],
)
async def test_integer_fk_widths_are_compatible_without_bypassing_value_range(
    parent_type: str, child_type: str, value: int, code: str | None
) -> None:
    plan, manifest, original, policy, profile = await case(
        amount=IntegerScalar(value=value)
    )
    base = original.schemas[0].tables[0]
    first, second = plan.mappings
    columns = {c.column_id: c for c in base.columns}
    pk = columns["id"]
    assert pk.inspection is not None
    parent = TableCatalog(
        table_id="public.parent",
        schema_name="public",
        name="parent",
        columns=(
            pk.model_copy(
                update={
                    "inspection": pk.inspection.model_copy(
                        update={
                            "data_type": DatabaseType(
                                native_type=parent_type,
                                canonical_type="integer",
                                type_kind="builtin",
                            )
                        }
                    )
                }
            ),
        ),
        primary_key=("id",),
        inspection=base.inspection,
    )
    child = TableCatalog(
        table_id="public.child",
        schema_name="public",
        name="child",
        columns=(
            pk.model_copy(
                update={
                    "inspection": pk.inspection.model_copy(
                        update={
                            "data_type": DatabaseType(
                                native_type=child_type,
                                canonical_type="integer",
                                type_kind="builtin",
                            )
                        }
                    )
                }
            ),
        ),
        primary_key=("id",),
        inspection=base.inspection,
        foreign_keys=(
            ForeignKeyCatalog(
                foreign_key_id="fk",
                column_ids=("id",),
                referenced_table_id=parent.table_id,
                referenced_column_ids=("id",),
            ),
        ),
    )
    # Для проверки переполнения parent направляем amount туда; source refs различны.
    sources = (
        (second, first) if parent_type == "integer" and value > 1 else (first, second)
    )
    mappings = tuple(
        m.model_copy(
            update={"target": CatalogColumnRef(table_id=t.table_id, column_id="id")}
        )
        for m, t in zip(sources, (parent, child), strict=True)
    )
    db = catalog(parent, child)
    policy = policy.model_copy(
        update={
            "scope": scope_for(db),
            "allow_tables": (("public", "parent"), ("public", "child")),
            "source_identity_allow": tuple(m.target for m in mappings),
        }
    )
    relation = MappingRelation(
        foreign_key_id="fk",
        child_table_id=child.table_id,
        parent_table_id=parent.table_id,
        child_column_ids=("id",),
        parent_column_ids=("id",),
        child_sources=(mappings[1].source,),
        parent_sources=(mappings[0].source,),
        strategy="mapped_parent",
    )
    plan = replan(
        plan,
        schema_version="1.1.0",
        mappings=mappings,
        relations=(relation,),
        database_fingerprint=db.database_fingerprint,
    )
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert isinstance(result, MappingPlanValidationResult)
    assert result.decision is (
        ValidationDecision.REJECTED if code else ValidationDecision.ACCEPTED
    )
    assert (result.validated_plan is None) is (code is not None)
    assert {i.code for i in result.issues} == ({code} if code else set())
