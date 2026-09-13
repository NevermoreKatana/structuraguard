"""Portable contract suite без БД и сетевых зависимостей."""

import pytest
from tests.contract.stores._suite import StagingContract
from tests.fakes.staging import StagingCase, StagingClock, staging_case

from structuraguard.contracts.staging import StagingRetentionPolicy
from structuraguard.stores import MemoryStagingStore

pytestmark = pytest.mark.anyio


@pytest.fixture
def clock() -> StagingClock:
    return StagingClock()


@pytest.fixture
def policy() -> StagingRetentionPolicy:
    return StagingRetentionPolicy()


@pytest.fixture
async def case(clock: StagingClock, policy: StagingRetentionPolicy) -> StagingCase:
    return await staging_case(clock, policy)


@pytest.fixture
def store(clock: StagingClock, policy: StagingRetentionPolicy) -> MemoryStagingStore:
    return MemoryStagingStore(target_id="main", retention=policy, clock=clock)


class TestMemoryStaging(StagingContract):
    pass


@pytest.fixture
def cleaner(store: MemoryStagingStore) -> MemoryStagingStore:
    return store
