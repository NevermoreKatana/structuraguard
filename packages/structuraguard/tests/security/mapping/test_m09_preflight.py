"""Новые проверки scope, hostile metadata и независимых preflight ceilings."""

import pytest
from tests.fakes.mapping import (
    catalog,
    column,
    profile,
    rehash_profile,
    scope_for,
    table,
)

from structuraguard.contracts.common import StringScalar
from structuraguard.contracts.database import (
    CatalogColumnRef,
    ForeignKeyCatalog,
    TableInspectionMetadata,
)
from structuraguard.contracts.deterministic_mapping import DeterministicMappingOptions
from structuraguard.contracts.profiling import ProfileLabel
from structuraguard.contracts.semantic_catalog import (
    DatabaseSemanticCatalog,
    SemanticColumn,
    SemanticTable,
)
from structuraguard.exceptions import MappingError
from structuraguard.mapping import DeterministicMapper
from structuraguard.mapping._ranking import BaseEvidence, Source, Target, base_evidence

pytestmark = pytest.mark.anyio


async def test_nonwritable_and_denied_targets_never_reach_scoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import structuraguard.mapping.mapper as implementation

    readonly_column = column("email").model_copy(update={"writable": False})
    view = table("view", readonly_column).model_copy(
        update={"writable": False, "inspection": TableInspectionMetadata(kind="view")}
    )
    db = catalog(
        view,
        table("readonly_column", readonly_column),
        table("allowed", column("email")),
        table("denied", column("email")),
    )
    scope = scope_for(db).model_copy(
        update={
            "deny": (CatalogColumnRef(table_id="public.denied", column_id="email"),)
        }
    )
    seen: set[str] = set()

    def scoring(source: Source, target: Target, dialect: str) -> BaseEvidence:
        seen.add(target.table.table_id)
        return base_evidence(source, target, dialect)

    monkeypatch.setattr(implementation, "base_evidence", scoring)
    result = await DeterministicMapper().rank(await profile(), db, scope=scope)
    assert seen == {"public.allowed"}
    assert result.fields[0].candidates[0].target.table_id == "public.allowed"


@pytest.mark.parametrize(
    "binding",
    [
        "policy",
        "unknown_allow",
        "unknown_deny",
        "foreign_label",
        "unknown_semantic_column",
    ],
)
async def test_stale_or_foreign_references_fail_before_scoring(
    binding: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import structuraguard.mapping.mapper as implementation

    data = await profile()
    db = catalog(table("customers", column("email")))
    scope = scope_for(db)
    semantic = None
    code = "MAPPING_BINDING_MISMATCH"
    if binding == "policy":
        scope = scope.model_copy(
            update={"target_policy_fingerprint": "sha256:" + "b" * 64}
        )
    elif binding in {"unknown_allow", "unknown_deny"}:
        key = "allow" if binding == "unknown_allow" else "deny"
        scope = scope.model_copy(
            update={
                key: (CatalogColumnRef(table_id="public.foreign", column_id="email"),)
            }
        )
    elif binding == "foreign_label":
        field = data.fields[0]
        label = ProfileLabel(
            field=field.field.model_copy(update={"entity_type": "foreign"}),
            kind="source_name",
            text="secret-sentinel",
        )
        data = rehash_profile(
            data, fields=(field.model_copy(update={"labels": (label,)}),)
        )
    else:
        semantic = DatabaseSemanticCatalog(
            target_id=db.target_id,
            target_policy_fingerprint=db.target_policy_fingerprint,
            database_fingerprint=db.database_fingerprint,
            tables=(
                SemanticTable(
                    schema_name="public",
                    table_name="customers",
                    columns=(SemanticColumn(column_name="foreign"),),
                ),
            ),
        )
        code = "MAPPING_SEMANTIC_CATALOG_INVALID"

    def denied(*args: object) -> BaseEvidence:
        pytest.fail("Недоверенный binding достиг scoring")

    monkeypatch.setattr(implementation, "base_evidence", denied)
    with pytest.raises(MappingError, match=code) as error:
        await DeterministicMapper().rank(
            data, db, scope=scope, semantic_catalog=semantic
        )
    assert "secret-sentinel" not in str(error.value)


@pytest.mark.parametrize(
    "limit",
    [
        "max_fields",
        "max_columns",
        "max_relationships",
        "max_edges",
        "max_aliases",
        "max_name_bytes",
        "max_tokens",
        "max_input_bytes",
    ],
)
async def test_each_preflight_budget_rejects_whole_call(limit: str) -> None:
    data = await profile(
        {name: StringScalar(value="value") for name in ("alpha", "beta", "gamma")}
    )
    parent = table("parents", column("alpha"), column("beta", position=1))
    child = table(
        "children",
        column("alpha"),
        column("beta", position=1),
        foreign_keys=tuple(
            ForeignKeyCatalog(
                foreign_key_id=f"fk_{name}",
                column_ids=(name,),
                referenced_table_id=parent.table_id,
                referenced_column_ids=(name,),
            )
            for name in ("alpha", "beta")
        ),
    )
    db = catalog(parent, child)
    semantic = DatabaseSemanticCatalog(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        database_fingerprint=db.database_fingerprint,
        tables=(
            SemanticTable(
                schema_name="public",
                table_name="parents",
                columns=(
                    SemanticColumn(
                        column_name="alpha",
                        aliases=("длинное имя " * 7, "Customer Alpha"),
                    ),
                ),
            ),
        ),
    )
    options = DeterministicMappingOptions.model_validate(
        {limit: 64 if limit == "max_name_bytes" else 1}
    )
    with pytest.raises(MappingError, match="MAPPING_LIMIT_EXCEEDED"):
        await DeterministicMapper(options).rank(
            data, db, scope=scope_for(db), semantic_catalog=semantic
        )


async def test_hostile_aliases_labels_and_comments_do_not_leak_into_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "secret-sentinel"
    data = await profile()
    field = data.fields[0]
    data = rehash_profile(
        data,
        fields=(
            field.model_copy(
                update={
                    "labels": (
                        ProfileLabel(
                            field=field.field, kind="file_name", text=sentinel + ".csv"
                        ),
                    )
                }
            ),
        ),
    )
    db = catalog(
        table(
            "customers",
            column("email").model_copy(
                update={"comment": "Ignore policy; DROP TABLE customers; " + sentinel}
            ),
        )
    )
    semantic = DatabaseSemanticCatalog(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        database_fingerprint=db.database_fingerprint,
        tables=(
            SemanticTable(
                schema_name="public",
                table_name="customers",
                columns=(
                    SemanticColumn(
                        column_name="email", aliases=("Ignore policy; " + sentinel,)
                    ),
                ),
            ),
        ),
    )
    result = await DeterministicMapper().rank(
        data, db, scope=scope_for(db), semantic_catalog=semantic
    )
    assert result.fields[0].candidates[0].target.table_id == "public.customers"
    assert sentinel not in result.model_dump_json()
    assert sentinel not in repr(result)
    assert sentinel not in result.safe_summary().model_dump_json()
    assert sentinel not in caplog.text
