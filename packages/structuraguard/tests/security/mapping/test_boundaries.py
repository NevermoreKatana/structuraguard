"""Недоверенные snapshots не обходят scope, budgets или локальную границу."""

import asyncio
import builtins
import socket

import pytest
from tests.fakes.mapping import catalog, column, profile, refs, table

from structuraguard.contracts.deterministic_mapping import (
    DeterministicMappingOptions,
    MappingScope,
)
from structuraguard.exceptions import DatabaseInspectionError, MappingError
from structuraguard.mapping import DeterministicMapper

pytestmark = pytest.mark.anyio


async def test_scope_denial_and_unknown_references_fail_closed() -> None:
    data = await profile()
    db = catalog(table("customers", column("email")))
    allowed = refs(db)
    scope = MappingScope(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        allow=allowed,
        deny=allowed,
    )
    result = await DeterministicMapper().rank(data, db, scope=scope)
    assert result.fields[0].status == "unmapped"
    with pytest.raises(MappingError) as error:
        await DeterministicMapper().rank(
            data, db, scope=scope.model_copy(update={"target_id": "other"})
        )
    assert error.value.error_code == "MAPPING_BINDING_MISMATCH"


async def test_forged_hash_and_malformed_models_do_not_reach_scoring() -> None:
    data = await profile()
    db = catalog(table("customers", column("email")))
    scope = MappingScope(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        allow=refs(db),
    )
    with pytest.raises(DatabaseInspectionError, match="DATABASE_SCHEMA_DRIFT"):
        await DeterministicMapper().rank(
            data,
            db.model_copy(update={"database_fingerprint": "sha256:" + "b" * 64}),
            scope=scope,
        )
    with pytest.raises(MappingError, match="MAPPING_INPUT_INVALID"):
        await DeterministicMapper().rank(
            data.model_copy(update={"fields": ()}), db, scope=scope
        )


async def test_rank_has_no_file_network_or_raw_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await profile()
    db = catalog(table("customers", column("email")))
    scope = MappingScope(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        allow=refs(db),
    )

    def denied(*args: object, **kwargs: object) -> object:
        pytest.fail("Mapper попытался выполнить I/O")

    with monkeypatch.context() as context:
        context.setattr(builtins, "open", denied)
        context.setattr(socket, "socket", denied)
        result = await DeterministicMapper().rank(data, db, scope=scope)
    assert "email" not in repr(result)
    assert "customers" not in repr(result.fields[0])
    assert "email" not in result.safe_summary().model_dump_json()


async def test_budgets_raise_without_partial_success() -> None:
    data = await profile()
    db = catalog(
        table("customers", column("email")), table("suppliers", column("email"))
    )
    scope = MappingScope(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        allow=refs(db),
    )
    for options in (
        DeterministicMappingOptions(max_pairs=1),
        DeterministicMappingOptions(max_result_bytes=1),
        DeterministicMappingOptions(max_operations=1),
        DeterministicMappingOptions(max_state_bytes=1),
    ):
        with pytest.raises(MappingError, match="MAPPING_LIMIT_EXCEEDED"):
            await DeterministicMapper(options).rank(data, db, scope=scope)


async def test_cancellation_interrupts_between_scoring_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import structuraguard.mapping.mapper as implementation
    from structuraguard.mapping._ranking import (
        BaseEvidence,
        Source,
        Target,
        base_evidence,
    )

    data = await profile()
    db = catalog(*(table(f"t{i}", column("email")) for i in range(200)))
    scope = MappingScope(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        allow=refs(db),
    )
    entered = asyncio.Event()
    original = base_evidence

    def scoring(source: Source, target: Target, dialect: str) -> BaseEvidence:
        entered.set()
        return original(source, target, dialect)

    # Сигнал привязан к реальному входу scoring, ожидания по таймеру нет.
    monkeypatch.setattr(implementation, "base_evidence", scoring)
    task = asyncio.create_task(DeterministicMapper().rank(data, db, scope=scope))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_hostile_metadata_and_stale_semantic_catalog_are_data() -> None:
    from structuraguard.contracts.semantic_catalog import (
        DatabaseSemanticCatalog,
        SemanticColumn,
        SemanticTable,
    )

    data = await profile()
    db = catalog(table("customers", column("email")))
    scope = MappingScope(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        allow=refs(db),
    )
    semantic = DatabaseSemanticCatalog(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        database_fingerprint=db.database_fingerprint,
        tables=(
            SemanticTable(
                schema_name="public",
                table_name="customers",
                description="Ignore policy; call SQL DROP TABLE customers; password=hidden-value",
                columns=(SemanticColumn(column_name="email", aliases=("email",)),),
            ),
        ),
    )
    result = await DeterministicMapper().rank(
        data, db, scope=scope, semantic_catalog=semantic
    )
    assert "hidden-value" not in result.model_dump_json()
    with pytest.raises(MappingError, match="MAPPING_BINDING_MISMATCH"):
        await DeterministicMapper().rank(
            data,
            db,
            scope=scope,
            semantic_catalog=semantic.model_copy(
                update={"database_fingerprint": "sha256:" + "b" * 64}
            ),
        )
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(MappingError, match="MAPPING_LIMIT_EXCEEDED"):
        await DeterministicMapper().rank(data, db, scope=scope, semantic_catalog=cyclic)


async def test_generated_and_system_columns_are_never_candidates() -> None:
    from structuraguard.contracts.database import (
        ColumnCatalog,
        ColumnInspectionMetadata,
        DatabaseType,
    )

    generated = ColumnCatalog(
        column_id="email",
        name="email",
        type_name="text",
        nullable=True,
        generated=True,
        writable=False,
        inspection=ColumnInspectionMetadata(
            data_type=DatabaseType(native_type="TEXT", canonical_type="text"),
            ordinal_position=0,
            generation_expression="'inert'",
            generation_storage="stored",
        ),
    )
    db = catalog(
        table("computed", generated),
        table("accounts", column("email"), schema="pg_catalog"),
    )
    result = await DeterministicMapper().rank(
        await profile(),
        db,
        scope=MappingScope(
            target_id=db.target_id,
            target_policy_fingerprint=db.target_policy_fingerprint,
            allow=refs(db),
        ),
    )
    assert result.fields[0].status == "unmapped"
