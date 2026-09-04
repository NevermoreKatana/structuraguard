"""Явное безопасное discovery декларативных parser plugin descriptors."""

from __future__ import annotations

import os
import re
import stat
from collections import Counter
from dataclasses import dataclass
from importlib import metadata
from itertools import islice
from pathlib import Path
from typing import Final, Never, cast

from pydantic import ValidationError

from structuraguard.contracts.common import _safe_text
from structuraguard.contracts.plugins import (
    PARSER_ENTRY_POINT_GROUP,
    ParserDiscoveryPolicy,
    ParserPluginDescriptor,
)
from structuraguard.exceptions import ParserError

_ADAPTER_ID_PATTERN: Final = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_DISTRIBUTION_NAME_PATTERN: Final = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,253}[A-Za-z0-9])?$"
)
_TARGET_PATTERN: Final = re.compile(
    r"^(?P<module>[A-Za-z_][A-Za-z0-9_]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_]*)*):"
    r"(?P<attribute>[A-Za-z_][A-Za-z0-9_]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_]*)*)$"
)
_VERSION_CHARACTERS: Final = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.!+_-"
)
_MAX_METADATA_HEADER_BYTES: Final = 64 * 1024
_MAX_ENTRY_POINTS_BYTES: Final = 256 * 1024
_METADATA_READ_CHUNK_BYTES: Final = 64 * 1024
_MAX_ENTRY_POINTS_PER_DISTRIBUTION: Final = 256

_DUPLICATE_REGISTRATION: Final = "PARSER_DUPLICATE_REGISTRATION"
_PLUGIN_DISCOVERY_FAILED: Final = "PARSER_PLUGIN_DISCOVERY_FAILED"
_PLUGIN_METADATA_INVALID: Final = "PARSER_PLUGIN_METADATA_INVALID"


@dataclass(frozen=True, slots=True, kw_only=True)
class ParserPluginDiscoveryFailure:
    """Санитизированная typed failure одного distribution или entry point.

    Args:
        distribution_name: PEP 503-canonical имя allowlisted distribution.
        adapter_id: Canonical ID затронутого entry point, если его удалось
            безопасно определить.
        error: Typed ``ParserError`` со стабильным code и безопасным reason.

    Raises:
        ValueError: Имя distribution или ``adapter_id`` неканоничны.
        TypeError: ``error`` не является ``ParserError``.

    Security:
        Failure не предназначена для raw metadata, filesystem paths или текста
        исходного исключения. Такие значения не должны добавляться caller.
    """

    distribution_name: str
    adapter_id: str | None
    error: ParserError

    def __post_init__(self) -> None:
        if not _is_canonical_distribution_name(self.distribution_name):
            raise ValueError("distribution_name должен быть PEP 503-canonical")
        if self.adapter_id is not None and not _is_canonical_adapter_id(
            self.adapter_id
        ):
            raise ValueError("adapter_id должен быть каноническим")
        if not isinstance(self.error, ParserError):
            raise TypeError("error должен быть ParserError")


@dataclass(frozen=True, slots=True, kw_only=True)
class ParserPluginDiscoveryReport:
    """Детерминированный результат explicit discovery без активации кода.

    Args:
        descriptors: Валидные declarative descriptors в canonical order.
        failures: Санитизированные failures в deterministic order.

    Raises:
        TypeError: Коллекции не являются точными tuples ожидаемых типов.

    Report может одновременно содержать успешные descriptors и ошибки других
    distributions. Чтение report не импортирует и не выполняет plugin code.
    """

    descriptors: tuple[ParserPluginDescriptor, ...]
    failures: tuple[ParserPluginDiscoveryFailure, ...]

    def __post_init__(self) -> None:
        if type(self.descriptors) is not tuple or not all(
            isinstance(item, ParserPluginDescriptor) for item in self.descriptors
        ):
            raise TypeError("descriptors должен быть tuple ParserPluginDescriptor")
        if type(self.failures) is not tuple or not all(
            isinstance(item, ParserPluginDiscoveryFailure) for item in self.failures
        ):
            raise TypeError("failures должен быть tuple ParserPluginDiscoveryFailure")


