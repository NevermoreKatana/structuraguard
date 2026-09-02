from __future__ import annotations

import _thread
import asyncio
import importlib
import importlib.machinery
import importlib.util
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, MutableMapping
from pathlib import Path
from types import FrameType
from typing import Never, cast


class ForbiddenSideEffect(RuntimeError):
    """Побочный эффект, запрещённый во время импорта SDK."""


class _ForbiddenEnvironment(MutableMapping[str, str]):
    def _raise(self, operation: str) -> Never:
        raise ForbiddenSideEffect(f"environment access: {operation}")

    def __getitem__(self, key: str) -> str:
        self._raise(f"read {key!r}")

    def __setitem__(self, key: str, value: str) -> None:
        self._raise(f"write {key!r}")

    def __delitem__(self, key: str) -> None:
        self._raise(f"delete {key!r}")

    def __iter__(self) -> Iterator[str]:
        self._raise("iterate")

    def __len__(self) -> int:
        self._raise("length")


class _ForbiddenBytesEnvironment(MutableMapping[bytes, bytes]):
    def _raise(self, operation: str) -> Never:
        raise ForbiddenSideEffect(f"environment bytes access: {operation}")

    def __getitem__(self, key: bytes) -> bytes:
        self._raise(f"read {key!r}")

    def __setitem__(self, key: bytes, value: bytes) -> None:
        self._raise(f"write {key!r}")

    def __delitem__(self, key: bytes) -> None:
        self._raise(f"delete {key!r}")

    def __iter__(self) -> Iterator[bytes]:
        self._raise("iterate")

    def __len__(self) -> int:
        self._raise("length")


type NamedLoggerSnapshot = tuple[
    str,
    int,
    int | None,
    int | None,
    bool | None,
    bool | None,
    tuple[int, ...],
    tuple[int, ...],
]
type HandlerSnapshot = tuple[
    int,
    int,
    int | None,
    tuple[int, ...],
    bool | None,
]
type LoggerSnapshot = tuple[
    int,
    bool,
    bool,
    int,
    tuple[int, ...],
    tuple[int, ...],
    tuple[HandlerSnapshot, ...],
    tuple[NamedLoggerSnapshot, ...],
    int,
    int,
    int,
    tuple[tuple[str, int], ...],
    bool,
    bool,
    bool,
    bool,
]


_PROCESS_AUDIT_PREFIXES = (
    "socket.",
    "subprocess.",
    "os.system",
    "os.fork",
    "os.posix_spawn",
    "os.spawn",
)
_FILE_MUTATION_AUDIT_EVENTS = frozenset(
    {
        "os.chmod",
        "os.chown",
        "os.link",
        "os.mkdir",
        "os.remove",
        "os.removexattr",
        "os.rename",
        "os.rmdir",
        "os.setxattr",
        "os.symlink",
        "os.truncate",
        "os.utime",
        "shutil.copyfile",
    }
)
_FILE_READ_AUDIT_EVENTS = frozenset({"os.listdir", "os.scandir"})


def _fail(
    boundary: object = "forbidden side effect",
    *args: object,
    **kwargs: object,
) -> Never:
    raise ForbiddenSideEffect(str(boundary))


def _package_import_paths() -> frozenset[str]:
    specification = importlib.util.find_spec("structuraguard")
    if specification is None or specification.origin is None:
        raise ForbiddenSideEffect("structuraguard import location is unavailable")
    package_directory = Path(specification.origin).resolve().parent
    allowed: set[str] = set()
    for source_path in package_directory.iterdir():
        if source_path.suffix.casefold() not in {
            ".py",
            ".pyc",
            ".so",
            ".pyd",
            ".dylib",
        }:
            continue
        allowed.add(os.path.realpath(source_path))
        if source_path.suffix.casefold() == ".py":
            allowed.add(
                os.path.realpath(importlib.util.cache_from_source(str(source_path)))
            )
    return frozenset(allowed)


def _normalized_guard_path(path: object, *, current_directory: str) -> str | None:
    if isinstance(path, int) or not isinstance(path, str | bytes | os.PathLike):
        return None
    decoded_path = os.fsdecode(path)
    if not os.path.isabs(decoded_path):
        decoded_path = os.path.join(current_directory, decoded_path)
    return os.path.normcase(os.path.normpath(decoded_path))


