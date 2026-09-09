"""Ослабление ownership probes не разрешает unsafe YAML или broken JSON decoding."""

from __future__ import annotations

import pytest
import yaml
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for

from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import (
    JsonDocumentParser,
    JsonLinesParser,
    YamlParser,
    builtin_text_parsers,
)

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("parser", (JsonDocumentParser(), JsonLinesParser()))
@pytest.mark.parametrize("content", (b'{"a":"\xff"}', b'1\n2\n{"a":"\xff"}\n'))
async def test_json_probe_and_parse_keep_typed_error_for_invalid_utf8(
    parser: JsonDocumentParser | JsonLinesParser, content: bytes
) -> None:
    source = source_for(content, display_name="attack.txt")
    probe, parse = contexts_for(source, content)
    with pytest.raises(ParserError) as failure:
        await parser.probe(source, probe)
    assert failure.value.error_code == "PARSER_ENCODING_UNSUPPORTED"
    with pytest.raises(ParserError) as failure:
        await collect(parser, source, parse)
    assert failure.value.error_code == "PARSER_ENCODING_UNSUPPORTED"


@pytest.mark.parametrize(
    "content",
    (
        b"a: !!python/object/apply:os.system [DO_NOT_EXECUTE]\n",
        b"---\na: !!python/object/apply:os.system [DO_NOT_EXECUTE]\n",
        b"? [key]\n: !!python/object/apply:os.system [DO_NOT_EXECUTE]\n",
        b"%TAG !run! tag:yaml.org,2002:python/object/apply:os.\n"
        b"---\na: !run!system [DO_NOT_EXECUTE]\n",
    ),
)
async def test_yaml_probe_never_downgrades_unsafe_tags_to_text(
    content: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(*args: object, **kwargs: object) -> None:
        pytest.fail("YAML constructors не должны вызываться")

    monkeypatch.setattr(yaml.SafeLoader, "construct_object", deny)
    source = source_for(content, display_name="attack.txt")
    probe, _ = contexts_for(source, content)
    registry = ParserRegistry()
    registry.register_many((*builtin_text_parsers(), YamlParser()))
    async with registry.session() as session:
        with pytest.raises(SecurityPolicyError) as failure:
            await session.select(source, probe)
    assert failure.value.error_code == "SECURITY_INPUT_REJECTED"
    assert "DO_NOT_EXECUTE" not in str(failure.value)


async def test_yaml_probe_keeps_malformed_confirmed_mapping_error() -> None:
    content = b"key: [one, two\n"
    source = source_for(content, display_name="malformed.txt")
    probe, _ = contexts_for(source, content)
    with pytest.raises(ParserError) as failure:
        await YamlParser().probe(source, probe)
    assert failure.value.error_code == "PARSER_MALFORMED_INPUT"
