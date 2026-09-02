"""Явная конфигурация публичного SDK."""

from pydantic import BaseModel, ConfigDict


class SDKConfig(BaseModel):
    """Пустая неизменяемая конфигурация SDK для M1.

    В M1 модель не содержит полей. Значения принимаются только явно:
    неизвестные поля отклоняются ``pydantic.ValidationError``. Создание
    объекта не читает переменные окружения и не выполняет I/O.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
