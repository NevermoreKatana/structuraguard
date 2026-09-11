"""Повторяемость решения отделена от lineage конкретного профиля и options."""

import pytest
from tests.fakes.mapping import (
    catalog,
    column,
    profile,
    rehash_profile,
    scope_for,
    table,
)
from tests.fakes.profiling import normalized_stream

from structuraguard.contracts import MappingCandidate
from structuraguard.contracts.common import StringScalar
from structuraguard.contracts.deterministic_mapping import (
    DeterministicMappingOptions,
    DeterministicMappingResult,
)
from structuraguard.contracts.profiling import ExamplePolicy, NormalizedProfilingOptions
from structuraguard.contracts.semantic_catalog import (
    DatabaseSemanticCatalog,
    SemanticColumn,
    SemanticTable,
)
from structuraguard.mapping import DeterministicMapper
from structuraguard.profiling import NormalizedDataProfiler

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "change", ["options", "scope", "semantic", "profile", "manifest", "content"]
)
async def test_changed_bindings_change_ids_without_losing_legacy_contract(
    change: str,
) -> None:
    data = await profile()
    db = catalog(
        table("customers", column("email")), table("other", column("unrelated"))
    )
    scope = scope_for(db)
    semantic = DatabaseSemanticCatalog(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        database_fingerprint=db.database_fingerprint,
    )
    baseline = await DeterministicMapper().rank(
        data, db, scope=scope, semantic_catalog=semantic
    )
    options = DeterministicMappingOptions(top_k=1 if change == "options" else 5)
    if change == "scope":
        scope = scope.model_copy(update={"allow": (scope.allow[0],)})
    elif change == "semantic":
        semantic = semantic.model_copy(
            update={
                "tables": (
                    SemanticTable(
                        schema_name="public",
                        table_name="customers",
                        description="Локальная аннотация",
                    ),
                )
            }
        )
    elif change in {"manifest", "content"}:
        key = (
            "normalized_manifest_fingerprint"
            if change == "manifest"
            else "normalized_data_fingerprint"
        )
        data = rehash_profile(data, **{key: "sha256:" + "f" * 64})
    elif change == "profile":
        data = rehash_profile(
            data, fields=(data.fields[0].model_copy(update={"examples": ()}),)
        )
    actual = await DeterministicMapper(options).rank(
        data, db, scope=scope, semantic_catalog=semantic
    )
    candidate = actual.fields[0].candidates[0]
    assert candidate.candidate_id != baseline.fields[0].candidates[0].candidate_id
    assert candidate.target == baseline.fields[0].candidates[0].target
    assert candidate.confidence == baseline.fields[0].candidates[0].confidence
    assert (
        MappingCandidate.model_validate_json(candidate.model_dump_json()) == candidate
    )
    assert type(actual) is DeterministicMappingResult
    assert actual.algorithm == options.algorithm
    assert (
        actual.normalization_version
        and actual.dictionary_version
        and actual.unicode_version
    )


async def test_rebatching_and_sample_policy_preserve_scores_but_rebind_ids() -> None:
    rows = [{"email": StringScalar(value=f"person{i}@example.org")} for i in range(30)]
    first = await NormalizedDataProfiler().profile(
        normalized_stream(rows, batch_size=5)
    )
    second = await NormalizedDataProfiler(
        NormalizedProfilingOptions(seed="other", examples=ExamplePolicy.OMIT)
    ).profile(normalized_stream(rows, batch_size=11))
    assert first.normalized_data_fingerprint == second.normalized_data_fingerprint
    assert (
        first.normalized_manifest_fingerprint != second.normalized_manifest_fingerprint
    )
    assert first.fields[0].examples and not second.fields[0].examples
    db = catalog(table("customers", column("email")))
    mapper = DeterministicMapper()
    left = await mapper.rank(first, db, scope=scope_for(db))
    right = await mapper.rank(second, db, scope=scope_for(db))
    assert (
        left.fields[0].candidates[0].confidence
        == right.fields[0].candidates[0].confidence
    )
    assert (
        left.fields[0].explanations[0].signals
        == right.fields[0].explanations[0].signals
    )
    assert left.fields[0].status == right.fields[0].status
    assert (
        left.fields[0].candidates[0].candidate_id
        != right.fields[0].candidates[0].candidate_id
    )


async def test_unordered_metadata_preserve_ranking_and_ids() -> None:
    data = await profile()
    tables = tuple(
        table(name, column("email"), column("unrelated", position=1), schema=schema)
        for schema in ("crm", "archive")
        for name in ("customers", "suppliers")
    )
    first = catalog(*tables)
    second = catalog(
        *(
            t.model_copy(update={"columns": tuple(reversed(t.columns))})
            for t in reversed(tables)
        )
    )
    scope = scope_for(first)

    def annotations(reverse: bool) -> DatabaseSemanticCatalog:
        entries = tuple(
            SemanticTable(
                schema_name=t.schema_name,
                table_name=t.name,
                aliases=("Покупатели", "Customers")
                if reverse
                else ("Customers", "Покупатели"),
                columns=(
                    SemanticColumn(
                        column_name="email",
                        aliases=("почта", "Email") if reverse else ("Email", "почта"),
                    ),
                ),
            )
            for t in (tuple(reversed(tables)) if reverse else tables)
        )
        return DatabaseSemanticCatalog(
            target_id=first.target_id,
            target_policy_fingerprint=first.target_policy_fingerprint,
            database_fingerprint=first.database_fingerprint,
            tables=entries,
        )

    mapper = DeterministicMapper()
    a = await mapper.rank(data, first, scope=scope, semantic_catalog=annotations(False))
    b = await mapper.rank(data, second, scope=scope, semantic_catalog=annotations(True))
    assert a == b
    assert a.fields[0].ambiguous
    assert a.fields[0].candidates[0].target.table_id == "archive.customers"
