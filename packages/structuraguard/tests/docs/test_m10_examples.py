"""Копируемый пример M10 использует FakeLLMProvider без сети и DB writes."""

import re
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "scenario", ["validated_decision", "metadata_only", "no_llm", "empty_scope"]
)
def test_m10_documented_example_is_copyable_offline(
    tmp_path: Path, scenario: str
) -> None:
    root = Path(__file__).resolve().parents[4]
    documentation = (root / "docs/llm-database-mapping.md").read_text()
    match = re.search(
        r"<!-- example:m10-proposal:start -->\s*```python\n(?P<code>.*?)\n```",
        documentation,
        flags=re.DOTALL,
    )
    assert match is not None
    guard = """
import sys

def deny_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise AssertionError('Сетевой доступ запрещён в примере M10')

sys.addaudithook(deny_network)
"""
    assertions = """
import asyncio
from tests.fakes.mapping import catalog as make_catalog, column, profile, table
from tests.fakes.semantic_mapping import RecordingScanner, decision_for, fake_provider, router_for
from structuraguard.contracts import DataClassification, LLMRoutingMode, PipelineStatus
from structuraguard.mapping import prepare_semantic_mapping

async def main():
    scenario = sys.argv[1]
    data = await profile()
    db = make_catalog(
        table('other' if scenario == 'empty_scope' else 'customers', column('email')),
        table('suppliers', column('email')),
    )
    before = data.canonical_json(), db.canonical_json()
    scope = MappingScope(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        allow=tuple(
            CatalogColumnRef(table_id=t.table_id, column_id=c.column_id)
            for s in db.schemas for t in s.tables for c in t.columns
            if (s.name, t.name, c.name) == ('public', 'customers', 'email')
        ),
    )
    prepared = await prepare_semantic_mapping(data, db, scope=scope)
    active = scenario not in {'no_llm', 'empty_scope'}
    provider = fake_provider(
        *(decision_for(g).canonical_json() for g in prepared.groups)
    ) if active else fake_provider()
    mode = LLMRoutingMode.NO_LLM if scenario == 'no_llm' else LLMRoutingMode.FIXED
    router, scanner = router_for(provider, mode=mode), RecordingScanner()
    result = await propose_email(
        data, db, router=router, scanner=scanner, run_id='mapping-run',
        options=SemanticMappingOptions(
            response_retention='metadata_only' if scenario == 'metadata_only' else 'validated_decision',
        ),
    )
    assert before == (data.canonical_json(), db.canonical_json())
    assert result.classification is DataClassification.RESTRICTED
    assert result.status is PipelineStatus.NEEDS_REVIEW
    assert provider.call_count == len(router.calls) == len(scanner.requests) == int(active)
    group = result.groups[0]
    if active:
        selected = {i for choice in group.choices for i in choice.selected_candidate_ids}
        targets = [c.mapping.target for c in group.candidates.columns if c.candidate_id in selected]
        assert targets == [CatalogColumnRef(table_id='public.customers', column_id='email')]
        assert result.action == 'confirm'
        assert (group.decision is None) == (scenario == 'metadata_only')
        assert group.calls[0].provider_id == 'fake'
        assert group.calls[0].prompt.fingerprint == group.prompt.fingerprint
        assert 'person@example.org' not in scanner.payloads[0]
        assert 'suppliers' not in scanner.payloads[0]
    else:
        assert group.decision is None
        assert ('LLM_DISABLED' if scenario == 'no_llm' else 'NO_ADMISSIBLE_CANDIDATES') in group.reasons
        assert result.action == ('confirm' if scenario == 'no_llm' else 'reject')
    assert set(result.safe_summary()) == {'groups', 'ambiguous', 'classification', 'status', 'action'}
    assert not any(s in str(result.safe_summary()) for s in ('person@example.org', 'customers', 'sha256:'))
    print('M10 example OK')

asyncio.run(main())
"""
    import_path = (
        f"import sys\nsys.path.insert(0, {str(root / 'packages/structuraguard')!r})\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            import_path + guard + match.group("code") + assertions,
            scenario,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == "M10 example OK\n"
