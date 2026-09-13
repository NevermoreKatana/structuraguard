"""Sandbox declarations принадлежат trusted host, не parser plugin."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt

from structuraguard.contracts._base import FrozenContract, canonical_sha256_value
from structuraguard.contracts.common import (
    FingerprintStr,
    SourceArtifactRef,
    VersionStr,
)
from structuraguard.contracts.plugins import ParserPluginDescriptor
from structuraguard.contracts.security import ParserFormat, SecurityLimits


class SandboxCapabilities(FrozenContract):
    """Все признаки обязательны; SDK не аттестует kernel/container configuration."""

    non_root: StrictBool = False
    read_only_filesystem: StrictBool = False
    no_network: StrictBool = False
    cpu_limit: StrictBool = False
    memory_limit: StrictBool = False
    pid_limit: StrictBool = False
    isolated_temp: StrictBool = False
    no_host_secrets: StrictBool = False
    no_docker_socket: StrictBool = False
    bounded_ipc: StrictBool = False
    terminate_and_reap: StrictBool = False

    @property
    def complete(self) -> bool:
        """Проверить, что host заявил все признаки; реальную изоляцию не аттестует."""
        return all(self.model_dump().values())


class SandboxParserSpec(FrozenContract):
    """Pinned executable artifact и entry point; host не импортирует descriptor."""

    descriptor: ParserPluginDescriptor
    artifact_fingerprint: FingerprintStr
    parser_version: VersionStr
    format_id: ParserFormat

    @property
    def fingerprint(self) -> str:
        """Связать descriptor/version/artifact/format hash без импорта plugin."""
        return canonical_sha256_value(self)


class SandboxPolicy(FrozenContract):
    """Allowlist spec fingerprints и конечные caps IPC/result/cleanup.

    Пустой allowed_specs запрещает запуск. Неверные поля дают Pydantic
    ValidationError; наличие policy не создаёт OS/container isolation."""

    allowed_specs: tuple[FingerprintStr, ...] = Field(default=(), max_length=64)
    max_frame_bytes: Annotated[StrictInt, Field(gt=0, le=1_048_576)] = 1_048_576
    max_result_bytes: Annotated[StrictInt, Field(gt=0, le=16_777_216)] = 8_388_608
    max_json_depth: Annotated[StrictInt, Field(gt=0, le=64)] = 48
    cleanup_time_ms: Annotated[StrictInt, Field(gt=0, le=30_000)] = 5000


class SandboxRequest(FrozenContract):
    """Bounded worker request без host paths, DB/LLM/audit handles.

    Source reference связывает identity/size; reader lease передаётся отдельным
    port. DTO не открывает источник и не импортирует plugin."""

    version: Literal[1] = 1
    run_id: UUID
    parser: SandboxParserSpec
    source: SourceArtifactRef
    source_size_bytes: Annotated[StrictInt, Field(ge=0, le=1_000_000_000)]
    limits: SecurityLimits
    output_policy: SandboxPolicy


class SandboxExit(FrozenContract):
    """Закрытый status и cleanup acknowledgement без stdout/stderr/команд."""

    status: Literal["completed", "failed", "terminated"]
    cleanup_complete: StrictBool
