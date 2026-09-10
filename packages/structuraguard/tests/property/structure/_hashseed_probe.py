"""Изолированный interpreter: serialization результата не зависит от hash seed."""

import asyncio

from tests.unit.structure.test_analysis import analyze_content
from tests.unit.structure.test_execution import execute, prepared

from structuraguard.contracts.parsing import StructureNeedsReview
from structuraguard.parsers.builtin import DelimitedTextParser, JsonDocumentParser


async def main() -> None:
    request, batches = await prepared(DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n")
    print(request.profile.canonical_json())
    print(request.plan.canonical_json())
    for batch in await execute(request, batches):
        print(batch.canonical_json())
    ambiguous = await analyze_content(
        JsonDocumentParser(), b'{"z":[{"id":1},{"id":2}],"a":[{"id":3},{"id":4}]}'
    )
    assert isinstance(ambiguous, StructureNeedsReview)
    print(ambiguous.canonical_json())


if __name__ == "__main__":
    asyncio.run(main())
