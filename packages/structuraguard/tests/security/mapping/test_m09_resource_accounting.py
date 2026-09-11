"""Attacker-controlled FK/identity fan-out обязан расходовать operation budget."""

import pytest
from tests.fakes.mapping import catalog, column, profile, scope_for, table

from structuraguard.contracts.database import CatalogColumnRef, ForeignKeyCatalog
from structuraguard.contracts.deterministic_mapping import DeterministicMappingOptions
from structuraguard.contracts.semantic_catalog import (
    DatabaseSemanticCatalog,
    SemanticTable,
)
from structuraguard.exceptions import MappingError
from structuraguard.mapping import DeterministicMapper

pytestmark = pytest.mark.anyio


async def test_identity_key_fanout_cannot_bypass_operations_budget() -> None:
    names = ("email", *(f"column_{i}" for i in range(39)))
    db = catalog(
        table("customers", *(column(name, position=i) for i, name in enumerate(names)))
    )
    semantic = DatabaseSemanticCatalog(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        database_fingerprint=db.database_fingerprint,
        tables=(
            SemanticTable(
                schema_name="public",
                table_name="customers",
                identity_keys=tuple((name,) for name in names),
            ),
        ),
    )
    # 80 lexical pair visits помещаются; дополнительные 40 identity keys — нет.
    with pytest.raises(MappingError, match="MAPPING_LIMIT_EXCEEDED") as error:
        await DeterministicMapper(DeterministicMappingOptions(max_operations=100)).rank(
            await profile(),
            db,
            scope=scope_for(db),
            semantic_catalog=semantic,
        )
    assert error.value.details == {"reason": "operations"}


async def test_out_of_scope_fk_fanout_cannot_bypass_operations_budget() -> None:
    parent = table("parents", *(column(f"key_{i}", position=i) for i in range(40)))
    child = table(
        "children",
        column("email"),
        foreign_keys=tuple(
            ForeignKeyCatalog(
                foreign_key_id=f"fk_{i}",
                column_ids=("email",),
                referenced_table_id=parent.table_id,
                referenced_column_ids=(f"key_{i}",),
            )
            for i in range(40)
        ),
    )
    db = catalog(parent, child)
    scope = scope_for(db).model_copy(
        update={
            "allow": (CatalogColumnRef(table_id=child.table_id, column_id="email"),)
        }
    )
    # Два pair visits не дают права бесплатно обойти 40 ограничений FK.
    with pytest.raises(MappingError, match="MAPPING_LIMIT_EXCEEDED") as error:
        await DeterministicMapper(DeterministicMappingOptions(max_operations=10)).rank(
            await profile(), db, scope=scope
        )
    assert error.value.details == {"reason": "operations"}
