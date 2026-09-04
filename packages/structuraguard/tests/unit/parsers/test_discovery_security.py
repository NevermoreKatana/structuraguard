from __future__ import annotations

import os
import traceback
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Never, cast

import pytest

import structuraguard.parsers.discovery as discovery_module
from structuraguard.contracts.plugins import ParserDiscoveryPolicy
from structuraguard.exceptions import ParserError
from structuraguard.parsers.discovery import (
    PARSER_ENTRY_POINT_GROUP,
    discover_parser_plugins,
)


@dataclass(slots=True)
class _LoadSentinelEntryPoint:
    name: str
    value: str
    group: str = PARSER_ENTRY_POINT_GROUP
    load_called: bool = False

    def load(self) -> Never:
        self.load_called = True
        raise AssertionError("EntryPoint.load() был вызван")


@dataclass(frozen=True, slots=True)
class _Distribution:
    name: str
    version: str
    entry_points: tuple[_LoadSentinelEntryPoint, ...]

    @property
    def metadata(self) -> dict[str, str]:
        return {"Name": self.name}


class _BrokenEntryPoints:
    def __iter__(self) -> Never:
        raise RuntimeError("password=DO_NOT_LEAK_ENTRY_POINTS")


class _BrokenGroupEntryPoint:
    def __init__(self, error: Exception) -> None:
        self._error = error

    @property
    def group(self) -> Never:
        raise self._error


class _HostilePolicyValue:
    def __repr__(self) -> str:
        return "password=DO_NOT_LEAK_POLICY_CONTEXT"


class _UnsupportedDistribution(metadata.Distribution):
    def __init__(self) -> None:
        self.accesses: list[str] = []

    def read_text(self, filename: str) -> Never:
        self.accesses.append(filename)
        raise AssertionError("password=DO_NOT_READ_CUSTOM_PROVIDER")

    def locate_file(self, path: str | os.PathLike[str]) -> Never:
        self.accesses.append(os.fspath(path))
        raise AssertionError("password=DO_NOT_LOCATE_CUSTOM_PROVIDER")


def _forbid_global_scan() -> Never:
    raise AssertionError("Discovery выполнило глобальное сканирование distributions")


def _snapshot(distribution: _Distribution) -> discovery_module._DistributionSnapshot:
    return discovery_module._DistributionSnapshot(
        name=distribution.name,
        version=distribution.version,
        entry_points=cast(tuple[object, ...], distribution.entry_points),
    )


def _write_dist_info(
    root: Path,
    *,
    distribution_name: str,
    metadata_text: str | None = None,
    entry_points_text: str | None = None,
) -> metadata.Distribution:
    dist_info = root / f"{distribution_name.replace('-', '_')}-1.0.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        metadata_text
        or (f"Metadata-Version: 2.1\nName: {distribution_name}\nVersion: 1.0.0\n"),
        encoding="utf-8",
    )
    if entry_points_text is not None:
        (dist_info / "entry_points.txt").write_text(
            entry_points_text,
            encoding="utf-8",
        )
    return metadata.Distribution.at(dist_info)


@pytest.mark.parametrize(
    ("oversized_filename", "expected_reason"),
    (
        ("METADATA", "distribution_metadata_size_limit"),
        ("entry_points.txt", "entry_points_size_limit"),
    ),
)
def test_oversized_metadata_is_bounded_and_does_not_hide_sibling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    oversized_filename: str,
    expected_reason: str,
) -> None:
    working = _write_dist_info(
        tmp_path / "working",
        distribution_name="working-plugin",
        entry_points_text=(
            "[structuraguard.parsers]\nparser.working = vendor.good:Parser\n"
        ),
    )
    oversized = _write_dist_info(
        tmp_path / "oversized",
        distribution_name="oversized-plugin",
        entry_points_text=(
            "[structuraguard.parsers]\nparser.oversized = vendor.bad:Parser\n"
        ),
    )
    metadata_root = cast(Path, oversized.__dict__["_path"])
    (metadata_root / oversized_filename).write_bytes(b"x" * (1024 * 1024 + 1))
    load_calls: list[str] = []

    def forbidden_load(entry_point: metadata.EntryPoint) -> Never:
        load_calls.append(entry_point.name)
        raise AssertionError("EntryPoint.load() был вызван")

    def lookup(name: str) -> metadata.Distribution:
        return working if name == "working-plugin" else oversized

    monkeypatch.setattr(metadata.EntryPoint, "load", forbidden_load)
    monkeypatch.setattr(metadata, "distribution", lookup)

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(
            allowed_distributions=("working-plugin", "oversized-plugin"),
        )
    )

    assert [item.adapter_id for item in report.descriptors] == ["parser.working"]
    assert len(report.failures) == 1
    assert report.failures[0].distribution_name == "oversized-plugin"
    assert report.failures[0].error.details["reason"] == expected_reason
    assert load_calls == []


