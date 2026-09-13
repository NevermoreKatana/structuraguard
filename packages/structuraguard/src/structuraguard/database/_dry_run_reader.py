"""Закрытый EXISTS reader текущей transaction; режим задаёт владелец connection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from structuraguard.contracts.constraint_validation import (
    ConstraintMatch,
    ConstraintReadPolicy,
    ConstraintReadRequest,
    ConstraintReadResult,
)
from structuraguard.contracts.database import DatabaseCatalog, DatabaseMetadataSnapshot
from structuraguard.loading.projection import failure
from structuraguard.validation._rule_input import checked

from . import _constraint_queries as queries

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection


class SnapshotReader:
    def __init__(
        self, connection: AsyncConnection, policy: ConstraintReadPolicy
    ) -> None:
        self.connection = connection
        self.policy = policy
        self.queries = 0
        self.keys = 0

    async def read(
        self, request: ConstraintReadRequest, *, catalog: DatabaseCatalog
    ) -> ConstraintReadResult:
        request = checked(
            request,
            ConstraintReadRequest,
            self.policy.limits,
            code="DRY_RUN_INPUT_INVALID",
        )
        if (
            request.target_id,
            request.target_policy_fingerprint,
            request.database_fingerprint,
        ) != (
            catalog.target_id,
            catalog.target_policy_fingerprint,
            catalog.database_fingerprint,
        ):
            raise failure("DRY_RUN_READER_INVALID")
        self.keys += len(request.lookups)
        if self.keys > self.policy.limits.max_keys:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        tables = queries.checked_tables(
            DatabaseMetadataSnapshot(
                dialect=catalog.dialect,
                target_id=catalog.target_id,
                target_policy_fingerprint=catalog.target_policy_fingerprint,
                schemas=catalog.schemas,
                comments_supported=catalog.comments_supported is True,
            ),
            request,
            self.policy,
        )
        matches: list[ConstraintMatch] = []
        for chunk, statement in queries.statements(
            request, tables, self.policy.chunk_size, "postgresql"
        ):
            self.queries += 1
            if self.queries > self.policy.max_queries:
                raise failure("SECURITY_LIMIT_EXCEEDED")
            cursor = await self.connection.execute(statement)
            matches.extend(queries.matches(chunk, tuple(cursor.one())))
            cursor.close()
        return queries.result(request, tuple(matches))