def _called_directly_by_import_machinery() -> bool:
    guard_frame_names = {
        "_audit_guard",
        "_called_directly_by_import_machinery",
        "_is_allowed_import_open",
        "guard_audit_event",
        "guarded",
    }
    frame: FrameType | None = sys._getframe(1)
    while (
        frame is not None
        and frame.f_globals is globals()
        and frame.f_code.co_name in guard_frame_names
    ):
        frame = frame.f_back
    return frame is not None and frame.f_code.co_filename in {
        "<frozen importlib._bootstrap>",
        "<frozen importlib._bootstrap_external>",
    }


def _allowed_import_metadata_paths(import_paths: frozenset[str]) -> frozenset[str]:
    allowed: set[str] = set(import_paths)
    for raw_path in import_paths:
        path = Path(raw_path)
        package_directory = path.parent
        if package_directory.name == "__pycache__":
            package_directory = package_directory.parent
        if path.suffix == ".py":
            for suffix in importlib.machinery.EXTENSION_SUFFIXES:
                allowed.add(os.fspath(path.with_name(f"{path.stem}{suffix}")))
        allowed.add(os.fspath(path.parent))
        allowed.add(os.fspath(package_directory))
        allowed.add(os.fspath(package_directory.parent))
    return frozenset(allowed)


def _allowed_import_directory_paths(import_paths: frozenset[str]) -> frozenset[str]:
    allowed: set[str] = set()
    for raw_path in import_paths:
        path = Path(raw_path)
        directory = path.parent
        if directory.name == "__pycache__":
            directory = directory.parent
        allowed.add(os.fspath(directory))
    return frozenset(allowed)


def _is_allowed_import_open(
    path: object,
    mode: object,
    flags: object,
    remaining_import_paths: set[str],
    current_directory: str,
) -> bool:
    if not _called_directly_by_import_machinery():
        return False
    if isinstance(mode, str) and any(flag in mode for flag in "wax+"):
        return False
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    if isinstance(flags, int) and flags & write_flags:
        return False
    normalized_path = _normalized_guard_path(path, current_directory=current_directory)
    if normalized_path is None or normalized_path not in remaining_import_paths:
        return False
    remaining_import_paths.remove(normalized_path)
    return True


def _audit_guard(
    event: str,
    args: tuple[object, ...],
    remaining_import_paths: set[str],
    current_directory: str,
    allowed_directory_paths: frozenset[str],
) -> None:
    if event == "open":
        path = args[0] if args else None
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else None
        if not _is_allowed_import_open(
            path,
            mode,
            flags,
            remaining_import_paths,
            current_directory,
        ):
            raise ForbiddenSideEffect(f"file access: {path!r}")
        return
    if event in _FILE_READ_AUDIT_EVENTS:
        path = args[0] if args else None
        normalized_path = _normalized_guard_path(
            path,
            current_directory=current_directory,
        )
        if normalized_path not in allowed_directory_paths:
            raise ForbiddenSideEffect(f"filesystem metadata read: {event}")
        return
    if event in _FILE_MUTATION_AUDIT_EVENTS:
        raise ForbiddenSideEffect(f"file mutation: {event}")
    if event.startswith(_PROCESS_AUDIT_PREFIXES):
        raise ForbiddenSideEffect(f"process or network access: {event}")


def _guarded_metadata_reader(
    operation: str,
    original: Callable[..., object],
    allowed_paths: frozenset[str],
    current_directory: str,
    *,
    allow_any_import_path: bool = False,
) -> Callable[..., object]:
    def guarded(path: object, *args: object, **kwargs: object) -> object:
        normalized_path = _normalized_guard_path(
            path,
            current_directory=current_directory,
        )
        allowed_path = normalized_path is not None and normalized_path in allowed_paths
        if not _called_directly_by_import_machinery() or not (
            allowed_path or allow_any_import_path
        ):
            raise ForbiddenSideEffect(
                f"filesystem metadata read: {operation} path={path!r}"
            )
        return original(path, *args, **kwargs)

    return guarded


def _signal_snapshot() -> dict[int, object]:
    snapshot: dict[int, object] = {}
    for member in signal.Signals:
        try:
            snapshot[member.value] = signal.getsignal(member)
        except (OSError, RuntimeError, ValueError):
            continue
    return snapshot


