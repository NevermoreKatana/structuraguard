from __future__ import annotations

import asyncio
import threading
from importlib import metadata
from typing import Never, Protocol

import pytest

from structuraguard import AsyncStructuraGuard, SDKConfig, StructuraGuard
from structuraguard.parsers import ParserRegistry


class _ConfiguredFacade(Protocol):
    @property
    def config(self) -> SDKConfig: ...

    @property
    def parsers(self) -> ParserRegistry: ...


class _FacadeFactory(Protocol):
    def __call__(
        self,
        *,
        config: SDKConfig | None = None,
        parser_registry: ParserRegistry | None = None,
    ) -> _ConfiguredFacade: ...


FACADE_FACTORIES: tuple[_FacadeFactory, ...] = (
    AsyncStructuraGuard,
    StructuraGuard,
)


def _forbidden_runtime_creation(*args: object, **kwargs: object) -> Never:
    raise AssertionError("facade construction attempted to create a runtime resource")


@pytest.mark.parametrize("facade_factory", FACADE_FACTORIES)
def test_facade_uses_explicit_config(facade_factory: _FacadeFactory) -> None:
    config = SDKConfig()
    facade = facade_factory(config=config)

    assert facade.config is config


@pytest.mark.parametrize("facade_factory", FACADE_FACTORIES)
def test_facade_creates_an_instance_local_default_config(
    facade_factory: _FacadeFactory,
) -> None:
    first = facade_factory()
    second = facade_factory()

    first_config = first.config
    second_config = second.config
    assert isinstance(first_config, SDKConfig)
    assert isinstance(second_config, SDKConfig)
    assert first_config is not second_config


@pytest.mark.parametrize("facade_factory", FACADE_FACTORIES)
def test_facade_uses_explicit_parser_registry(
    facade_factory: _FacadeFactory,
) -> None:
    registry = ParserRegistry()

    facade = facade_factory(parser_registry=registry)

    assert facade.parsers is registry


@pytest.mark.parametrize("facade_factory", FACADE_FACTORIES)
def test_facade_creates_an_instance_local_default_parser_registry(
    facade_factory: _FacadeFactory,
) -> None:
    first = facade_factory()
    second = facade_factory()

    assert isinstance(first.parsers, ParserRegistry)
    assert isinstance(second.parsers, ParserRegistry)
    assert first.parsers is not second.parsers


@pytest.mark.parametrize("facade_factory", FACADE_FACTORIES)
def test_facade_construction_does_not_start_plugin_discovery(
    facade_factory: _FacadeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metadata, "distribution", _forbidden_runtime_creation)
    monkeypatch.setattr(metadata, "distributions", _forbidden_runtime_creation)
    monkeypatch.setattr(metadata, "entry_points", _forbidden_runtime_creation)
    monkeypatch.setattr(
        metadata.EntryPoint,
        "load",
        _forbidden_runtime_creation,
    )

    facade = facade_factory()

    assert isinstance(facade.parsers, ParserRegistry)


def test_sync_facade_is_not_an_async_facade_subclass() -> None:
    assert not issubclass(StructuraGuard, AsyncStructuraGuard)


def test_facade_construction_does_not_create_thread_or_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(asyncio, "new_event_loop", _forbidden_runtime_creation)
    monkeypatch.setattr(threading.Thread, "start", _forbidden_runtime_creation)

    AsyncStructuraGuard()
    StructuraGuard()


@pytest.mark.anyio
async def test_async_facade_construction_preserves_the_running_loop() -> None:
    loop_before = asyncio.get_running_loop()

    AsyncStructuraGuard()

    assert asyncio.get_running_loop() is loop_before
