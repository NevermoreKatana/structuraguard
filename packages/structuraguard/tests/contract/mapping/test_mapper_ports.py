"""Один contract suite для всех implementations CandidateMapper."""

import asyncio

import pytest
from tests.fakes.mapping import catalog, column, profile, refs, table

from structuraguard.contracts import DeterministicMappingResult, MappingScope
from structuraguard.mapping import DeterministicMapper
from structuraguard.ports import CandidateMapper

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("implementation", [DeterministicMapper])
async def test_mapper_contract_roundtrip_and_concurrent_reuse(
    implementation: type[DeterministicMapper],
) -> None:
    mapper: CandidateMapper = implementation()
    assert isinstance(mapper, CandidateMapper)
    data = await profile()
    db = catalog(table("customers", column("email")))
    scope = MappingScope(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        allow=refs(db),
    )
    first, second = await asyncio.gather(
        mapper.rank(data, db, scope=scope), mapper.rank(data, db, scope=scope)
    )
    assert first == second
    assert (
        DeterministicMappingResult.model_validate_json(first.model_dump_json()) == first
    )
    assert first.safe_summary().candidate_count == 1
    empty = await mapper.rank(await profile({}), db, scope=scope)
    assert empty.fields == ()