def _named_logger_snapshot() -> tuple[NamedLoggerSnapshot, ...]:
    root = logging.getLogger()
    snapshot: list[NamedLoggerSnapshot] = []
    for name, value in root.manager.loggerDict.items():
        if isinstance(value, logging.Logger):
            snapshot.append(
                (
                    name,
                    id(value),
                    id(value.parent) if value.parent is not None else None,
                    value.level,
                    value.disabled,
                    value.propagate,
                    tuple(id(handler) for handler in value.handlers),
                    tuple(id(filter_) for filter_ in value.filters),
                )
            )
            continue
        snapshot.append((name, id(value), None, None, None, None, (), ()))
    return tuple(sorted(snapshot))


def _handler_snapshot() -> tuple[HandlerSnapshot, ...]:
    root = logging.getLogger()
    handlers = {id(handler): handler for handler in root.handlers}
    for value in root.manager.loggerDict.values():
        if isinstance(value, logging.Logger):
            handlers.update({id(handler): handler for handler in value.handlers})
    if logging.lastResort is not None:
        handlers[id(logging.lastResort)] = logging.lastResort

    snapshot: list[HandlerSnapshot] = []
    for handler_id, handler in handlers.items():
        closed = handler.__dict__.get("_closed")
        snapshot.append(
            (
                handler_id,
                handler.level,
                id(handler.formatter) if handler.formatter is not None else None,
                tuple(id(filter_) for filter_ in handler.filters),
                closed if isinstance(closed, bool) else None,
            )
        )
    return tuple(sorted(snapshot))


def _logger_snapshot() -> LoggerSnapshot:
    root = logging.getLogger()
    return (
        root.level,
        root.disabled,
        root.propagate,
        root.manager.disable,
        tuple(id(handler) for handler in root.handlers),
        tuple(id(filter_) for filter_ in root.filters),
        _handler_snapshot(),
        _named_logger_snapshot(),
        id(logging.lastResort),
        id(logging.getLogRecordFactory()),
        id(logging.getLoggerClass()),
        tuple(sorted(logging.getLevelNamesMapping().items())),
        logging.raiseExceptions,
        logging.logThreads,
        logging.logProcesses,
        logging.logMultiprocessing,
    )


def _thread_snapshot() -> tuple[tuple[int | None, str, bool], ...]:
    return tuple(
        sorted(
            (thread.ident, thread.name, thread.daemon)
            for thread in threading.enumerate()
        )
    )


def _assert_no_running_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise ForbiddenSideEffect("a running event loop appeared during import")


