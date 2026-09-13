"""Synthetic fixtures: detection, ошибочные совпадения и безопасный отказ."""

from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from structuraguard.contracts.common import DataClassification as C
from structuraguard.contracts.privacy import CustomPattern, DetectionPolicy, ScanLimits
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.security.classification import ContentProtector


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("contact: alice@example.test", "email"),
        ("call +7 (999) 123-45-67", "phone"),
        ("ИНН: 1234567890", "inn"),
        ("СНИЛС: 123-456-789 00", "snils"),
        ("паспорт: 45 12 345678", "passport"),
        ("card 4111 1111 1111 1111", "card"),
        ("sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX", "api_key"),
        ("Bearer abcdefghijklmnopqrstuvwxyz", "token"),
        ('password = "synthetic secret"', "password"),
        (
            "-----BEGIN PRIVATE KEY-----\nSYNTHETIC\n-----END PRIVATE KEY-----",
            "private_key",
        ),
    ],
)
async def test_builtin_detection_and_redaction(text: str, category: str) -> None:
    protector = ContentProtector(DetectionPolicy())
    report = await protector.classify(text)
    assert category in {finding.category.value for finding in report.findings}
    output = await protector.redact(text, run_id=UUID(int=1))
    assert output.text != text
    assert output.classification in {C.CONFIDENTIAL, C.RESTRICTED}
    assert text not in repr(output)
    assert text not in output.safe_summary().canonical_json()


@pytest.mark.anyio
async def test_password_fields_and_sensitive_names_are_scanned() -> None:
    result = await ContentProtector(DetectionPolicy()).redact_fields(
        {"passwordHash": "synthetic secret", "alice@example.test": "ok"},
        run_id=UUID(int=1),
    )
    assert result.classification is C.RESTRICTED
    assert "synthetic secret" not in str(result.fields)
    assert "alice@example.test" not in str(result.fields)


@pytest.mark.anyio
async def test_classification_never_downgrades_and_public_is_explicit() -> None:
    assert (
        await ContentProtector(DetectionPolicy()).classify("hello")
    ).classification is C.INTERNAL
    public = ContentProtector(DetectionPolicy(baseline=C.PUBLIC))
    assert (await public.classify("hello")).classification is C.PUBLIC
    assert (
        await public.classify("hello", minimum_classification=C.RESTRICTED)
    ).classification is C.RESTRICTED


@pytest.mark.anyio
async def test_custom_patterns_only_add_findings() -> None:
    protector = ContentProtector(
        DetectionPolicy(custom_patterns=(CustomPattern(pattern=r"CLIENT-[0-9]{4}"),))
    )
    result = await protector.redact(
        "CLIENT-1234 alice@example.test", run_id=UUID(int=1)
    )
    assert "CLIENT-1234" not in result.text and "alice@example.test" not in result.text
    assert result.classification is C.RESTRICTED


@pytest.mark.parametrize(
    "pattern", [r"(a+)+$", r"(a|aa)+$", r"(?=a)", r"(a)\1", r".*", r"^a*"]
)
def test_unsafe_or_empty_custom_pattern_is_rejected(pattern: str) -> None:
    with pytest.raises(SecurityPolicyError, match="SECURITY_PATTERN_UNSUPPORTED"):
        ContentProtector(
            DetectionPolicy(custom_patterns=(CustomPattern(pattern=pattern),))
        )


@pytest.mark.anyio
@pytest.mark.parametrize("extra", [0, 1])
async def test_scan_character_boundary(extra: int) -> None:
    protector = ContentProtector(DetectionPolicy(limits=ScanLimits(max_chars=4)))
    if extra:
        with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
            await protector.classify("a" * 5)
    else:
        assert (await protector.classify("a" * 4)).complete


@given(st.text(alphabet="0123456789", min_size=20, max_size=1000))
def test_card_check_is_bounded_and_ascii_only(text: str) -> None:
    from structuraguard.domain.pii_patterns import card_like

    assert not card_like(text)
    assert not card_like("４１１１１１１１１１１１１１１１")
