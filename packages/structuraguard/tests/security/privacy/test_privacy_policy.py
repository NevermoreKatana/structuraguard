"""Безопасные отказы, precision настройки и границы output/store policy."""

from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.fakes.privacy import READER, RUN, WRITER, Clock, protector, store_for

from structuraguard.contracts.common import DataClassification as C
from structuraguard.contracts.privacy import (
    CategoryRule,
    CustomPattern,
    DetectionPolicy,
    PlaceholderMapPolicy,
    ScanLimits,
    SensitiveCategory,
)
from structuraguard.domain.pii_patterns import card_like
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.security import ContentProtector, EncryptedMemoryPlaceholderStore
from structuraguard.security.redaction import restore_fields


@pytest.mark.anyio
@pytest.mark.parametrize(
    "label", ["email", "phone", "ИНН", "СНИЛС", "passport", "cardNumber", "accessToken"]
)
async def test_sensitive_labels_mask_obfuscated_values(label: str) -> None:
    output = await ContentProtector(DetectionPolicy()).redact_fields(
        {label: "not-a-recognizable-pattern"}, run_id=RUN
    )
    assert output.fields[0].value.startswith("[SGR:")
    assert output.classification in {C.CONFIDENTIAL, C.RESTRICTED}


@pytest.mark.anyio
async def test_configured_precision_is_explicit_and_custom_fields_are_additive() -> (
    None
):
    detector = ContentProtector(
        DetectionPolicy(
            inn_validation="checksum",
            phone_mode="international",
            custom_secret_fields=("internalCredential",),
            categories=(
                CategoryRule(
                    category=SensitiveCategory.EMAIL, classification=C.RESTRICTED
                ),
            ),
        )
    )
    report = await detector.classify("1234567890")
    assert not report.findings
    assert (await detector.classify("a@b.test")).classification is C.RESTRICTED
    result = await detector.redact_fields(
        {"internalCredential": "synthetic"}, run_id=RUN
    )
    assert "synthetic" not in result.fields[0].value


@pytest.mark.anyio
async def test_uri_credentials_are_masked_without_network_or_parsing_side_effects() -> (
    None
):
    output = await ContentProtector(DetectionPolicy()).redact(
        "connection postgresql://user:secret-canary@localhost/db", run_id=RUN
    )
    assert "secret-canary" not in output.text
    assert output.classification is C.RESTRICTED


@pytest.mark.anyio
@pytest.mark.parametrize("extra", [0, 1])
async def test_redaction_expansion_and_restore_limits(extra: int) -> None:
    detector = ContentProtector(
        DetectionPolicy(limits=ScanLimits(max_output_chars=43 - extra))
    )
    if extra:
        with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
            await detector.redact("a@b.test", run_id=RUN)
    else:
        assert len((await detector.redact("a@b.test", run_id=RUN)).text) == 43
    store = store_for()
    output = await protector().redact(
        "a@b.test", run_id=RUN, store=store, principal=WRITER
    )
    if extra:
        with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
            await restore_fields(
                output, store=store, run_id=RUN, principal=READER, max_chars=7
            )
    else:
        assert (
            await restore_fields(
                output, store=store, run_id=RUN, principal=READER, max_chars=8
            )
        )[0].value == "a@b.test"


@pytest.mark.anyio
@pytest.mark.parametrize("tick", [0.999, 1.0, 1.001])
async def test_scan_deadline_never_returns_partial_clean(tick: float) -> None:
    clock = Clock()
    detector = ContentProtector(DetectionPolicy(), monotonic=clock.monotonic)

    async def chunks() -> AsyncIterator[str]:
        yield "hello"
        clock.seconds = tick
        yield "world"

    if tick < 1:
        assert (await detector.classify_chunks(chunks())).complete
    else:
        with pytest.raises(SecurityPolicyError, match="PROCESSING_TIMEOUT"):
            await detector.classify_chunks(chunks())