def _install_guards(
    allowed_import_paths: frozenset[str],
) -> tuple[
    object,
    LoggerSnapshot,
    dict[int, object],
    tuple[tuple[int | None, str, bool], ...],
]:
    _assert_no_running_loop()
    event_loop_policy = asyncio.get_event_loop_policy()
    logger_before = _logger_snapshot()
    signals_before = _signal_snapshot()
    threads_before = _thread_snapshot()
    current_directory = os.getcwd()
    allowed_metadata_paths = _allowed_import_metadata_paths(allowed_import_paths)
    allowed_directory_paths = _allowed_import_directory_paths(allowed_import_paths)

    remaining_import_paths = set(allowed_import_paths)

    def guard_audit_event(event: str, args: tuple[object, ...]) -> None:
        _audit_guard(
            event,
            args,
            remaining_import_paths,
            current_directory,
            allowed_directory_paths,
        )

    sys.addaudithook(guard_audit_event)

    setattr(os, "environ", _ForbiddenEnvironment())
    setattr(os, "getenv", _fail)
    if hasattr(os, "environb"):
        setattr(os, "environb", _ForbiddenBytesEnvironment())
    if hasattr(os, "getenvb"):
        setattr(os, "getenvb", _fail)
    low_level_os = importlib.import_module(os.name)
    if hasattr(low_level_os, "environ"):
        setattr(low_level_os, "environ", _ForbiddenBytesEnvironment())
    for name in ("access", "lstat", "stat"):
        if not hasattr(low_level_os, name):
            continue
        guarded = _guarded_metadata_reader(
            name,
            getattr(low_level_os, name),
            allowed_metadata_paths,
            current_directory,
            allow_any_import_path=True,
        )
        setattr(low_level_os, name, guarded)
        setattr(os, name, guarded)
    for name in ("listdir", "scandir"):
        if not hasattr(low_level_os, name):
            continue
        guarded = _guarded_metadata_reader(
            name,
            getattr(low_level_os, name),
            allowed_directory_paths,
            current_directory,
        )
        setattr(low_level_os, name, guarded)
        setattr(os, name, guarded)
    for name in (
        "chdir",
        "chflags",
        "chmod",
        "chown",
        "fchdir",
        "fchmod",
        "fchown",
        "fstat",
        "ftruncate",
        "getxattr",
        "lchflags",
        "lchown",
        "link",
        "listxattr",
        "makedirs",
        "mkdir",
        "mkfifo",
        "mknod",
        "putenv",
        "readlink",
        "remove",
        "removedirs",
        "removexattr",
        "rename",
        "renames",
        "replace",
        "rmdir",
        "setxattr",
        "symlink",
        "system",
        "popen",
        "fork",
        "forkpty",
        "posix_spawn",
        "posix_spawnp",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "truncate",
        "umask",
        "unlink",
        "unsetenv",
        "utime",
    ):
        if hasattr(os, name):
            setattr(os, name, _fail)
        if hasattr(low_level_os, name):
            setattr(low_level_os, name, _fail)

    setattr(socket, "socket", _fail)
    setattr(socket, "socketpair", _fail)
    setattr(socket, "create_connection", _fail)
    setattr(socket, "getaddrinfo", _fail)

    for name in ("Popen", "run", "call", "check_call", "check_output"):
        setattr(subprocess, name, _fail)

    setattr(threading.Thread, "start", _fail)
    setattr(_thread, "start_new_thread", _fail)

    setattr(asyncio, "new_event_loop", _fail)
    setattr(asyncio, "set_event_loop", _fail)
    setattr(asyncio, "set_event_loop_policy", _fail)
    setattr(asyncio, "run", _fail)
    setattr(event_loop_policy, "new_event_loop", _fail)
    setattr(event_loop_policy, "set_event_loop", _fail)

    setattr(signal, "signal", _fail)

    root = logging.getLogger()
    setattr(logging, "basicConfig", _fail)
    setattr(logging, "captureWarnings", _fail)
    setattr(logging, "disable", _fail)
    setattr(logging, "addLevelName", _fail)
    setattr(logging, "setLogRecordFactory", _fail)
    setattr(logging, "setLoggerClass", _fail)
    setattr(logging, "shutdown", _fail)
    for name in (
        "_log",
        "addFilter",
        "addHandler",
        "callHandlers",
        "critical",
        "debug",
        "error",
        "exception",
        "handle",
        "info",
        "log",
        "removeFilter",
        "removeHandler",
        "setLevel",
        "warning",
    ):
        setattr(logging.Logger, name, _fail)
    for name in (
        "addFilter",
        "emit",
        "flush",
        "handle",
        "removeFilter",
        "setFormatter",
        "setLevel",
    ):
        setattr(logging.Handler, name, _fail)
    setattr(root, "addHandler", _fail)
    setattr(root, "removeHandler", _fail)
    setattr(root, "setLevel", _fail)

    return event_loop_policy, logger_before, signals_before, threads_before


def _assert_snapshots_unchanged(
    *,
    event_loop_policy: object,
    logger_before: LoggerSnapshot,
    signals_before: dict[int, object],
    threads_before: tuple[tuple[int | None, str, bool], ...],
) -> None:
    _assert_no_running_loop()
    if asyncio.get_event_loop_policy() is not event_loop_policy:
        raise ForbiddenSideEffect("event loop policy changed during import")
    if _logger_snapshot() != logger_before:
        raise ForbiddenSideEffect("root logger changed during import")
    if _signal_snapshot() != signals_before:
        raise ForbiddenSideEffect("signal handlers changed during import")
    if _thread_snapshot() != threads_before:
        raise ForbiddenSideEffect("thread set changed during import")


def _expect_forbidden(operation: Callable[[], object], *, boundary: str) -> None:
    try:
        operation()
    except ForbiddenSideEffect:
        return
    raise ForbiddenSideEffect(f"guard self-test failed: {boundary}")


