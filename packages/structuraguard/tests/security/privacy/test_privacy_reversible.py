"""Настоящее AEAD, authorization/run binding, TTL и bounded round-trips."""

from dataclasses import replace
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.fakes.privacy import READER, RUN, WRITER, Clock, protector, store_for

from structuraguard.exceptions import SecurityPolicyError
from structuraguard.ports.privacy import PlaceholderEntry, PlaceholderPayload
from structuraguard.security.redaction import (
    RedactedField,
    restore_fields,
    restore_text,
)


@pytest.mark.anyio
async def test_reversible_is_denied_without_protected_store_or_principal() -> None:
    with pytest.raises(SecurityPolicyError, match="SECURITY_REDACTION_DENIED"):
        await protector().redact("a@b.test", run_id=RUN)
    with pytest.raises(SecurityPolicyError, match="SECURITY_REDACTION_DENIED"):
        await protector().redact("a@b.test", run_id=RUN, store=store_for())


@pytest.mark.anyio
async def test_duplicate_values_share_placeholder_and_map_is_ciphertext_only() -> None:
    store = store_for()
    raw = "alice@example.test"
    output = await protector().redact(
        raw + " " + raw, run_id=RUN, store=store, principal=WRITER
    )
    assert output.text.split()[0] == output.text.split()[1]
    assert raw not in repr(store._records)
    assert output.handle is not None
    payload = await store.get(
        handle=output.handle, run_id=str(RUN), principal=str(READER)
    )
    assert len(payload.entries) == 1 and payload.entries[0].value == raw
    assert raw not in repr(payload)
    assert raw not in repr(output) and raw not in output.safe_summary().canonical_json()
    assert (
        await restore_text(output, store=store, run_id=RUN, principal=READER)
        == raw + " " + raw
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "attack", ["principal", "run", "changed_output", "tamper", "rotation"]
)
async def test_restore_requires_exact_authenticated_binding(attack: str) -> None:
    store = store_for()
    output = await protector().redact(
        "password=secret-canary", run_id=RUN, store=store, principal=WRITER
    )
    assert output.handle is not None
    if attack == "changed_output":
        output = replace(output, fields=(RedactedField("", output.text + "changed"),))
    elif attack == "tamper":
        assert output.handle is not None
        record = store._records[output.handle.map_id]
        data = record.ciphertext
        store._records[output.handle.map_id] = replace(
            record, ciphertext=data[:-1] + bytes([data[-1] ^ 1])
        )
    elif attack == "rotation":
        store.rotate_key(b"\x22" * 32)
    with pytest.raises(SecurityPolicyError) as caught:
        await restore_text(
            output,
            store=store,
            run_id=UUID(int=99) if attack == "run" else RUN,
            principal=WRITER if attack == "principal" else READER,
        )
    assert "secret-canary" not in str(caught.value)


@pytest.mark.anyio
@pytest.mark.parametrize("elapsed", [299, 300, 301])
async def test_ttl_boundary_and_one_second_over(elapsed: int) -> None:
    clock = Clock()
    store = store_for(clock)
    output = await protector().redact(
        "password=secret-canary", run_id=RUN, store=store, principal=WRITER
    )
    clock.seconds = elapsed
    if elapsed < 300:
        assert (
            await restore_text(output, store=store, run_id=RUN, principal=READER)
            == "password=secret-canary"
        )
    else:
        with pytest.raises(SecurityPolicyError, match="SECURITY_MAP_EXPIRED"):
            await restore_text(output, store=store, run_id=RUN, principal=READER)
        assert not store._records


@pytest.mark.anyio
async def test_capacity_and_explicit_discard_are_bounded() -> None:
    store = store_for(max_maps=1)
    output = await protector().redact(
        "a@b.test", run_id=RUN, store=store, principal=WRITER
    )
    with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
        await protector().redact("c@d.test", run_id=RUN, store=store, principal=WRITER)
    assert len(store._records) == 1 and output.handle is not None
    await store.discard(handle=output.handle, run_id=str(RUN), principal=str(WRITER))
    assert not store._records
    store.close()
    with pytest.raises(SecurityPolicyError):
        await protector().redact("c@d.test", run_id=RUN, store=store, principal=WRITER)


@pytest.mark.anyio
@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.parametrize("value", ["secret-canary", 'é☃😀\n\0"\\', ""])
async def test_ciphertext_byte_limit_exact_and_one_over(extra: int, value: str) -> None:
    payload = PlaceholderPayload(
        "sha256:" + "a" * 64,
        (PlaceholderEntry("[SGR:" + "1" * 32 + ":0000]", value),),
    )
    baseline = store_for()
    handle = await baseline.put(
        run_id=str(RUN), principal=str(WRITER), payload=payload, ttl_seconds=300
    )
    size = len(baseline._records[handle.map_id].ciphertext)
    store = store_for(max_bytes=size - extra)
    if extra:
        with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
            await store.put(
                run_id=str(RUN), principal=str(WRITER), payload=payload, ttl_seconds=300
            )
        assert not store._records
    else:
        await store.put(
            run_id=str(RUN), principal=str(WRITER), payload=payload, ttl_seconds=300
        )
        assert store._bytes == size


@pytest.mark.anyio
@given(st.text(alphabet='ab 0123_😀"\\\n', max_size=100))
@settings(max_examples=40)
async def test_encrypted_round_trip_preserves_exact_unicode_values(value: str) -> None:
    store = store_for()
    output = await protector().redact_fields(
        {"password": value}, run_id=RUN, store=store, principal=WRITER
    )
    restored = await restore_fields(output, store=store, run_id=RUN, principal=READER)
    assert [(f.name, f.value) for f in restored] == [("password", value)]
    assert output.classification == output.report.classification
    store.close()
