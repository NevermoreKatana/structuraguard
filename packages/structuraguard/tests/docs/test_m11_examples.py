"""Копируемый пример M11 проверяется по коду руководства без сети и записи в БД."""

import re
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "scenario",
    [
        "insert",
        "upsert",
        "forbidden_target",
        "malformed",
        "sql_fragment",
        "low_confidence",
        "schema_drift",
        "unverified",
        "limit",
    ],
)
def test_m11_documented_example_is_copyable_offline(
    tmp_path: Path, scenario: str
) -> None:
    root = Path(__file__).resolve().parents[4]
    documentation = (root / "docs/mapping-plan-validation.md").read_text()
    match = re.search(
        r"<!-- example:m11-validation:start -->\s*```python\n(?P<code>.*?)\n```",
        documentation,
        flags=re.DOTALL,
    )
    assert match is not None
    guard = """
import sys

def deny_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise AssertionError('Сеть запрещена в примере M11')

sys.addaudithook(deny_network)
"""
    assertions = """
import asyncio
import json
import sqlite3
from decimal import Decimal
from tests.fakes.mapping import catalog as make_catalog, column, table
from tests.fakes.mapping_validation import case, replan
from structuraguard.contracts import LoadOperation, ValidationDecision
from structuraguard.exceptions import ValidationError

def no_database(*args, **kwargs):
    raise AssertionError('Запись/чтение БД запрещены в примере M11')

sqlite3.connect = no_database

async def main():
    scenario = sys.argv[1]
    plan, manifest, db, _, profile = await case(
        semantic_type='unresolved' if scenario == 'unverified' else 'integer',
    )
    db = make_catalog(db.schemas[0].tables[0], table('other', column('amount', 'integer')))
    plan = replan(plan, database_fingerprint=db.database_fingerprint)
    if scenario == 'upsert':
        plan = replan(plan, operation=LoadOperation.UPSERT)
    if scenario == 'low_confidence':
        plan = replan(plan, confidence=Decimal('0.89'))
    if scenario == 'forbidden_target':
        first, second = plan.mappings
        plan = replan(plan, mappings=(first, second.model_copy(update={
            'target': CatalogColumnRef(table_id='public.other', column_id='amount'),
        })))
    if scenario == 'schema_drift':
        target = db.schemas[0].tables[0]
        db = make_catalog(target.model_copy(update={
            'columns': (*target.columns, column('note', position=2)),
        }), db.schemas[0].tables[1])
    payload = plan.canonical_json().encode()
    if scenario == 'malformed':
        payload = b'{'
    elif scenario == 'sql_fragment':
        raw = plan.model_dump(mode='json')
        raw['sql'] = 'DROP TABLE orders; -- secret-canary'
        payload = json.dumps(raw).encode()
    before = tuple(x.canonical_json() for x in (plan, manifest, db, profile))
    try:
        result = await check_order_plan(
            payload, manifest, db,
            profile=None if scenario == 'unverified' else profile,
            options=MappingValidationOptions(max_mappings=1) if scenario == 'limit' else None,
        )
    except ValidationError as error:
        assert scenario == 'limit'
        assert error.error_code == 'MAPPING_LIMIT_EXCEEDED'
    else:
        assert scenario != 'limit'
        codes = {issue.code for issue in result.issues}
        if scenario in {'insert', 'upsert'}:
            assert isinstance(result, MappingPlanValidationResult)
            assert result.decision is ValidationDecision.ACCEPTED
            assert result.validated_plan is not None
            assert result.evidence is not None
            assert result.evidence.load_order == ('public.orders',)
            assert not codes
        elif scenario in {'malformed', 'sql_fragment'}:
            assert isinstance(result, MappingPlanInputReport)
            assert result.decision is ValidationDecision.REJECTED
            assert ('MAPPING_PLAN_INVALID' if scenario == 'malformed' else 'MAPPING_SQL_FORBIDDEN') in codes
            assert result.complete is (scenario == 'sql_fragment')
            assert 'secret-canary' not in result.canonical_json()
        else:
            assert isinstance(result, MappingPlanValidationResult)
            assert result.validated_plan is None
            assert result.decision is (ValidationDecision.NEEDS_REVIEW if scenario == 'unverified' else ValidationDecision.REJECTED)
            expected = {
                'forbidden_target': 'MAPPING_TABLE_DENIED',
                'low_confidence': 'MAPPING_CONFIDENCE_BELOW_THRESHOLD',
                'schema_drift': 'DATABASE_SCHEMA_DRIFT',
                'unverified': 'MAPPING_TYPE_UNVERIFIED',
            }
            assert expected[scenario] in codes
    assert before == tuple(x.canonical_json() for x in (plan, manifest, db, profile))
    print('M11 example OK')

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
    assert result.stdout == "M11 example OK\n"