@dataclass(frozen=True, slots=True)
class _Candidate:
    descriptor: ParserPluginDescriptor

    @property
    def target(self) -> tuple[str, str]:
        return (self.descriptor.module, self.descriptor.attribute)


@dataclass(frozen=True, slots=True)
class _EntryPointMetadata:
    name: object
    value: object
    group: object


@dataclass(frozen=True, slots=True)
class _DistributionSnapshot:
    name: object
    version: object
    entry_points: tuple[object, ...]


class _DistributionBoundaryError(Exception):
    """Внутренняя ошибка fail-closed metadata boundary с безопасным reason."""

    __slots__ = ("error_code", "reason")

    def __init__(self, *, error_code: str, reason: str) -> None:
        self.error_code = error_code
        self.reason = reason
        super().__init__(reason)


def _canonical_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _is_canonical_distribution_name(value: object) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= 255
        and _DISTRIBUTION_NAME_PATTERN.fullmatch(value) is not None
        and value == _canonical_distribution_name(value)
    )


def _is_canonical_adapter_id(value: object) -> bool:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or _ADAPTER_ID_PATTERN.fullmatch(value) is None
    ):
        return False
    try:
        _safe_text(value)
    except ValueError:
        return False
    return True


def _is_safe_distribution_metadata_name(value: object, expected: str) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= 255
        and _DISTRIBUTION_NAME_PATTERN.fullmatch(value) is not None
        and _canonical_distribution_name(value) == expected
    )


def _is_safe_version(value: object) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= 128
        and value[0].isalnum()
        and value[0].isascii()
        and all(character in _VERSION_CHARACTERS for character in value)
    )


def _failure(
    *,
    distribution_name: str,
    error_code: str,
    reason: str,
    adapter_id: str | None = None,
    cause: BaseException | None = None,
) -> ParserPluginDiscoveryFailure:
    details: dict[str, str] = {
        "distribution_name": distribution_name,
        "reason": reason,
    }
    if adapter_id is not None:
        details["adapter_id"] = adapter_id
    return ParserPluginDiscoveryFailure(
        distribution_name=distribution_name,
        adapter_id=adapter_id,
        error=ParserError(
            error_code=error_code,
            message="Parser plugin не прошёл безопасное discovery.",
            details=details,
            cause=cause,
        ),
    )


def _metadata_failure(
    distribution_name: str,
    reason: str,
    *,
    adapter_id: str | None = None,
    cause: BaseException | None = None,
) -> ParserPluginDiscoveryFailure:
    return _failure(
        distribution_name=distribution_name,
        adapter_id=adapter_id,
        error_code=_PLUGIN_METADATA_INVALID,
        reason=reason,
        cause=cause,
    )


def _read_distribution_identity(
    distribution: _DistributionSnapshot,
    expected_name: str,
) -> tuple[str, str] | ParserPluginDiscoveryFailure:
    raw_name = distribution.name
    raw_version = distribution.version

    if not _is_safe_distribution_metadata_name(raw_name, expected_name):
        return _metadata_failure(expected_name, "distribution_name_mismatch")
    if not _is_safe_version(raw_version):
        return _metadata_failure(expected_name, "distribution_version_format")
    return (expected_name, cast(str, raw_version))


def _read_entry_points(
    distribution: _DistributionSnapshot,
    distribution_name: str,
) -> tuple[object, ...] | ParserPluginDiscoveryFailure:
    try:
        iterator = iter(distribution.entry_points)
        entries = tuple(islice(iterator, _MAX_ENTRY_POINTS_PER_DISTRIBUTION + 1))
    except Exception as error:  # сторонний metadata provider не доверен
        return _failure(
            distribution_name=distribution_name,
            error_code=_PLUGIN_DISCOVERY_FAILED,
            reason="entry_enumeration_failed",
            cause=error,
        )

    if len(entries) > _MAX_ENTRY_POINTS_PER_DISTRIBUTION:
        return _failure(
            distribution_name=distribution_name,
            error_code=_PLUGIN_DISCOVERY_FAILED,
            reason="distribution_entry_limit_exceeded",
        )
    return entries