def test_oversized_metadata_header_reads_no_more_than_cap_plus_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    distribution = _write_dist_info(
        tmp_path,
        distribution_name="oversized-header-plugin",
        metadata_text="x" * (4 * discovery_module._MAX_METADATA_HEADER_BYTES),
    )
    original_read = os.read
    bytes_read = 0

    def counting_read(descriptor: int, size: int) -> bytes:
        nonlocal bytes_read
        chunk = original_read(descriptor, size)
        bytes_read += len(chunk)
        return chunk

    monkeypatch.setattr(os, "read", counting_read)
    monkeypatch.setattr(metadata, "distribution", lambda name: distribution)

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(allowed_distributions=("oversized-header-plugin",))
    )

    assert report.descriptors == ()
    assert len(report.failures) == 1
    assert report.failures[0].error.details["reason"] == (
        "distribution_metadata_size_limit"
    )
    assert bytes_read <= discovery_module._MAX_METADATA_HEADER_BYTES + 1


def test_metadata_mutation_during_read_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    distribution = _write_dist_info(
        tmp_path,
        distribution_name="mutable-plugin",
        metadata_text=(
            "Metadata-Version: 2.1\nName: mutable-plugin\nVersion: 1.0.0\n\n"
        ),
        entry_points_text=(
            "[structuraguard.parsers]\nparser.mutable = vendor.bad:Parser\n"
        ),
    )
    metadata_root = cast(Path, distribution.__dict__["_path"])
    metadata_path = metadata_root / "METADATA"
    original_read = os.read
    mutated = False

    def mutating_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        chunk = original_read(descriptor, size)
        if not mutated:
            mutated = True
            with metadata_path.open("ab") as stream:
                stream.write(b"x")
        return chunk

    monkeypatch.setattr(os, "read", mutating_read)
    monkeypatch.setattr(metadata, "distribution", lambda name: distribution)

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(allowed_distributions=("mutable-plugin",))
    )

    assert mutated is True
    assert report.descriptors == ()
    assert len(report.failures) == 1
    assert report.failures[0].error.details["reason"] == (
        "metadata_changed_during_read"
    )


def test_entry_points_symlink_is_rejected_without_following_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    distribution = _write_dist_info(
        tmp_path / "plugin",
        distribution_name="symlink-plugin",
    )
    outside = tmp_path / "outside-entry-points.txt"
    outside.write_text(
        "[structuraguard.parsers]\npassword=DO_NOT_LEAK_SYMLINK = vendor.bad:Parser\n",
        encoding="utf-8",
    )
    metadata_root = cast(Path, distribution.__dict__["_path"])
    (metadata_root / "entry_points.txt").symlink_to(outside)
    monkeypatch.setattr(metadata, "distribution", lambda name: distribution)

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(allowed_distributions=("symlink-plugin",))
    )

    assert report.descriptors == ()
    assert len(report.failures) == 1
    assert report.failures[0].error.details["reason"] == "metadata_path_unsafe"
    assert "DO_NOT_LEAK_SYMLINK" not in repr(report)


def test_symlinked_distribution_root_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = _write_dist_info(
        tmp_path / "target",
        distribution_name="root-link-plugin",
        entry_points_text=(
            "[structuraguard.parsers]\nparser.linked = vendor.bad:Parser\n"
        ),
    )
    target_path = cast(Path, target.__dict__["_path"])
    linked_path = tmp_path / "root_link_plugin-1.0.0.dist-info"
    linked_path.symlink_to(target_path, target_is_directory=True)
    linked = metadata.Distribution.at(linked_path)
    monkeypatch.setattr(metadata, "distribution", lambda name: linked)

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(allowed_distributions=("root-link-plugin",))
    )

    assert report.descriptors == ()
    assert len(report.failures) == 1
    assert report.failures[0].error.details["reason"] == "metadata_path_unsafe"


