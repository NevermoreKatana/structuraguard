"""Bilingual holdout не участвует в выборе weights; false auto недопустимы."""

from pathlib import Path

import pytest
from tests.fakes.mapping_evaluation import calibrate, evaluate, load_cases, metrics

from structuraguard.contracts.deterministic_mapping import MappingWeights

pytestmark = pytest.mark.anyio


async def test_bilingual_holdout_recall_and_false_auto() -> None:
    path = Path(__file__).resolve().parents[2] / "fixtures/mapping/ru_en_cases.json"
    cases = load_cases(path.read_text(encoding="utf-8"))
    selected = await calibrate(cases)
    assert selected == await calibrate(tuple(reversed(cases)))
    calibration = {
        t.schema_name for c in cases if c.split == "calibration" for t in c.targets
    }
    holdout = tuple(c for c in cases if c.split == "holdout")
    assert calibration.isdisjoint({t.schema_name for c in holdout for t in c.targets})
    for weights in (MappingWeights(), selected):
        observations = await evaluate(holdout, weights)
        result = metrics(observations)
        assert result.recall_at_5 == "1.000000"
        assert result.false_auto == 0
        assert observations == await evaluate(tuple(reversed(holdout)), weights)