def _assert_guards_active(allowed_import_paths: frozenset[str]) -> None:
    def expect_os_operation_forbidden(name: str, *args: object) -> None:
        operation = os.__dict__.get(name)
        if operation is None:
            return
        callable_operation = cast(Callable[..., object], operation)
        _expect_forbidden(
            lambda: callable_operation(*args),
            boundary=f"os.{name}",
        )

    _expect_forbidden(lambda: os.getenv("PATH"), boundary="os.getenv")
    if hasattr(os, "environb"):
        _expect_forbidden(
            lambda: os.environb.get(b"PATH"),
            boundary="os.environb",
        )
    low_level_os = importlib.import_module(os.name)
    low_level_environ = getattr(low_level_os, "environ", None)
    if isinstance(low_level_environ, MutableMapping):
        _expect_forbidden(
            lambda: low_level_environ.get(b"PATH"),
            boundary=f"{os.name}.environ",
        )
    _expect_forbidden(lambda: os.putenv("SG_PROBE", "1"), boundary="os.putenv")
    _expect_forbidden(lambda: os.listdir("."), boundary="os.listdir")
    _expect_forbidden(lambda: os.stat("."), boundary="os.stat")
    _expect_forbidden(lambda: os.readlink("."), boundary="os.readlink")
    expect_os_operation_forbidden("setxattr", ".", "user.sg_probe", b"1")
    expect_os_operation_forbidden("mkfifo", "sg-import-probe.fifo")
    expect_os_operation_forbidden("umask", 0)
    _expect_forbidden(
        lambda: (Path.cwd() / "not-an-import.py").read_bytes(),
        boundary="arbitrary .py read",
    )
    allowed_source_path = next(
        path for path in sorted(allowed_import_paths) if Path(path).suffix == ".py"
    )
    bootstrap = importlib.import_module("importlib._bootstrap")
    call_with_frames_removed = cast(
        Callable[[Callable[[], bytes]], bytes],
        bootstrap.__dict__["_call_with_frames_removed"],
    )
    _expect_forbidden(
        lambda: call_with_frames_removed(Path(allowed_source_path).read_bytes),
        boundary="direct package source read under import machinery",
    )
    _expect_forbidden(lambda: logging.disable(), boundary="logging.disable")
    _expect_forbidden(
        lambda: logging.setLogRecordFactory(logging.LogRecord),
        boundary="logging.setLogRecordFactory",
    )
    _expect_forbidden(
        lambda: logging.getLogger().warning("guard probe"),
        boundary="logging emission",
    )
    _expect_forbidden(logging.shutdown, boundary="logging.shutdown")


def _prepare_attribution_dependencies() -> None:
    pydantic = importlib.import_module("pydantic")
    type(
        "_AttributionProbeModel",
        (pydantic.BaseModel,),
        {
            "__module__": __name__,
            "model_config": pydantic.ConfigDict(),
        },
    )

    plugin_loader = importlib.import_module("pydantic.plugin._loader")
    setattr(plugin_loader, "get_plugins", lambda: ())


def run_probe(mode: str) -> None:
    resolve_exports = mode == "attribution"
    if mode == "attribution":
        _prepare_attribution_dependencies()
    elif mode != "black-box":
        raise ValueError(f"unknown probe mode: {mode}")

    for module_name in tuple(sys.modules):
        if module_name == "structuraguard" or module_name.startswith("structuraguard."):
            del sys.modules[module_name]

    allowed_import_paths = _package_import_paths()
    policy, logger, signals, threads = _install_guards(allowed_import_paths)
    _assert_guards_active(allowed_import_paths)
    module = importlib.import_module("structuraguard")
    if resolve_exports:
        for export_name in module.__all__:
            getattr(module, export_name)
        config = module.SDKConfig()
        async_sdk = module.AsyncStructuraGuard(config=config)
        sync_sdk = module.StructuraGuard(config=config)
        if async_sdk.config is not config or sync_sdk.config is not config:
            raise ForbiddenSideEffect("facade did not preserve explicit SDKConfig")
        default_async_sdk = module.AsyncStructuraGuard()
        default_sync_sdk = module.StructuraGuard()
        if default_async_sdk.config is default_sync_sdk.config:
            raise ForbiddenSideEffect("facades share a default SDKConfig instance")
    elif "pydantic" in sys.modules:
        raise ForbiddenSideEffect("plain import eagerly loaded Pydantic")
    _assert_snapshots_unchanged(
        event_loop_policy=policy,
        logger_before=logger,
        signals_before=signals,
        threads_before=threads,
    )


def main(arguments: list[str]) -> int:
    if len(arguments) not in {1, 2}:
        print(
            "usage: import_probe.py {black-box|attribution} [source-root]",
            file=sys.stderr,
        )
        return 2
    if len(arguments) == 2:
        sys.path.insert(0, str(Path(arguments[1]).resolve()))
    try:
        run_probe(arguments[0])
    except ForbiddenSideEffect as error:
        print(f"forbidden import side effect: {error}", file=sys.stderr)
        return 1
    print(f"import probe passed: {arguments[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
