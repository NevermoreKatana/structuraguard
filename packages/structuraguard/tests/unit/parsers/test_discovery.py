from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import cast

import pytest

import structuraguard.parsers.discovery as discovery_module
from structuraguard.contracts.plugins import ParserDiscoveryPolicy
from structuraguard.parsers.discovery import (
    PARSER_ENTRY_POINT_GROUP,
    discover_parser_plugins,
)


@dataclass(frozen=True, slots=True)
class _EntryPoint:
    name: str
    value: str
    group: str = PARSER_ENTRY_POINT_GROUP

    def load(self) -> object:
        raise AssertionError("Discovery не должно загружать entry-point target")


@dataclass(frozen=True, slots=True)
class _Distribution:
    name: str
    version: str
    entry_points: tuple[_EntryPoint, ...]

    @property
    def metadata(self) -> dict[str, str]:
        return {"Name": self.name}


def _install_distributions(
    monkeypatch: pytest.MonkeyPatch,
    distributions: dict[str, _Distribution | BaseException],
) -> list[str]:
    calls: list[str] = []

    def lookup(name: str) -> discovery_module._DistributionSnapshot:
        calls.append(name)
        result = distributions[name]
        if isinstance(result, BaseException):
            raise result
        return discovery_module._DistributionSnapshot(
            name=result.name,
            version=result.version,
            entry_points=cast(tuple[object, ...], result.entry_points),
        )

    monkeypatch.setattr(discovery_module, "_resolve_distribution", lookup)
    return calls


def test_discovery_looks_up_only_allowlisted_distributions_in_canonical_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_distributions(
        monkeypatch,
        {
            "alpha-plugin": _Distribution(
                name="Alpha_Plugin",
                version="1.2.0",
                entry_points=(
                    _EntryPoint(name="parser.zeta", value="vendor.alpha:ZetaParser"),
                    _EntryPoint(
                        name="ignored",
                        value="vendor.alpha:IgnoredParser",
                        group="another.group",
                    ),
                ),
            ),
            "zeta-plugin": _Distribution(
                name="zeta-plugin",
                version="2.0.0",
                entry_points=(
                    _EntryPoint(name="parser.alpha", value="vendor.zeta:Parser"),
                ),
            ),
        },
    )

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(
            allowed_distributions=("zeta-plugin", "alpha-plugin"),
        )
    )

    assert calls == ["alpha-plugin", "zeta-plugin"]
    assert report.failures == ()
    assert tuple(descriptor.adapter_id for descriptor in report.descriptors) == (
        "parser.alpha",
        "parser.zeta",
    )
    assert report.descriptors[0].module == "vendor.zeta"
    assert report.descriptors[0].attribute == "Parser"
    assert report.descriptors[0].entry_point_group == PARSER_ENTRY_POINT_GROUP
    assert report.descriptors[0].metadata_fingerprint.startswith("sha256:")


def test_discovery_reads_real_dist_info_as_descriptor_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dist_info = tmp_path / "sample_plugin-1.4.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: sample-plugin\nVersion: 1.4.0\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        "[structuraguard.parsers]\n"
        "parser.sample = vendor.sample:SampleParser\n"
        "[another.group]\n"
        "ignored = vendor.sample:Ignored\n",
        encoding="utf-8",
    )
    distribution = metadata.Distribution.at(dist_info)

    def lookup(name: str) -> metadata.Distribution:
        assert name == "sample-plugin"
        return distribution

    monkeypatch.setattr(metadata, "distribution", lookup)

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(allowed_distributions=("sample-plugin",))
    )

    assert report.failures == ()
    assert len(report.descriptors) == 1
    descriptor = report.descriptors[0]
    assert descriptor.adapter_id == "parser.sample"
    assert descriptor.distribution_version == "1.4.0"
    assert descriptor.module == "vendor.sample"
    assert descriptor.attribute == "SampleParser"


def test_discovery_is_stable_when_entry_point_order_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = (
        _EntryPoint(name="parser.beta", value="vendor.parsers:Beta"),
        _EntryPoint(name="parser.alpha", value="vendor.parsers:Alpha"),
    )
    distribution = _Distribution(
        name="stable-plugin",
        version="1.0.0",
        entry_points=entries,
    )
    _install_distributions(monkeypatch, {"stable-plugin": distribution})
    policy = ParserDiscoveryPolicy(allowed_distributions=("stable-plugin",))

    first = discover_parser_plugins(policy)
    reversed_distribution = _Distribution(
        name="stable-plugin",
        version="1.0.0",
        entry_points=tuple(reversed(entries)),
    )
    _install_distributions(
        monkeypatch,
        {"stable-plugin": reversed_distribution},
    )
    second = discover_parser_plugins(policy)

    assert first == second


