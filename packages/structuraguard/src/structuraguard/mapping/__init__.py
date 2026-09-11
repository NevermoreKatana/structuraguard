"""Локальное deterministic mapping без DB/LLM connection и генерации SQL."""

from .mapper import DeterministicMapper

__all__ = ("DeterministicMapper",)