def test_custom_metadata_provider_is_rejected_before_property_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    custom = _UnsupportedDistribution()
    working = _write_dist_info(
        tmp_path,
        distribution_name="working-plugin",
        entry_points_text=(
            "[structuraguard.parsers]\nparser.working = vendor.good:Parser\n"
        ),
    )

    def lookup(name: str) -> metadata.Distribution:
        return custom if name == "custom-plugin" else working

    monkeypatch.setattr(metadata, "distribution", lookup)

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(
            allowed_distributions=("working-plugin", "custom-plugin"),
        )
    )

    assert [item.adapter_id for item in report.descriptors] == ["parser.working"]
    assert len(report.failures) == 1
    assert report.failures[0].distribution_name == "custom-plugin"
    assert report.failures[0].error.details["reason"] == (
        "metadata_provider_unsupported"
    )
    assert custom.accesses == []
    assert "DO_NOT_READ_CUSTOM_PROVIDER" not in repr(report)


def test_large_metadata_body_is_not_read_after_bounded_header(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    distribution = _write_dist_info(
        tmp_path,
        distribution_name="large-body-plugin",
        metadata_text=(
            "Metadata-Version: 2.1\n"
            "Name: large-body-plugin\n"
            "Version: 1.0.0\n\n" + "x" * (2 * 1024 * 1024)
        ),
        entry_points_text=(
            "[structuraguard.parsers]\nparser.large-body = vendor.safe:Parser\n"
        ),
    )
    original_read = os.read
    bytes_read = 0

    def counting_read(descriptor: int, size: int) -> bytes:
        nonlocal bytes_read
        chunk = original_read(descriptor, size)
        bytes_read += len(chunk)
        return chunk

    monkeypatch.setattr(os, "read", counting_read)
    monkeypatch.setattr(metadata, "distribution", lambda name: distribution)

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(allowed_distributions=("large-body-plugin",))
    )

    assert [item.adapter_id for item in report.descriptors] == ["parser.large-body"]
    assert report.failures == ()
    assert bytes_read < 70 * 1024


def test_discovery_never_performs_global_scan_or_loads_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    distribution = _write_dist_info(
        tmp_path,
        distribution_name="safe-plugin",
        entry_points_text=(
            "[structuraguard.parsers]\nparser.safe = vendor.safe:Parser\n"
        ),
    )
    calls: list[str] = []
    load_calls: list[str] = []

    def lookup(name: str) -> metadata.Distribution:
        calls.append(name)
        return distribution

    def forbidden_load(entry_point: metadata.EntryPoint) -> Never:
        load_calls.append(entry_point.name)
        raise AssertionError("EntryPoint.load() был вызван")

    monkeypatch.setattr(metadata, "distribution", lookup)
    monkeypatch.setattr(metadata, "distributions", _forbid_global_scan)
    monkeypatch.setattr(metadata, "entry_points", _forbid_global_scan)
    monkeypatch.setattr(metadata.EntryPoint, "load", forbidden_load)

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(allowed_distributions=("safe-plugin",))
    )

    assert calls == ["safe-plugin"]
    assert [descriptor.adapter_id for descriptor in report.descriptors] == [
        "parser.safe"
    ]
    assert load_calls == []


def test_forged_discovery_policy_is_rejected_before_metadata_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = ParserDiscoveryPolicy(allowed_distributions=("safe-plugin",))
    forged = policy.model_copy(
        update={
            "allowed_distributions": ("../../outside",),
            "max_entries": 100_000,
        }
    )
    monkeypatch.setattr(metadata, "distribution", _forbid_global_scan)

    with pytest.raises(ParserError) as raised:
        discover_parser_plugins(forged)

    assert raised.value.error_code == "PARSER_PLUGIN_DISCOVERY_FAILED"
    assert raised.value.details["reason"] == "policy_schema"


def test_forged_policy_serialization_error_does_not_retain_raw_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = ParserDiscoveryPolicy(allowed_distributions=("safe-plugin",))
    forged = policy.model_copy(
        update={
            "allowed_distributions": cast(
                tuple[str, ...],
                _HostilePolicyValue(),
            )
        }
    )
    monkeypatch.setattr(metadata, "distribution", _forbid_global_scan)

    with pytest.raises(ParserError) as raised:
        discover_parser_plugins(forged)

    rendered = " ".join(
        (
            str(raised.value),
            repr(raised.value),
            repr(raised.value.details),
            repr(raised.value.__context__),
            repr(raised.value.__cause__),
            "".join(traceback.format_exception(raised.value)),
        )
    )
    assert raised.value.error_code == "PARSER_PLUGIN_DISCOVERY_FAILED"
    assert raised.value.details["reason"] == "policy_schema"
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert "DO_NOT_LEAK_POLICY_CONTEXT" not in rendered


