"""Подписи, lifecycle sync facade и отделение observer failures от COMMIT."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from tests.fakes.pipeline import FakeDatabase, FakeParser, defaults, engine, request

from structuraguard import StructuraGuard
from structuraguard.contracts import PipelineStatus as S
from structuraguard.parsers import ParserRegistry
from structuraguard.pipeline.session import opaque_id
from structuraguard.security.audit import AuditChain, HMACAuditSigner, MemoryAuditKeys
from structuraguard.security.audit_store import MemoryAuditChainStore
from structuraguard.security.session import SecuritySession


@pytest.mark.anyio
async def test_signed_audit_references_verify_against_independent_anchor() -> None:
    store = MemoryAuditChainStore()
    key = uuid4()
    chains: list[AuditChain] = []

    def factory(resources: SecuritySession) -> AuditChain:
        chain = AuditChain(
            chain_id=uuid4(),
            run_id=resources.run_id,
            key_id=key,
            policy_fingerprint=resources.policy.fingerprint,
            signer=HMACAuditSigner(MemoryAuditKeys({key: b"k" * 32})),
            store=store,
        )
        chains.append(chain)
        return chain

    result = await engine(
        dependencies=replace(defaults(FakeDatabase()), audit=factory, actor_id=uuid4())
    ).ingest(request(), dry_run=True)
    assert result.status is S.COMPLETED, result.errors
    assert len(result.audit_references) == 4
    verification = await chains[0].verify(expected_head=result.audit_references[-1])
    assert verification.anchored and verification.count == 4


def test_sync_context_closes_owned_source_and_parser() -> None:
    parser = FakeParser()
    registry = ParserRegistry()
    registry.register(parser)
    with StructuraGuard(
        parser_registry=registry, dependencies=defaults(FakeDatabase())
    ) as sdk:
        source = sdk.inspect_source(request())
        plan = sdk.create_parse_plan(source)
        assert plan
        normalized = sdk.parse_semantically(source, plan=plan)
        mapping = sdk.create_mapping_plan(normalized)
        assert mapping.plan
        result = sdk.execute(normalized, plan=mapping.plan, dry_run=True)
        assert result.status is S.COMPLETED
    assert source._run.closed
    assert parser.probes == parser.parses == 1 and parser.closed


def test_nonce_generation_preserves_audit_pii_guard() -> None:
    # Валидная UUID с Luhn-последовательностью должна быть отброшена до run.
    bad = UUID("aaaaaaaa-aaaa-4aaa-4111-111111111111")
    good = UUID("aaaaaaaa-bbbb-4ccc-addd-eeeeeeeeeeee")
    values = iter((bad, good))
    assert opaque_id(replace(defaults(), new_id=lambda: next(values))) == good
