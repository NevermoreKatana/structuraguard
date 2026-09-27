"""Markdown использует каноническую allowlist, независимо от расширения."""

from uuid import UUID

import pytest
from tests.fakes.parsers import FakeSourceReader
from tests.unit.parsers.builtin._support import contexts_for, source_for

from structuraguard.exceptions import SecurityPolicyError
from structuraguard.parsers.builtin import MarkdownParser
from structuraguard.security import SecurityPolicy, SecuritySession


@pytest.mark.anyio
@pytest.mark.parametrize("extension", ["md", "markdown"])
@pytest.mark.parametrize("allowed", [True, False])
async def test_markdown_policy_allows_only_explicit_canonical_format(
    extension: str, *, allowed: bool
) -> None:
    content = b"# Contacts\n\nEmail: reader@example.test\n"
    source = source_for(
        content, display_name="contacts." + extension, media_type="text/markdown"
    )
    reader = FakeSourceReader(source.source_fingerprint, content=content)
    _, context = contexts_for(source, content, reader=reader)
    run = SecuritySession(
        SecurityPolicy(
            allowed_formats=("markdown",) if allowed else ("txt",),
            parser_trust="trusted",
        ),
        run_id=UUID(int=1),
    )

    if not allowed:
        with pytest.raises(SecurityPolicyError, match="SECURITY_INPUT_REJECTED"):
            async with run.parse(MarkdownParser(), source, context):
                pytest.fail("Запрещённый Markdown допущен к чтению")
        assert reader.reads == ()
        assert run.events
        return

    async with run.parse(MarkdownParser(), source, context) as stream:
        batches = [batch async for batch in stream]
    assert [line.text for batch in batches for line in batch.lines] == [
        "# Contacts",
        "",
        "Email: reader@example.test",
    ]
    assert not run.events