@pytest.mark.parametrize(
    "target",
    [
        "vendor.safe:Parser [remote]",
        "../vendor:Parser",
        "https://attacker.invalid/plugin:Parser",
        "vendor.safe:Parser()",
        "vendor.safe:\u202eParser",
        "vendor.safe",
        "vendor.safe:password=DO_NOT_LEAK_TARGET",
    ],
)
def test_untrusted_target_grammar_is_rejected_without_echo(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    entry_point = _LoadSentinelEntryPoint(name="parser.hostile", value=target)
    distribution = _Distribution(
        name="hostile-plugin",
        version="1.0.0",
        entry_points=(entry_point,),
    )

    def lookup(name: str) -> discovery_module._DistributionSnapshot:
        assert name == "hostile-plugin"
        return _snapshot(distribution)

    monkeypatch.setattr(discovery_module, "_resolve_distribution", lookup)

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(allowed_distributions=("hostile-plugin",))
    )

    assert report.descriptors == ()
    assert len(report.failures) == 1
    assert report.failures[0].error.error_code == "PARSER_PLUGIN_METADATA_INVALID"
    assert report.failures[0].error.details["reason"] == "target_format"
    rendered = f"{report!s} {report!r} {report.failures[0].error.details!r}"
    assert target not in rendered
    assert "DO_NOT_LEAK_TARGET" not in rendered
    assert entry_point.load_called is False


def test_distribution_version_uses_descriptor_grammar_at_metadata_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    distribution = _Distribution(
        name="hostile-plugin",
        version="1.0~hostile",
        entry_points=(
            _LoadSentinelEntryPoint(
                name="parser.hostile",
                value="vendor.safe:Parser",
            ),
        ),
    )
    monkeypatch.setattr(
        discovery_module,
        "_resolve_distribution",
        lambda name: _snapshot(distribution),
    )

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(allowed_distributions=("hostile-plugin",))
    )

    assert report.descriptors == ()
    assert len(report.failures) == 1
    assert report.failures[0].adapter_id is None
    assert report.failures[0].error.error_code == "PARSER_PLUGIN_METADATA_INVALID"
    assert report.failures[0].error.details["reason"] == "distribution_version_format"


def test_hostile_distribution_metadata_is_rejected_without_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "password=DO_NOT_LEAK_DISTRIBUTION"
    distribution = _Distribution(
        name=f"safe-plugin {secret}",
        version="1.0.0",
        entry_points=(
            _LoadSentinelEntryPoint(name="parser.safe", value="vendor.safe:Parser"),
        ),
    )

    def lookup(name: str) -> discovery_module._DistributionSnapshot:
        assert name == "safe-plugin"
        return _snapshot(distribution)

    monkeypatch.setattr(discovery_module, "_resolve_distribution", lookup)

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(allowed_distributions=("safe-plugin",))
    )

    assert report.descriptors == ()
    assert len(report.failures) == 1
    rendered = f"{report!s} {report!r} {report.failures[0].error.details!r}"
    assert secret not in rendered
    assert "DO_NOT_LEAK_DISTRIBUTION" not in rendered


def test_hostile_adapter_name_is_rejected_without_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "password=DO_NOT_LEAK_ADAPTER"
    distribution = _Distribution(
        name="safe-plugin",
        version="1.0.0",
        entry_points=(
            _LoadSentinelEntryPoint(
                name=f"parser.safe {secret}",
                value="vendor.safe:Parser",
            ),
        ),
    )

    def lookup(name: str) -> discovery_module._DistributionSnapshot:
        return _snapshot(distribution)

    monkeypatch.setattr(discovery_module, "_resolve_distribution", lookup)

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(allowed_distributions=("safe-plugin",))
    )

    assert report.descriptors == ()
    assert len(report.failures) == 1
    assert report.failures[0].adapter_id is None
    rendered = f"{report!s} {report!r} {report.failures[0].error.details!r}"
    assert secret not in rendered
    assert "DO_NOT_LEAK_ADAPTER" not in rendered


