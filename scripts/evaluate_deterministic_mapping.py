#!/usr/bin/env python3
"""Воспроизвести offline M9 metrics; нужны dev dependencies и синтетические fixtures."""

import argparse
import asyncio
import json
import sys
from pathlib import Path


async def evaluate(path: Path) -> dict[str, object]:
    from tests.fakes.mapping_evaluation import (
        calibrate,
        evaluate,
        grouped_metrics,
        load_cases,
        metrics,
    )

    from structuraguard.contracts.deterministic_mapping import MappingWeights

    cases = load_cases(path.read_text(encoding="utf-8"))
    selected = await calibrate(cases)
    output: dict[str, object] = {
        "selected_weights": selected.model_dump(mode="json"),
        "selection_uses": "calibration_only",
    }
    for name, weights in (("baseline", MappingWeights()), ("selected", selected)):
        for split in ("calibration", "holdout"):
            observations = await evaluate(
                tuple(c for c in cases if c.split == split), weights
            )
            output[f"{name}_{split}"] = {
                "overall": metrics(observations).model_dump(mode="json"),
                "groups": {
                    k: v.model_dump(mode="json")
                    for k, v in grouped_metrics(observations).items()
                },
            }
    return output


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "packages" / "structuraguard"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=root
        / "packages/structuraguard/tests/fixtures/mapping/ru_en_cases.json",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(evaluate(args.fixtures)),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
