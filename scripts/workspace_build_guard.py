"""PEP 517 backend, запрещающий сборку виртуального корня workspace."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import NoReturn

type ConfigSettings = Mapping[str, str | Sequence[str]] | None

_ERROR_MESSAGE = (
    "Корень workspace не является Python distribution; "
    "собирайте packages/structuraguard"
)


class WorkspaceBuildError(RuntimeError):
    """Сборка виртуального корня workspace запрещена."""


def _reject() -> NoReturn:
    raise WorkspaceBuildError(_ERROR_MESSAGE)


def get_requires_for_build_sdist(
    config_settings: ConfigSettings = None,
) -> NoReturn:
    """Отклонить запрос build-зависимостей для root sdist."""
    _reject()


def build_sdist(
    sdist_directory: str,
    config_settings: ConfigSettings = None,
) -> NoReturn:
    """Отклонить сборку root sdist."""
    _reject()


def get_requires_for_build_wheel(
    config_settings: ConfigSettings = None,
) -> NoReturn:
    """Отклонить запрос build-зависимостей для root wheel."""
    _reject()


def prepare_metadata_for_build_wheel(
    metadata_directory: str,
    config_settings: ConfigSettings = None,
) -> NoReturn:
    """Отклонить подготовку metadata для root wheel."""
    _reject()


def build_wheel(
    wheel_directory: str,
    config_settings: ConfigSettings = None,
    metadata_directory: str | None = None,
) -> NoReturn:
    """Отклонить сборку root wheel."""
    _reject()


def get_requires_for_build_editable(
    config_settings: ConfigSettings = None,
) -> NoReturn:
    """Отклонить запрос build-зависимостей для editable root."""
    _reject()


def prepare_metadata_for_build_editable(
    metadata_directory: str,
    config_settings: ConfigSettings = None,
) -> NoReturn:
    """Отклонить подготовку metadata для editable root."""
    _reject()


def build_editable(
    wheel_directory: str,
    config_settings: ConfigSettings = None,
    metadata_directory: str | None = None,
) -> NoReturn:
    """Отклонить сборку editable root."""
    _reject()
