"""Независимый oracle для scope veto и стабильности M11 diagnostics."""

import asyncio
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.fakes.mapping import catalog, column, table
from tests.fakes.mapping_validation import case, replan

from structuraguard.contracts.common import ValidationDecision
from structuraguard.contracts.mapping import MappingPlanValidationResult
from structuraguard.contracts.mapping_rules import MappingIssueLocation
from structuraguard.contracts.mapping_validation import MappingValidationOptions
from structuraguard.mapping import MappingPlanValidator
from structuraguard.mapping._validation_report import Issues


@settings(max_examples=20, deadline=None)
@given(
    schema_order=st.permutations((0, 1)),
    table_order=st.permutations((0, 1)),
    column_order=st.permutations((0, 1)),
    allow_order=st.permutations((0, 1, 2)),
)
def test_catalog_and_policy_permutations_preserve_full_evidence(
    schema_order: list[int],
    table_order: list[int],
    column_order: list[int],
    allow_order: list[int],
) -> None:
    async def run() -> None:
        plan, manifest, db, policy, profile = await case()
        selected = db.schemas[0].tables[0]
        db = catalog(
            selected,
            table("other", column("x")),
            table("remote", column("x"), schema="extra"),
        )
        plan = replan(plan, database_fingerprint=db.database_fingerprint)
        allowed = (("public", "orders"), ("public", "other"), ("extra", "remote"))
        policy = policy.model_copy(
            update={
                "allow_schemas": ("public", "extra"),
                "allow_tables": allowed,
                "deny_tables": (("public", "orders"),),
            }
        )
        baseline = await MappingPlanValidator(policy=policy).validate(
            plan, manifest, db, profile=profile
        )
        public = next(s for s in db.schemas if s.name == "public")
        tables = (
            selected.model_copy(
                update={"columns": tuple(selected.columns[i] for i in column_order)}
            ),
            public.tables[1],
        )
        public = public.model_copy(
            update={"tables": tuple(tables[i] for i in table_order)}
        )
        schemas = (public, next(s for s in db.schemas if s.name == "extra"))
        changed_db = db.model_copy(
            update={"schemas": tuple(schemas[i] for i in schema_order)}
        )
        changed_policy = policy.model_copy(
            update={
                "allow_schemas": tuple(reversed(policy.allow_schemas)),
                "allow_tables": tuple(allowed[i] for i in allow_order),
                "scope": policy.scope.model_copy(
                    update={"allow": tuple(reversed(policy.scope.allow))}
                ),
            }
        )
        changed = await MappingPlanValidator(policy=changed_policy).validate(
            plan, manifest, changed_db, profile=profile
        )
        assert isinstance(baseline, MappingPlanValidationResult)
        assert isinstance(changed, MappingPlanValidationResult)
        assert baseline.decision is changed.decision is ValidationDecision.REJECTED
        assert baseline.evidence == changed.evidence
        assert baseline.issues == changed.issues
        assert baseline.validation_fingerprint == changed.validation_fingerprint

    asyncio.run(run())


@settings(max_examples=25, deadline=None)
@given(
    threshold=st.decimals(min_value="0.90", max_value="1", places=3),
    remove_allow=st.booleans(),
    readonly=st.booleans(),
)
def test_scope_threshold_and_writability_restrictions_never_remove_a_veto(
    threshold: Decimal, remove_allow: bool, readonly: bool
) -> None:
    async def run() -> None:
        plan, manifest, db, policy, profile = await case()
        plan = replan(plan, confidence=Decimal("0.89"))
        policy = policy.model_copy(update={"confidence_threshold": threshold})
        if remove_allow:
            policy = policy.model_copy(
                update={"scope": policy.scope.model_copy(update={"allow": ()})}
            )
        if readonly:
            target = db.schemas[0].tables[0]
            db = catalog(
                target.model_copy(
                    update={
                        "columns": tuple(
                            c.model_copy(update={"writable": False})
                            for c in target.columns
                        )
                    }
                )
            )
        assert db.database_fingerprint == plan.database_fingerprint
        result = await MappingPlanValidator(policy=policy).validate(
            plan, manifest, db, profile=profile
        )
        assert isinstance(result, MappingPlanValidationResult)
        assert result.decision is ValidationDecision.REJECTED
        assert result.validated_plan is None
        codes = {i.code for i in result.issues}
        assert "MAPPING_CONFIDENCE_BELOW_THRESHOLD" in codes
        if remove_allow:
            assert "MAPPING_COLUMN_DENIED" in codes
        if readonly:
            assert "MAPPING_COLUMN_NOT_WRITABLE" in codes

    asyncio.run(run())


@given(order=st.permutations((0, 1, 2)))
def test_independent_check_completion_order_does_not_change_report(
    order: list[int],
) -> None:
    expected = (
        ("MAPPING_COLUMN_DENIED", MappingIssueLocation(section="mappings", index=1)),
        (
            "MAPPING_TYPE_INCOMPATIBLE",
            MappingIssueLocation(section="mappings", index=0),
        ),
        ("MAPPING_CONFIDENCE_BELOW_THRESHOLD", MappingIssueLocation()),
    )
    report = Issues(MappingValidationOptions())
    for index in order:
        report.add(*expected[index])
    issues, locations = report.ordered()
    assert (
        tuple((issue.code, loc) for issue, loc in zip(issues, locations, strict=True))
        == expected
    )
    assert report.decision is ValidationDecision.REJECTED


@settings(max_examples=30, deadline=None)
@given(
    deny_schema=st.booleans(),
    deny_table=st.booleans(),
    deny_column=st.booleans(),
    low=st.booleans(),
)
def test_all_independent_vetoes_are_present(
    deny_schema: bool, deny_table: bool, deny_column: bool, low: bool
) -> None:
    async def run() -> None:
        plan, manifest, db, policy, profile = await case()
        expected = set()
        if deny_schema:
            policy = policy.model_copy(update={"deny_schemas": ("public",)})
            expected.add("MAPPING_SCHEMA_DENIED")
        if deny_table:
            policy = policy.model_copy(update={"deny_tables": (("public", "orders"),)})
            expected.add("MAPPING_TABLE_DENIED")
        if deny_column:
            policy = policy.model_copy(
                update={
                    "scope": policy.scope.model_copy(
                        update={"deny": (plan.mappings[1].target,)}
                    )
                }
            )
            expected.add("MAPPING_COLUMN_DENIED")
        if low:
            plan = replan(plan, confidence=Decimal("0.89"))
            expected.add("MAPPING_CONFIDENCE_BELOW_THRESHOLD")
        first = await MappingPlanValidator(policy=policy).validate(
            plan, manifest, db, profile=profile
        )
        second = await MappingPlanValidator(policy=policy).validate(
            plan, manifest, db, profile=profile
        )
        assert {i.code for i in first.issues} == expected
        assert first.issues == second.issues
        assert (first.decision is ValidationDecision.ACCEPTED) is (not expected)

    asyncio.run(run())
