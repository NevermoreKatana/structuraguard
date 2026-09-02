from __future__ import annotations

import os
from typing import Never

import pytest
from pydantic import ValidationError

from structuraguard import SDKConfig


def _forbidden_getenv(key: str, default: str | None = None) -> Never:
    raise AssertionError(f"SDKConfig attempted to read environment variable {key!r}")


def test_config_is_an_explicit_empty_contract() -> None:
    config = SDKConfig()

    assert SDKConfig.model_fields == {}
    assert config.model_dump() == {}


def test_config_model_contract_is_frozen_and_forbids_extra_fields() -> None:
    assert SDKConfig.model_config.get("frozen") is True
    assert SDKConfig.model_config.get("extra") == "forbid"


def test_config_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SDKConfig.model_validate({"unexpected": True})


def test_config_is_frozen() -> None:
    config = SDKConfig()

    with pytest.raises(ValidationError):
        setattr(config, "unexpected", True)


def test_config_construction_does_not_read_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "getenv", _forbidden_getenv)

    assert SDKConfig().model_dump() == {}
