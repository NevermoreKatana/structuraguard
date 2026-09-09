"""Регрессии XXE, entities, unsafe YAML и inert HTML на trust boundary."""

from __future__ import annotations

import builtins
import json
import socket

import pytest
import yaml
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for

from structuraguard.contracts import PhysicalNodeKind
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.parsers.builtin import (
    HtmlParser,
    XmlParser,
    XmlParserLimits,
    YamlParser,
    YamlParserLimits,
    html_safe_json,
)

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "content",
    [
        b'<!DOCTYPE r SYSTEM "https://example.invalid/external.dtd"><r/>',
        b'<!DOCTYPE r [<!ENTITY x SYSTEM "file:///private/secret">]><r>&x;</r>',
        b'<!DOCTYPE r [<!ENTITY x SYSTEM "https://example.invalid/secret">]><r>&x;</r>',
        b'<!DOCTYPE r [<!ENTITY % x SYSTEM "https://example.invalid/dtd">%x;]><r/>',
        b'<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol1 "&lol;&lol;&lol;"><!ENTITY lol2 "&lol1;&lol1;&lol1;">]><lolz>&lol2;</lolz>',
        b"<!DOCTYPE r><r/>",
    ],
)
async def test_xml_dtd_xxe_billion_laughs_rejected_without_io(
    content: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(*args: object, **kwargs: object) -> None:
        pytest.fail("XML не должен обращаться к внешним ресурсам")

    monkeypatch.setattr(socket, "create_connection", deny)
    import defusedxml.ElementTree

    assert isinstance(defusedxml.ElementTree.DefusedXMLParser, type)
    monkeypatch.setattr(builtins, "open", deny)
    source = source_for(content, display_name="attack.xml")
    probe, parse = contexts_for(source, content)
    for action in (
        XmlParser().probe(source, probe),
        collect(XmlParser(), source, parse),
    ):
        with pytest.raises(
            SecurityPolicyError, match="SECURITY_INPUT_REJECTED"
        ) as failure:
            await action
        assert "secret" not in str(failure.value.details)


async def test_xml_incomplete_token_cannot_hide_behind_comment_gt() -> None:
    content = b"<r><!-->" + b"x" * 10000
    source = source_for(content, display_name="attack.xml")
    with pytest.raises(SecurityPolicyError):
        await collect(
            XmlParser(limits=XmlParserLimits(max_token_chars=32, read_chunk_bytes=16)),
            source,
            contexts_for(source, content)[1],
        )


@pytest.mark.parametrize(
    "content",
    [
        b'!!python/object/apply:os.system ["echo secret"]',
        b"a: !!python/object:builtins.object {}",
        b"a: !arbitrary value",
    ],
)
async def test_yaml_no_python_or_custom_objects(
    content: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(*args: object, **kwargs: object) -> None:
        pytest.fail("YAML constructors не должны вызываться")

    monkeypatch.setattr(yaml.SafeLoader, "construct_object", deny)
    source = source_for(content, display_name="attack.yaml")
    with pytest.raises(SecurityPolicyError, match="SECURITY_INPUT_REJECTED") as failure:
        await collect(YamlParser(), source, contexts_for(source, content)[1])
    assert "secret" not in str(failure.value.details)


async def test_yaml_alias_bomb_is_bounded_not_expanded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny(*args: object, **kwargs: object) -> None:
        pytest.fail("Даже safe constructors не нужны physical YAML events")

    monkeypatch.setattr(yaml.SafeLoader, "construct_object", deny)
    content = b"a: &a [x,x,x]\nb: &b [*a,*a,*a]\nc: &c [*b,*b,*b]\nd: [*c,*c,*c]\n"
    source = source_for(content, display_name="bomb.yaml")
    context = contexts_for(source, content)[1]
    with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
        await collect(
            YamlParser(limits=YamlParserLimits(max_aliases=8)), source, context
        )
    batches = await collect(YamlParser(), source, context)
    assert (
        sum(n.node_kind is PhysicalNodeKind.ALIAS for b in batches for n in b.trees)
        == 9
    )
    assert (
        sum(
            n.value is not None and n.value.raw_value.value == "x"
            for b in batches
            for n in b.trees
        )
        == 3
    )


async def test_html_xss_remains_data_and_safe_json_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny(*args: object, **kwargs: object) -> None:
        pytest.fail("HTML не должен загружать resources")

    monkeypatch.setattr(socket, "create_connection", deny)
    content = b'<script>fetch("https://example.invalid/secret")</script><style>@import "https://example.invalid/a";</style><iframe src="https://example.invalid"><p>denied</p></iframe><p>&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;</p><img src="https://example.invalid" onerror="alert(1)">'
    source = source_for(content, display_name="attack.html")
    batches = await collect(HtmlParser(), source, contexts_for(source, content)[1])
    nodes = [n for b in batches for n in b.trees]
    assert any(
        n.value and n.value.raw_value.value == 'fetch("https://example.invalid/secret")'
        for n in nodes
    )
    assert any(
        n.value and n.value.raw_value.value == "</script><img src=x onerror=alert(1)>"
        for n in nodes
    )
    assert all(n.metadata for n in nodes if n.raw_name in {"script", "style", "iframe"})
    assert not any(b.text == "denied" for batch in batches for b in batch.blocks)
    for batch in batches:
        safe = html_safe_json(batch)
        assert "<" not in safe and ">" not in safe and "&" not in safe
        assert json.loads(safe) == json.loads(batch.canonical_json())
