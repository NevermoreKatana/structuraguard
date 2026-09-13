"""Синтетические principals, часы и настоящий encrypted store для DLP tests."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from structuraguard.contracts.privacy import DetectionPolicy, PlaceholderMapPolicy
from structuraguard.security.classification import ContentProtector
from structuraguard.security.placeholder_store import EncryptedMemoryPlaceholderStore

RUN, WRITER, READER = UUID(int=1), UUID(int=2), UUID(int=3)


class Clock:
    seconds = 0.0

    def __call__(self) -> datetime:
        return datetime(2026, 9, 13, tzinfo=UTC) + timedelta(seconds=self.seconds)

    def monotonic(self) -> float:
        return self.seconds


def store_for(
    clock: Clock | None = None, **caps: int
) -> EncryptedMemoryPlaceholderStore:
    chosen = clock or Clock()
    return EncryptedMemoryPlaceholderStore(
        key=b"\x11" * 32,
        policy=PlaceholderMapPolicy(
            runs=(str(RUN),), writers=(str(WRITER),), readers=(str(READER),), **caps
        ),
        clock=chosen,
        monotonic=chosen.monotonic,
    )


def protector() -> ContentProtector:
    return ContentProtector(DetectionPolicy(redaction_mode="reversible"))
