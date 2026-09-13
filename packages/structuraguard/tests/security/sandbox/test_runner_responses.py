"""D5: недоверенные IPC responses закрывают execution и освобождают worker."""

import json
import traceback

import pytest
from tests.security.sandbox.test_m14_runner_boundary import (
    Backend,
    Process,
    collect,
    fixture_process,
    runner,
    spec,
)

from structuraguard.contracts.sandbox import SandboxExit, SandboxPolicy
from structuraguard.exceptions import SecurityPolicyError


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["probe", "frame", "exit"])
async def test_malformed_response_is_secret_safe_and_worker_closed(phase: str) -> None:
    process = await fixture_process()
    if phase == "probe":
        process.probe_frame = b'{"raw":"restricted-response-canary"}'
    elif phase == "frame":
        process.frames[0] = b'{"raw":"restricted-response-canary"}'
    else:

        class ForgedExitProcess(Process):
            async def wait(self) -> SandboxExit:
                return SandboxExit.model_construct(
                    status="restricted-response-canary", cleanup_complete=True
                )

        process = ForgedExitProcess(process.probe_frame, process.frames)
    with pytest.raises(SecurityPolicyError) as caught:
        await collect(runner(Backend(process)))
    assert process.closed
    assert "restricted-response-canary" not in "".join(
        traceback.format_exception(caught.value)
    )


def json_depth(value: object) -> int:
    if isinstance(value, dict):
        return 1 + max((json_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((json_depth(v) for v in value), default=0)
    return 0


@pytest.mark.anyio
@pytest.mark.parametrize("extra", [0, 1])
async def test_ipc_json_depth_exact_and_one_over(extra: int) -> None:
    process = await fixture_process()
    depth = max(
        json_depth(json.loads(frame))
        for frame in [process.probe_frame, *process.frames]
    )
    policy = SandboxPolicy(
        allowed_specs=(spec().fingerprint,), max_json_depth=depth - extra
    )
    if extra:
        with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
            await collect(runner(Backend(process), policy))
    else:
        assert await collect(runner(Backend(process), policy))
    assert process.closed