def _boundary_error(reason: str, *, metadata_invalid: bool = False) -> Never:
    raise _DistributionBoundaryError(
        error_code=(
            _PLUGIN_METADATA_INVALID if metadata_invalid else _PLUGIN_DISCOVERY_FAILED
        ),
        reason=reason,
    )


def _secure_metadata_flags() -> tuple[int, int]:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    nonblock = getattr(os, "O_NONBLOCK", None)
    if (
        not isinstance(no_follow, int)
        or not isinstance(directory, int)
        or not isinstance(nonblock, int)
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
    ):
        _boundary_error("secure_metadata_open_unavailable")
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    root_flags = os.O_RDONLY | directory | no_follow | close_on_exec
    file_flags = os.O_RDONLY | no_follow | nonblock | close_on_exec
    return root_flags, file_flags


def _metadata_root_path(distribution: metadata.PathDistribution) -> Path:
    raw_path: object = getattr(distribution, "_path", None)
    if not isinstance(raw_path, Path):
        _boundary_error("metadata_provider_unsupported")
    return raw_path


def _open_metadata_root(path: Path, flags: int) -> int:
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _boundary_error("metadata_path_unsafe")
    try:
        opened = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        _boundary_error("metadata_path_unsafe")
    if not stat.S_ISDIR(opened.st_mode):
        os.close(descriptor)
        _boundary_error("metadata_path_unsafe")
    return descriptor