def test_distribution_failure_is_reported_without_hiding_other_plugins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_distributions(
        monkeypatch,
        {
            "broken-plugin": RuntimeError("broken metadata"),
            "working-plugin": _Distribution(
                name="working-plugin",
                version="1.0.0",
                entry_points=(
                    _EntryPoint(name="parser.working", value="vendor.good:Parser"),
                ),
            ),
        },
    )

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(
            allowed_distributions=("working-plugin", "broken-plugin"),
        )
    )

    assert [item.adapter_id for item in report.descriptors] == ["parser.working"]
    assert len(report.failures) == 1
    assert report.failures[0].distribution_name == "broken-plugin"
    assert report.failures[0].error.error_code == "PARSER_PLUGIN_DISCOVERY_FAILED"
    assert report.failures[0].error.details["reason"] == "distribution_lookup_failed"


def test_invalid_entry_does_not_discard_valid_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_distributions(
        monkeypatch,
        {
            "mixed-plugin": _Distribution(
                name="mixed-plugin",
                version="1.0.0",
                entry_points=(
                    _EntryPoint(name="INVALID NAME", value="vendor.bad:Parser"),
                    _EntryPoint(name="parser.valid", value="vendor.good:Parser"),
                ),
            )
        },
    )

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(allowed_distributions=("mixed-plugin",))
    )

    assert [item.adapter_id for item in report.descriptors] == ["parser.valid"]
    assert len(report.failures) == 1
    assert report.failures[0].adapter_id is None
    assert report.failures[0].error.error_code == "PARSER_PLUGIN_METADATA_INVALID"
    assert report.failures[0].error.details["reason"] == "adapter_id_format"


def test_duplicate_adapter_ids_are_all_rejected_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_distributions(
        monkeypatch,
        {
            "first-plugin": _Distribution(
                name="first-plugin",
                version="1.0.0",
                entry_points=(
                    _EntryPoint(name="parser.duplicate", value="vendor.first:Parser"),
                ),
            ),
            "second-plugin": _Distribution(
                name="second-plugin",
                version="1.0.0",
                entry_points=(
                    _EntryPoint(name="parser.duplicate", value="vendor.second:Parser"),
                ),
            ),
        },
    )

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(
            allowed_distributions=("first-plugin", "second-plugin"),
        )
    )

    assert report.descriptors == ()
    assert len(report.failures) == 2
    assert {failure.error.details["reason"] for failure in report.failures} == {
        "duplicate_adapter_id"
    }
    assert all(
        failure.error.error_code == "PARSER_DUPLICATE_REGISTRATION"
        for failure in report.failures
    )


def test_duplicate_targets_are_all_rejected_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_distributions(
        monkeypatch,
        {
            "first-plugin": _Distribution(
                name="first-plugin",
                version="1.0.0",
                entry_points=(
                    _EntryPoint(name="parser.first", value="vendor.shared:Parser"),
                ),
            ),
            "second-plugin": _Distribution(
                name="second-plugin",
                version="1.0.0",
                entry_points=(
                    _EntryPoint(name="parser.second", value="vendor.shared:Parser"),
                ),
            ),
        },
    )

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(
            allowed_distributions=("first-plugin", "second-plugin"),
        )
    )

    assert report.descriptors == ()
    assert len(report.failures) == 2
    assert {failure.error.details["reason"] for failure in report.failures} == {
        "duplicate_target"
    }


def test_max_entries_keeps_canonical_prefix_and_reports_excess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_distributions(
        monkeypatch,
        {
            "bounded-plugin": _Distribution(
                name="bounded-plugin",
                version="1.0.0",
                entry_points=(
                    _EntryPoint(name="parser.zeta", value="vendor.parsers:Zeta"),
                    _EntryPoint(name="parser.alpha", value="vendor.parsers:Alpha"),
                ),
            )
        },
    )

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(
            allowed_distributions=("bounded-plugin",),
            max_entries=1,
        )
    )

    assert [item.adapter_id for item in report.descriptors] == ["parser.alpha"]
    assert len(report.failures) == 1
    assert report.failures[0].adapter_id == "parser.zeta"
    assert report.failures[0].error.error_code == "PARSER_PLUGIN_DISCOVERY_FAILED"
    assert report.failures[0].error.details["reason"] == "entry_limit_exceeded"
