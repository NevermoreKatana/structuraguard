"""M14 review: отказ sandbox закрывает общий run до следующего side effect."""

from uuid import UUID

import pytest
from tests.security.sandbox.test_m14_runner_boundary import (
    Backend,
    collect,
    fixture_process,
    spec,
)

from structuraguard.contracts.sandbox import SandboxPolicy
from structuraguard.contracts.security import SecurityPolicy
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.parsers.runners import SandboxParserRunner
from structuraguard.security import SecuritySession


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["probe", "provenance", "cleanup"])
async def test_runner_failure_closes_shared_run_and_records_safe_event(
    failure: str,
) -> None:
    process = await fixture_process()
    if failure == "probe":
        process.probe_frame = b'{"raw":"restricted-probe-canary"}'
    elif failure == "provenance":
        process.frames[0] = process.frames[0].replace(b"source-1", b"source-2")
    else:
        process.cleanup_ok = False
    run = SecuritySession(SecurityPolicy(allowed_formats=("txt",)), run_id=UUID(int=1))
    value = SandboxParserRunner(
        spec(),
        policy=SandboxPolicy(allowed_specs=(spec().fingerprint,)),
        session=run,
        backend=Backend(process),
        run_id=run.run_id,
    )
    with pytest.raises(SecurityPolicyError):
        await collect(value)
    assert process.closed
    with pytest.raises(SecurityPolicyError, match="SECURITY_RUN_CLOSED"):
        run.before_query()
    assert len(run.events) == 1 and run.events[0].outcome == "failed"
    assert "restricted-probe-canary" not in run.events[0].canonical_json()