def _read_bounded_metadata_file(
    root_descriptor: int,
    filename: str,
    *,
    file_flags: int,
    byte_limit: int,
    header_only: bool,
) -> bytes | None:
    descriptor: int | None = None
    try:
        try:
            expected = os.stat(
                filename,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        except OSError:
            _boundary_error("metadata_path_unsafe")
        if not stat.S_ISREG(expected.st_mode):
            _boundary_error("metadata_path_unsafe")
        if not header_only and expected.st_size > byte_limit:
            _boundary_error(
                "entry_points_size_limit"
                if filename == "entry_points.txt"
                else "distribution_metadata_size_limit"
            )

        try:
            descriptor = os.open(filename, file_flags, dir_fd=root_descriptor)
        except FileNotFoundError:
            _boundary_error("metadata_changed_during_read")
        except OSError:
            _boundary_error("metadata_path_unsafe")

        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or (expected.st_dev, expected.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            _boundary_error("metadata_changed_during_read")
        if not header_only and before.st_size > byte_limit:
            _boundary_error(
                "entry_points_size_limit"
                if filename == "entry_points.txt"
                else "distribution_metadata_size_limit"
            )

        content = bytearray()
        while len(content) <= byte_limit:
            remaining = byte_limit + 1 - len(content)
            chunk = os.read(
                descriptor,
                min(_METADATA_READ_CHUNK_BYTES, remaining),
            )
            if not chunk:
                break
            content.extend(chunk)
            if header_only:
                lf_end = content.find(b"\n\n")
                crlf_end = content.find(b"\r\n\r\n")
                endings = tuple(index for index in (lf_end, crlf_end) if index >= 0)
                if endings:
                    end = min(endings)
                    separator_size = 4 if end == crlf_end else 2
                    del content[end + separator_size :]
                    break
        if len(content) > byte_limit:
            _boundary_error(
                "distribution_metadata_size_limit"
                if header_only
                else "entry_points_size_limit"
            )

        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            _boundary_error("metadata_changed_during_read")
        return bytes(content)
    except OSError:
        _boundary_error("metadata_file_unreadable")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _parse_distribution_identity(raw_metadata: bytes) -> tuple[str, str]:
    try:
        text = raw_metadata.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _boundary_error("distribution_metadata_encoding", metadata_invalid=True)

    names: list[str] = []
    versions: list[str] = []
    current_field: str | None = None
    for line in text.splitlines():
        if not line:
            break
        if line[0].isspace():
            if current_field in {"name", "version"}:
                _boundary_error("distribution_metadata_format", metadata_invalid=True)
            continue
        field, separator, value = line.partition(":")
        if not separator:
            _boundary_error("distribution_metadata_format", metadata_invalid=True)
        current_field = field.casefold()
        if current_field == "name":
            names.append(value.strip())
        elif current_field == "version":
            versions.append(value.strip())
    if len(names) != 1 or len(versions) != 1:
        _boundary_error("distribution_metadata_format", metadata_invalid=True)
    return names[0], versions[0]


def _parse_entry_points_metadata(raw_entry_points: bytes | None) -> tuple[object, ...]:
    if raw_entry_points is None:
        return ()
    try:
        text = raw_entry_points.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _boundary_error("entry_points_encoding", metadata_invalid=True)

    group: str | None = None
    entries: list[object] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1]
            if not 1 <= len(group) <= 255:
                _boundary_error("entry_points_format", metadata_invalid=True)
            try:
                _safe_text(group)
            except ValueError:
                _boundary_error("entry_points_format", metadata_invalid=True)
            continue
        if group is None:
            _boundary_error("entry_points_format", metadata_invalid=True)
        name, separator, value = line.partition("=")
        if not separator:
            _boundary_error("entry_points_format", metadata_invalid=True)
        entries.append(
            _EntryPointMetadata(
                name=name.strip(),
                value=value.strip(),
                group=group,
            )
        )
        if len(entries) > _MAX_ENTRY_POINTS_PER_DISTRIBUTION:
            _boundary_error("distribution_entry_limit_exceeded")
    return tuple(entries)


def _resolve_distribution(distribution_name: str) -> _DistributionSnapshot:
    distribution = metadata.distribution(distribution_name)
    if type(distribution) is not metadata.PathDistribution:
        _boundary_error("metadata_provider_unsupported")

    root_path = _metadata_root_path(distribution)
    root_flags, file_flags = _secure_metadata_flags()
    root_descriptor = _open_metadata_root(root_path, root_flags)
    try:
        raw_metadata = _read_bounded_metadata_file(
            root_descriptor,
            "METADATA",
            file_flags=file_flags,
            byte_limit=_MAX_METADATA_HEADER_BYTES,
            header_only=True,
        )
        if raw_metadata is None:
            raw_metadata = _read_bounded_metadata_file(
                root_descriptor,
                "PKG-INFO",
                file_flags=file_flags,
                byte_limit=_MAX_METADATA_HEADER_BYTES,
                header_only=True,
            )
        if raw_metadata is None:
            _boundary_error("distribution_metadata_unreadable", metadata_invalid=True)
        raw_entry_points = _read_bounded_metadata_file(
            root_descriptor,
            "entry_points.txt",
            file_flags=file_flags,
            byte_limit=_MAX_ENTRY_POINTS_BYTES,
            header_only=False,
        )
    finally:
        os.close(root_descriptor)

    raw_name, raw_version = _parse_distribution_identity(raw_metadata)
    return _DistributionSnapshot(
        name=raw_name,
        version=raw_version,
        entry_points=_parse_entry_points_metadata(raw_entry_points),
    )


def _parse_entry_point(
    entry_point: object,
    *,
    distribution_name: str,
    distribution_version: str,
) -> _Candidate | ParserPluginDiscoveryFailure | None:
    checked = cast(_EntryPointMetadata, entry_point)
    try:
        raw_group = checked.group
    except Exception as error:  # property может принадлежать стороннему provider
        return _metadata_failure(
            distribution_name,
            "entry_group_unreadable",
            cause=error,
        )

    if type(raw_group) is not str:
        return _metadata_failure(distribution_name, "entry_group_type")
    if raw_group != PARSER_ENTRY_POINT_GROUP:
        return None

    try:
        raw_adapter_id = checked.name
        raw_target = checked.value
    except Exception as error:  # property может принадлежать стороннему provider
        return _metadata_failure(
            distribution_name,
            "entry_metadata_unreadable",
            cause=error,
        )

    if not _is_canonical_adapter_id(raw_adapter_id):
        return _metadata_failure(distribution_name, "adapter_id_format")
    adapter_id = cast(str, raw_adapter_id)
    if type(raw_target) is not str or len(raw_target) > 512:
        return _metadata_failure(
            distribution_name,
            "target_format",
            adapter_id=adapter_id,
        )
    target_match = _TARGET_PATTERN.fullmatch(raw_target)
    if target_match is None:
        return _metadata_failure(
            distribution_name,
            "target_format",
            adapter_id=adapter_id,
        )

    module = target_match.group("module")
    attribute = target_match.group("attribute")
    if len(module) > 255 or len(attribute) > 255:
        return _metadata_failure(
            distribution_name,
            "target_format",
            adapter_id=adapter_id,
        )

    try:
        descriptor = ParserPluginDescriptor(
            adapter_id=adapter_id,
            distribution_name=distribution_name,
            distribution_version=distribution_version,
            module=module,
            attribute=attribute,
            entry_point_group=PARSER_ENTRY_POINT_GROUP,
        )
    except (TypeError, ValueError, ValidationError) as error:
        return _metadata_failure(
            distribution_name,
            "descriptor_invalid",
            adapter_id=adapter_id,
            cause=error,
        )
    return _Candidate(descriptor=descriptor)


def _remove_duplicate_claims(
    candidates: list[_Candidate],
) -> tuple[list[_Candidate], list[ParserPluginDiscoveryFailure]]:
    failures: list[ParserPluginDiscoveryFailure] = []
    adapter_id_counts = Counter(
        candidate.descriptor.adapter_id for candidate in candidates
    )
    duplicate_adapter_ids = {
        adapter_id for adapter_id, count in adapter_id_counts.items() if count > 1
    }

    unique_names: list[_Candidate] = []
    for candidate in candidates:
        descriptor = candidate.descriptor
        if descriptor.adapter_id not in duplicate_adapter_ids:
            unique_names.append(candidate)
            continue
        failures.append(
            _failure(
                distribution_name=descriptor.distribution_name,
                adapter_id=descriptor.adapter_id,
                error_code=_DUPLICATE_REGISTRATION,
                reason="duplicate_adapter_id",
            )
        )

    target_counts = Counter(candidate.target for candidate in unique_names)
    duplicate_targets = {target for target, count in target_counts.items() if count > 1}
    unique_targets: list[_Candidate] = []
    for candidate in unique_names:
        descriptor = candidate.descriptor
        if candidate.target not in duplicate_targets:
            unique_targets.append(candidate)
            continue
        failures.append(
            _failure(
                distribution_name=descriptor.distribution_name,
                adapter_id=descriptor.adapter_id,
                error_code=_DUPLICATE_REGISTRATION,
                reason="duplicate_target",
            )
        )
    return unique_targets, failures


def _descriptor_key(
    candidate: _Candidate,
) -> tuple[str, str, str, str, str]:
    descriptor = candidate.descriptor
    return (
        descriptor.adapter_id,
        descriptor.distribution_name,
        descriptor.distribution_version,
        descriptor.module,
        descriptor.attribute,
    )


def _failure_key(
    failure: ParserPluginDiscoveryFailure,
) -> tuple[str, str, str, str, str]:
    reason = failure.error.details.get("reason")
    return (
        failure.distribution_name,
        failure.adapter_id or "",
        failure.error.error_code,
        reason if isinstance(reason, str) else "",
        failure.error.cause or "",
    )


def _validate_policy(policy: object) -> ParserDiscoveryPolicy:
    if type(policy) is not ParserDiscoveryPolicy:
        raise ParserError(
            error_code=_PLUGIN_DISCOVERY_FAILED,
            message="Parser discovery policy не прошла runtime validation.",
            details={"reason": "policy_type"},
        )
    validation_failure: ParserError | None = None
    try:
        payload = policy.model_dump(
            mode="python",
            round_trip=True,
            warnings="error",
        )
        validated = ParserDiscoveryPolicy.model_validate(payload, strict=True)
    except Exception as error:  # DTO мог быть forged через model_copy
        validation_failure = ParserError(
            error_code=_PLUGIN_DISCOVERY_FAILED,
            message="Parser discovery policy не прошла runtime validation.",
            details={"reason": "policy_schema"},
            cause=error,
        )
    if validation_failure is not None:
        raise validation_failure from None
    return validated


def discover_parser_plugins(
    policy: ParserDiscoveryPolicy,
) -> ParserPluginDiscoveryReport:
    """Найти allowlisted descriptors без импорта или выполнения plugin code.

    Args:
        policy: Exact ``ParserDiscoveryPolicy`` с непустым canonical allowlist
            distributions и пределом принятых descriptors.

    Returns:
        Deterministic report. Ошибка одного distribution или entry point
        возвращается как typed failure и не отменяет остальные валидные
        descriptors.

    Raises:
        ParserError: Policy имеет неверный runtime type, была forged либо не
            проходит повторную strict validation.

    Side Effects:
        Выполняет именованный lookup и bounded чтение filesystem metadata только
        для allowlisted distributions. Глобальное перечисление distributions,
        registration и activation не выполняются.

    Security:
        Принимается только exact group ``structuraguard.parsers`` и native
        ``PathDistribution`` с безопасным ``dir_fd``. Metadata files читаются
        без symlink, с hard byte/count limits и проверкой подмены. Функция не
        вызывает ``EntryPoint.load()``, не импортирует target module и не
        конструирует parser. Host-configured metadata finder остаётся частью
        доверенной composition environment.
    """

    policy = _validate_policy(policy)
    candidates: list[_Candidate] = []
    failures: list[ParserPluginDiscoveryFailure] = []

    for distribution_name in policy.allowed_distributions:
        try:
            distribution = _resolve_distribution(distribution_name)
        except _DistributionBoundaryError as error:
            failures.append(
                _failure(
                    distribution_name=distribution_name,
                    error_code=error.error_code,
                    reason=error.reason,
                    cause=error,
                )
            )
            continue
        except Exception as error:  # allowlisted distribution — внешняя граница
            reason = (
                "distribution_not_found"
                if isinstance(error, metadata.PackageNotFoundError)
                else "distribution_lookup_failed"
            )
            failures.append(
                _failure(
                    distribution_name=distribution_name,
                    error_code=_PLUGIN_DISCOVERY_FAILED,
                    reason=reason,
                    cause=error,
                )
            )
            continue

        identity = _read_distribution_identity(distribution, distribution_name)
        if isinstance(identity, ParserPluginDiscoveryFailure):
            failures.append(identity)
            continue
        canonical_name, distribution_version = identity

        entry_points = _read_entry_points(distribution, canonical_name)
        if isinstance(entry_points, ParserPluginDiscoveryFailure):
            failures.append(entry_points)
            continue

        for entry_point in entry_points:
            parsed = _parse_entry_point(
                entry_point,
                distribution_name=canonical_name,
                distribution_version=distribution_version,
            )
            if isinstance(parsed, _Candidate):
                candidates.append(parsed)
            elif isinstance(parsed, ParserPluginDiscoveryFailure):
                failures.append(parsed)

    unique_candidates, duplicate_failures = _remove_duplicate_claims(candidates)
    failures.extend(duplicate_failures)
    unique_candidates.sort(key=_descriptor_key)

    accepted = unique_candidates[: policy.max_entries]
    for candidate in unique_candidates[policy.max_entries :]:
        descriptor = candidate.descriptor
        failures.append(
            _failure(
                distribution_name=descriptor.distribution_name,
                adapter_id=descriptor.adapter_id,
                error_code=_PLUGIN_DISCOVERY_FAILED,
                reason="entry_limit_exceeded",
            )
        )

    return ParserPluginDiscoveryReport(
        descriptors=tuple(candidate.descriptor for candidate in accepted),
        failures=tuple(sorted(failures, key=_failure_key)),
    )


__all__ = (
    "PARSER_ENTRY_POINT_GROUP",
    "ParserPluginDiscoveryFailure",
    "ParserPluginDiscoveryReport",
    "discover_parser_plugins",
)
