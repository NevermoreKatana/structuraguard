"""M12 AC-04: неизвестная UNIQUE/FK semantics не превращается в pass/read."""

import pytest
from tests.fakes.mapping import catalog, column, table
from tests.unit.validation.test_db_constraints import data, integer, policy

from structuraguard.contracts.constraint_validation import (
    ConstraintReadRequest,
    ConstraintReadResult,
)
from structuraguard.contracts.database import (
    DatabaseCatalog,
    ForeignKeyCatalog,
    ForeignKeyInspectionMetadata,
    TableCatalog,
)
from structuraguard.validation import DatabaseConstraintValidator


class ForbiddenReader:
    async def read(
        self, request: ConstraintReadRequest, *, catalog: DatabaseCatalog
    ) -> ConstraintReadResult:
        pytest.fail("Недоказанная key semantics не разрешает reader precheck")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case", ["partial", "expression", "collation", "operator_class", "deferred"]
)
async def test_unsupported_unique_semantics_remain_unverified(case: str) -> None:
    text = case == "collation"
    payload = table("items", column("key", "text" if text else "integer")).model_dump(
        mode="python"
    )
    key: dict[str, object] = {"column_id": "key"}
    index: dict[str, object] = {
        "index_id": "unique_key",
        "name": "unique_key",
        "keys": (key,),
        "unique": True,
        "origin": "index",
    }
    if case == "partial":
        index["predicate"] = "key > 0"
    elif case == "expression":
        key.clear()
        key["expression"] = "lower(key)"
    elif case == "collation":
        key["collation"] = "und-x-icu"
    elif case == "operator_class":
        key["operator_class"] = "custom_equals"
    else:
        payload["unique_constraints"] = (("key",),)
        payload["inspection"]["constraints"] = (
            {
                "name": "uq",
                "kind": "unique",
                "column_ids": ("key",),
                "deferrable": True,
            },
        )
    payload["inspection"]["indexes"] = (index,)
    db = catalog(TableCatalog.model_validate(payload))
    result = await DatabaseConstraintValidator(
        policy(db), reader=ForbiddenReader()
    ).validate(
        data(
            (
                "public.items",
                {"key": {"kind": "string", "value": "ß"} if text else integer(1)},
            )
        ),
        catalog=db,
    )
    assert not result.accepted
    assert {issue.code for issue in result.issues} == {"DB_CONSTRAINT_UNVERIFIED"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "metadata",
    [
        None,
        ForeignKeyInspectionMetadata(
            on_update="NO ACTION",
            on_delete="NO ACTION",
            match="SIMPLE",
            deferrable=True,
        ),
        ForeignKeyInspectionMetadata(
            on_update="NO ACTION", on_delete="NO ACTION", match="PARTIAL"
        ),
    ],
)
async def test_unknown_or_deferred_fk_cannot_borrow_an_incoming_parent(
    metadata: ForeignKeyInspectionMetadata | None,
) -> None:
    parent_payload = table("parent", column("id", "integer")).model_dump(mode="python")
    parent_payload["unique_constraints"] = (("id",),)
    parent = TableCatalog.model_validate(parent_payload)
    child = table(
        "child",
        column("pid", "integer"),
        foreign_keys=(
            ForeignKeyCatalog(
                foreign_key_id="fk",
                column_ids=("pid",),
                referenced_table_id=parent.table_id,
                referenced_column_ids=("id",),
                inspection=metadata,
            ),
        ),
    )
    db = catalog(parent, child)
    result = await DatabaseConstraintValidator(policy(db)).validate(
        data(
            (child.table_id, {"pid": integer(1)}), (parent.table_id, {"id": integer(1)})
        ),
        catalog=db,
    )
    assert not result.accepted
    assert any(
        issue.code == "DB_CONSTRAINT_UNVERIFIED"
        and issue.collection_id == child.table_id
        for issue in result.issues
    )
