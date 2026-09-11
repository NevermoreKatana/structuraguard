"""Копируемые M9 snippets исполняются отдельно, с запретом сетевого доступа."""

import re
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("example", "scenario"),
    [
        ("m09-rank", "allowed"),
        ("m09-rank", "missing"),
        ("m09-aliases", "Контрагент"),
        ("m09-aliases", "Customer"),
    ],
)
def test_m09_documented_example_is_copyable_offline(
    tmp_path: Path, example: str, scenario: str
) -> None:
    root = Path(__file__).resolve().parents[4]
    documentation = (root / "docs/deterministic-mapping.md").read_text()
    match = re.search(
        rf"<!-- example:{example}:start -->\s*```python\n(?P<code>.*?)\n```",
        documentation,
        flags=re.DOTALL,
    )
    assert match is not None
    guard = """
import sys

def deny_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise AssertionError('Сетевой доступ запрещён в примере M9')

sys.addaudithook(deny_network)
"""
    assertions = """
import asyncio
from tests.fakes.mapping import catalog as make_catalog, column, profile, scope_for, table
from structuraguard.contracts.common import StringScalar
from structuraguard.mapping import DeterministicMapper

async def main():
    scenario = sys.argv[1]
    if scenario in {'allowed', 'missing'}:
        db = make_catalog(
            table('customers' if scenario == 'allowed' else 'other', column('email')),
            table('suppliers', column('email')),
        )
        data = await profile()
        before = data.canonical_json(), db.canonical_json()
        result = await rank_email(data, db)
        field = result.fields[0]
        if scenario == 'allowed':
            assert len(field.candidates) == 1
            assert field.candidates[0].target.table_id == 'public.customers'
            assert field.candidates[0].target.column_id == 'email'
            assert field.status == 'rejected'
        else:
            assert field.status == 'unmapped' and not field.candidates
    else:
        db = make_catalog(
            table('customers', column('legal_name'), column('inn', position=1)),
            table('suppliers', column('legal_name'), column('inn', position=1)),
        )
        data = await profile({scenario: StringScalar(value='Acme')})
        before = data.canonical_json(), db.canonical_json()
        semantic = customer_aliases(db)
        result = await DeterministicMapper().rank(data, db, scope=scope_for(db), semantic_catalog=semantic)
        field = result.fields[0]
        assert field.candidates[0].target.table_id == 'public.customers'
        assert field.candidates[0].target.column_id == 'legal_name'
        explanation = field.explanations[0]
        assert next(s.value for s in explanation.signals if s.code == 'alias_match') == 1
        assert 'IDENTITY_KEY_SUPPORTED' not in explanation.identity_evidence
    assert before == (data.canonical_json(), db.canonical_json())
    assert result.safe_summary().classification == data.classification
    safe = result.safe_summary().model_dump_json()
    assert 'person@example.org' not in safe and 'customers' not in safe
    assert set(result.safe_summary().model_dump()) == {
        'field_count', 'candidate_count', 'ambiguous_count', 'unmapped_count', 'classification',
    }

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
