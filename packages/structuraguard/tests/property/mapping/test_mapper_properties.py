"""Перестановки metadata и bounded heap сравниваются с exhaustive oracle."""

import asyncio
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.fakes.mapping import catalog, column, profile, refs, table

from structuraguard.contracts.deterministic_mapping import (
    DeterministicMappingOptions,
    MappingScope,
)
from structuraguard.mapping import DeterministicMapper


@settings(max_examples=15, deadline=None)
@given(
    st.permutations(("customers", "suppliers", "partners", "staff")), st.integers(1, 4)
)
def test_catalog_order_preserves_ranking_ids_and_top_k(
    order: list[str], k: int
) -> None:
    async def run() -> None:
        data = await profile()
        db = catalog(*(table(name, column("email")) for name in order))
        canonical = catalog(*(table(name, column("email")) for name in sorted(order)))
        scope = MappingScope(
            target_id=db.target_id,
            target_policy_fingerprint=db.target_policy_fingerprint,
            allow=refs(db),
        )
        mapper = DeterministicMapper(DeterministicMappingOptions(top_k=k))
        actual = await mapper.rank(data, db, scope=scope)
        expected = await mapper.rank(data, canonical, scope=scope)
        assert actual == expected
        full = await DeterministicMapper(DeterministicMappingOptions(top_k=10)).rank(
            data, db, scope=scope
        )
        assert tuple(c.target for c in actual.fields[0].candidates) == tuple(
            c.target for c in full.fields[0].candidates[:k]
        )
        assert actual.fields[0].gap == Decimal(0)
        assert actual.fields[0].tie_count == 4

    asyncio.run(run())