def test_credential_canary_adapter_name_is_rejected_without_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-supersecret"
    distribution = _Distribution(
        name="safe-plugin",
        version="1.0.0",
        entry_points=(
            _LoadSentinelEntryPoint(
                name=secret,
                value="vendor.safe:Parser",
            ),
        ),
    )
    monkeypatch.setattr(
        discovery_module,
        "_resolve_distribution",
        lambda name: _snapshot(distribution),
    )

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(allowed_distributions=("safe-plugin",))
    )

    assert report.descriptors == ()
    rendered = f"{report!s} {report!r} {report.failures[0].error.details!r}"
    assert secret not in rendered
    assert report.failures[0].adapter_id is None


def test_failure_order_is_independent_of_hostile_entry_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = (
        _BrokenGroupEntryPoint(ValueError("first")),
        _BrokenGroupEntryPoint(RuntimeError("second")),
    )

    def run(current: tuple[_BrokenGroupEntryPoint, ...]) -> tuple[str | None, ...]:
        distribution = _Distribution(
            name="safe-plugin",
            version="1.0.0",
            entry_points=cast(tuple[_LoadSentinelEntryPoint, ...], current),
        )
        monkeypatch.setattr(
            discovery_module,
            "_resolve_distribution",
            lambda name: _snapshot(distribution),
        )
        report = discover_parser_plugins(
            ParserDiscoveryPolicy(allowed_distributions=("safe-plugin",))
        )
        return tuple(failure.error.cause for failure in report.failures)

    assert run(entries) == run(tuple(reversed(entries)))


def test_entry_enumeration_error_is_sanitized_and_other_distribution_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working = _Distribution(
        name="working-plugin",
        version="1.0.0",
        entry_points=(
            _LoadSentinelEntryPoint(name="parser.working", value="vendor.good:Parser"),
        ),
    )

    def lookup(name: str) -> discovery_module._DistributionSnapshot:
        if name == "broken-plugin":
            return discovery_module._DistributionSnapshot(
                name="broken-plugin",
                version="1.0.0",
                entry_points=cast(tuple[object, ...], _BrokenEntryPoints()),
            )
        return _snapshot(working)

    monkeypatch.setattr(discovery_module, "_resolve_distribution", lookup)

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(
            allowed_distributions=("working-plugin", "broken-plugin"),
        )
    )

    assert [descriptor.adapter_id for descriptor in report.descriptors] == [
        "parser.working"
    ]
    assert len(report.failures) == 1
    assert report.failures[0].error.details["reason"] == "entry_enumeration_failed"
    rendered = f"{report!s} {report!r} {report.failures[0].error.details!r}"
    assert "DO_NOT_LEAK_ENTRY_POINTS" not in rendered


def test_spoofed_group_is_ignored_before_target_is_inspected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_point = _LoadSentinelEntryPoint(
        name="parser.spoofed",
        value="password=DO_NOT_INSPECT_WRONG_GROUP",
        group="structuraguard.parser",
    )
    distribution = _Distribution(
        name="spoofed-plugin",
        version="1.0.0",
        entry_points=(entry_point,),
    )

    def lookup(name: str) -> discovery_module._DistributionSnapshot:
        return _snapshot(distribution)

    monkeypatch.setattr(discovery_module, "_resolve_distribution", lookup)

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(allowed_distributions=("spoofed-plugin",))
    )

    assert report.descriptors == ()
    assert report.failures == ()
    assert entry_point.load_called is False


def test_oversized_distribution_is_bounded_without_hiding_other_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized = _Distribution(
        name="oversized-plugin",
        version="1.0.0",
        entry_points=tuple(
            _LoadSentinelEntryPoint(
                name=f"parser.item-{index}",
                value=f"vendor.parsers:Parser{index}",
            )
            for index in range(257)
        ),
    )
    working = _Distribution(
        name="working-plugin",
        version="1.0.0",
        entry_points=(
            _LoadSentinelEntryPoint(name="parser.working", value="vendor.good:Parser"),
        ),
    )

    def lookup(name: str) -> discovery_module._DistributionSnapshot:
        distribution = oversized if name == "oversized-plugin" else working
        return _snapshot(distribution)

    monkeypatch.setattr(discovery_module, "_resolve_distribution", lookup)

    report = discover_parser_plugins(
        ParserDiscoveryPolicy(
            allowed_distributions=("oversized-plugin", "working-plugin"),
        )
    )

    assert [descriptor.adapter_id for descriptor in report.descriptors] == [
        "parser.working"
    ]
    assert len(report.failures) == 1
    assert report.failures[0].distribution_name == "oversized-plugin"
    assert (
        report.failures[0].error.details["reason"]
        == "distribution_entry_limit_exceeded"
    )
