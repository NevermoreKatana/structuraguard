from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from structuraguard.contracts import (
    PARSER_ENTRY_POINT_GROUP,
    ParserDiscoveryPolicy,
    ParserPluginDescriptor,
    ProbeResult,
    ProbeSignal,
    ProbeSignalKind,
    ProbeSignalOutcome,
)

SOURCE_FINGERPRINT = "sha256:" + "a" * 64


def _probe_payload() -> dict[str, object]:
    return {
        "source": {
            "artifact_id": "source-1",
            "source_fingerprint": SOURCE_FINGERPRINT,
        },
        "adapter_id": "parser.csv",
        "adapter_version": "1.0.0",
        "supported": True,
        "confidence": "0.95",
        "detected_media_type": "text/csv",
    }


def _descriptor_payload() -> dict[str, object]:
    return {
        "adapter_id": "parser.csv",
        "distribution_name": "structuraguard-parser-csv",
        "distribution_version": "1.2.3",
        "module": "structuraguard_parser_csv.adapter",
        "attribute": "CsvParser",
    }


def test_probe_signal_vocabulary_is_closed_and_stable() -> None:
    assert tuple(ProbeSignalKind) == (
        ProbeSignalKind.SIGNATURE,
        ProbeSignalKind.CONTENT_MEDIA_TYPE,
        ProbeSignalKind.INTERNAL_STRUCTURE,
        ProbeSignalKind.DECLARED_MEDIA_TYPE,
        ProbeSignalKind.EXTENSION,
    )
    assert tuple(member.value for member in ProbeSignalOutcome) == (
        "match",
        "mismatch",
        "inconclusive",
    )


def test_probe_result_accepts_legacy_json_without_m03_evidence() -> None:
    result = ProbeResult.model_validate(_probe_payload())
    restored = ProbeResult.model_validate_json(result.model_dump_json())

    assert restored == result
    assert restored.format_id is None
    assert restored.signals == ()


def test_probe_result_preserves_typed_bounded_evidence() -> None:
    result = ProbeResult.model_validate(
        {
            **_probe_payload(),
            "format_id": "csv",
            "signals": (
                {"kind": "signature", "outcome": "match"},
                {"kind": "extension", "outcome": "mismatch"},
            ),
        }
    )

    assert result.confidence == Decimal("0.95")
    assert result.signals == (
        ProbeSignal(
            kind=ProbeSignalKind.SIGNATURE,
            outcome=ProbeSignalOutcome.MATCH,
        ),
        ProbeSignal(
            kind=ProbeSignalKind.EXTENSION,
            outcome=ProbeSignalOutcome.MISMATCH,
        ),
    )


@pytest.mark.parametrize(
    "format_id",
    (
        "CSV",
        " csv",
        "csv ",
        "csv/tsv",
        "csv..table",
        "сsv",
        "sk-supersecret",
        "parser.csv\n",
        "a" * 129,
    ),
)
def test_probe_result_rejects_noncanonical_format_id(format_id: str) -> None:
    with pytest.raises(ValidationError):
        ProbeResult.model_validate({**_probe_payload(), "format_id": format_id})


def test_probe_result_rejects_duplicate_signal_kinds() -> None:
    with pytest.raises(ValidationError):
        ProbeResult.model_validate(
            {
                **_probe_payload(),
                "format_id": "csv",
                "signals": (
                    {"kind": "signature", "outcome": "match"},
                    {"kind": "signature", "outcome": "inconclusive"},
                ),
            }
        )


def test_plugin_descriptor_is_declarative_frozen_and_self_fingerprinting() -> None:
    descriptor = ParserPluginDescriptor.model_validate(_descriptor_payload())
    same = ParserPluginDescriptor.model_validate(_descriptor_payload())

    assert descriptor == same
    assert descriptor.entry_point_group == PARSER_ENTRY_POINT_GROUP
    assert descriptor.metadata_fingerprint.startswith("sha256:")
    assert descriptor.content_fingerprint() == descriptor.metadata_fingerprint
    assert {
        "target",
        "callable",
        "factory",
        "metadata",
        "path",
    }.isdisjoint(ParserPluginDescriptor.model_fields)
    with pytest.raises(ValidationError):
        ParserPluginDescriptor.model_validate(
            {**descriptor.model_dump(), "metadata_fingerprint": "sha256:" + "f" * 64}
        )
    stale = descriptor.model_copy(update={"module": "vendor.changed"})
    with pytest.raises(ValueError, match="fingerprint"):
        stale.validate_content_fingerprint()


def test_plugin_descriptor_rejects_explicit_auto_fingerprint_sentinel() -> None:
    with pytest.raises(ValidationError, match="fingerprint"):
        ParserPluginDescriptor.model_validate(
            {
                **_descriptor_payload(),
                "metadata_fingerprint": "sha256:" + "0" * 64,
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("adapter_id", "Parser.CSV"),
        ("adapter_id", "parser/csv"),
        ("distribution_name", "StructuraGuard-Parser-CSV"),
        ("distribution_name", "structuraguard_parser_csv"),
        ("distribution_version", "https://evil.invalid/plugin"),
        ("distribution_version", "sk-supersecret"),
        ("module", "../plugin"),
        ("module", "plugin:factory"),
        ("module", "plugin\u202edriver"),
        ("attribute", "Parser()"),
        ("attribute", "Parser [extra]"),
        ("entry_point_group", "structuraguard.parser"),
    ),
)
def test_plugin_descriptor_rejects_noncanonical_or_executable_metadata(
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError):
        ParserPluginDescriptor.model_validate({**_descriptor_payload(), field: value})


def test_discovery_policy_is_bounded_frozen_and_canonical() -> None:
    policy = ParserDiscoveryPolicy(
        allowed_distributions=("vendor-parser", "structuraguard-parser-csv"),
        max_entries=16,
    )

    assert policy.allowed_distributions == (
        "structuraguard-parser-csv",
        "vendor-parser",
    )
    with pytest.raises(ValidationError):
        policy.max_entries = 17


@pytest.mark.parametrize(
    "payload",
    (
        {"allowed_distributions": ()},
        {"allowed_distributions": ("vendor-parser", "vendor-parser")},
        {"allowed_distributions": ("Vendor-Parser",)},
        {"allowed_distributions": tuple(f"parser-{index}" for index in range(65))},
        {"allowed_distributions": ("vendor-parser",), "max_entries": 0},
        {"allowed_distributions": ("vendor-parser",), "max_entries": 257},
        {"allowed_distributions": ("vendor-parser",), "max_entries": True},
    ),
)
def test_discovery_policy_rejects_invalid_allowlist_and_limits(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ParserDiscoveryPolicy.model_validate(payload)


def test_m03_contracts_are_public_only_from_contract_namespace() -> None:
    import structuraguard.contracts as contracts

    expected = {
        "PARSER_ENTRY_POINT_GROUP",
        "ParserDiscoveryPolicy",
        "ParserPluginDescriptor",
        "ProbeSignal",
        "ProbeSignalKind",
        "ProbeSignalOutcome",
    }

    assert expected <= set(contracts.__all__)