@pytest.mark.anyio
@pytest.mark.parametrize(
    "value", [b"secret-canary", {"nested": "secret-canary"}, 42, "\ud800"]
)
async def test_unsupported_input_never_stringifies_raw_data(value: object) -> None:
    with pytest.raises(SecurityPolicyError) as caught:
        await ContentProtector(DetectionPolicy()).classify(cast(str, value))
    assert "secret-canary" not in str(caught.value)
    assert "secret-canary" not in repr(caught.value)


@pytest.mark.anyio
async def test_default_map_policy_denies_write_and_foreign_run_is_denied() -> None:
    for policy in (
        PlaceholderMapPolicy(),
        PlaceholderMapPolicy(runs=(str(UUID(int=99)),), writers=(str(WRITER),)),
    ):
        store = EncryptedMemoryPlaceholderStore(key=b"\x33" * 32, policy=policy)
        with pytest.raises(SecurityPolicyError, match="SECURITY_MAP_ACCESS_DENIED"):
            await protector().redact(
                "a@b.test", run_id=RUN, store=store, principal=WRITER
            )
        assert not store._records


@pytest.mark.anyio
async def test_map_clock_rollback_denies_restore() -> None:
    clock = Clock()
    store = store_for(clock)
    output = await protector().redact(
        "a@b.test", run_id=RUN, store=store, principal=WRITER
    )
    clock.seconds = 10
    await restore_fields(output, store=store, run_id=RUN, principal=READER)
    clock.seconds = 9
    with pytest.raises(SecurityPolicyError, match="SECURITY_MAP_ACCESS_DENIED"):
        await restore_fields(output, store=store, run_id=RUN, principal=READER)


@pytest.mark.anyio
@pytest.mark.parametrize("extra", [0, 1])
async def test_unique_map_entries_boundary(extra: int) -> None:
    store = store_for(max_entries=2 - extra)
    if extra:
        with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
            await protector().redact(
                "a@b.test c@d.test", run_id=RUN, store=store, principal=WRITER
            )
        assert not store._records
    else:
        output = await protector().redact(
            "a@b.test c@d.test", run_id=RUN, store=store, principal=WRITER
        )
        assert output.handle is not None


@pytest.mark.anyio
@given(st.sampled_from(list(C)), st.text(alphabet="ab _", max_size=40))
async def test_redaction_cannot_lower_caller_classification(
    minimum: C, suffix: str
) -> None:
    output = await ContentProtector(DetectionPolicy()).redact(
        "a@b.test " + suffix, run_id=RUN, minimum_classification=minimum
    )
    levels = list(C)
    assert levels.index(output.classification) >= levels.index(minimum)
    assert "a@b.test" not in output.text
    assert output.handle is None


@given(st.integers(min_value=1, max_value=9))
def test_single_digit_card_mutation_fails_luhn(delta: int) -> None:
    assert card_like("4111111111111111")
    assert not card_like("411111111111111" + str((1 + delta) % 10))


def test_forged_pattern_policy_is_rejected_without_secret_echo() -> None:
    forged = CustomPattern.model_construct(pattern="secret-canary" * 30)
    policy = DetectionPolicy.model_construct(custom_patterns=(forged,))
    with pytest.raises(SecurityPolicyError, match="SECURITY_POLICY_INVALID") as caught:
        ContentProtector(policy)
    assert "secret-canary" not in str(caught.value)


@pytest.mark.anyio
async def test_category_override_also_applies_to_custom_findings() -> None:
    pattern = CustomPattern(pattern="CLIENT-[0-9]{4}", classification=C.CONFIDENTIAL)
    detector = ContentProtector(DetectionPolicy(custom_patterns=(pattern,)))
    assert (await detector.classify("CLIENT-1234")).classification is C.CONFIDENTIAL
    stronger = ContentProtector(
        DetectionPolicy(
            custom_patterns=(pattern,),
            categories=(
                CategoryRule(
                    category=SensitiveCategory.CUSTOM, classification=C.RESTRICTED
                ),
            ),
        )
    )
    assert (await stronger.classify("CLIENT-1234")).classification is C.RESTRICTED
