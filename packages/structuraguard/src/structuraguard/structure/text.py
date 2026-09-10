"""Фиксированные lexical классы; token signatures никогда не компилируются."""

from decimal import Decimal
from typing import Literal

from structuraguard.contracts.common import ParsePlanKind
from structuraguard.contracts.structure import TextObservation
from structuraguard.structure._observations import Observations
from structuraguard.structure._samples import Line, primitive

_LEVELS = frozenset(
    {
        "TRACE",
        "DEBUG",
        "INFO",
        "NOTICE",
        "WARN",
        "WARNING",
        "ERROR",
        "CRITICAL",
        "FATAL",
    }
)


def key_values(text: str) -> tuple[str, ...]:
    result: list[str] = []
    for token in text.split(maxsplit=64)[:64]:
        key, separator, value = token.partition("=")
        if (
            separator
            and key
            and len(key) <= 128
            and value
            and key not in result
            and all(char.isalnum() or char in "_.-" for char in key)
        ):
            result.append(key)
    return tuple(result)


def signatures(text: str) -> tuple[tuple[str, ...], tuple[str, ...], int, int]:
    template: list[str] = []
    shape: list[str] = []
    timestamps = levels = 0
    tokens = text.split(maxsplit=64)[:64]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        hint = primitive(token)
        if (
            hint == "date"
            and index < len(tokens)
            and primitive(token + " " + tokens[index]) == "timestamp"
        ):
            hint = "timestamp"
            index += 1
        timestamps += hint == "timestamp"
        is_level = token.strip("[]:") in _LEVELS
        levels += is_level
        key, separator, value = token.partition("=")
        if separator and key and len(key) <= 128 and value:
            template.append(key + "=<" + primitive(value) + ">")
            shape.append("key_value")
        elif hint != "string":
            template.append("<" + hint + ">")
            shape.append(hint)
        else:
            template.append(token)
            shape.append("level" if is_level else "word")
    return tuple(template), tuple(shape), timestamps, levels


def analyze_text(output: Observations) -> None:
    lines = [line for line in output.samples.lines if not line.block]
    blocks = [line for line in output.samples.lines if line.block]
    for line in blocks:
        output.add(
            TextObservation(
                source_refs=(line.reference,),
                role="block_boundary",
                occurrences=1,
                line_start=line.start,
                line_end=line.end,
            ),
            Decimal("1"),
        )
        if line.end > line.start:
            output.add(
                TextObservation(
                    source_refs=(line.reference,),
                    role="multiline",
                    occurrences=1,
                    line_start=line.start,
                    line_end=line.end,
                ),
                Decimal("0.8"),
                candidate=ParsePlanKind.LOG,
            )
    if not lines:
        return
    templates: dict[tuple[str, ...], list[Line]] = {}
    shapes: dict[tuple[str, ...], list[Line]] = {}
    for line in lines:
        if len(line.text.split(maxsplit=64)) > 64:
            output.samples.reasons.add("token_limit")
            continue
        template, shape, timestamp, level = signatures(line.text)
        for signature, groups in ((template, templates), (shape, shapes)):
            if signature not in groups and len(groups) >= output.options.max_patterns:
                output.samples.reasons.add("pattern_limit")
            else:
                groups.setdefault(signature, []).append(line)
        keys = key_values(line.text)
        if keys:
            output.add(
                TextObservation(
                    source_refs=(line.reference,),
                    role="key_value",
                    occurrences=1,
                    line_start=line.start,
                    line_end=line.end,
                    keys=keys,
                    timestamp_count=timestamp,
                    level_count=level,
                ),
                Decimal("0.75"),
                candidate=ParsePlanKind.LOG,
            )
    boundary_groups: list[list[Line]] = []
    for line in lines:
        if not boundary_groups or line.start != boundary_groups[-1][-1].end + 1:
            boundary_groups.append([])
        boundary_groups[-1].append(line)
    for group in boundary_groups:
        output.add(
            TextObservation(
                source_refs=tuple(
                    dict.fromkeys((group[0].reference, group[-1].reference))
                ),
                role="line_boundary",
                occurrences=len(group),
                line_start=group[0].start,
                line_end=group[-1].end,
            ),
            Decimal("1"),
            candidate=ParsePlanKind.LOG,
        )
    kinds: tuple[
        tuple[dict[tuple[str, ...], list[Line]], Literal["template", "line_shape"]], ...
    ] = ((templates, "template"), (shapes, "line_shape"))
    for groups, role in kinds:
        for signature, group in groups.items():
            if not signature or len(group) < 2:
                continue
            output.add(
                TextObservation(
                    source_refs=tuple(line.reference for line in group[:4]),
                    role=role,
                    signature=signature,
                    occurrences=len(group),
                    line_start=group[0].start,
                    line_end=group[-1].end,
                    timestamp_count=sum(signatures(line.text)[2] for line in group),
                    level_count=sum(signatures(line.text)[3] for line in group),
                ),
                Decimal("0.8"),
                candidate=ParsePlanKind.LOG if role == "template" else None,
            )
    start: Line | None = None
    previous: Line | None = None
    for line in lines:
        contiguous = previous is None or line.start == previous.end + 1
        if not contiguous:
            start = None
        if not line.text.strip():
            if (
                start is not None
                and previous is not None
                and previous.end > start.start
            ):
                output.add(
                    TextObservation(
                        source_refs=tuple(
                            dict.fromkeys((start.reference, previous.reference))
                        ),
                        role="multiline",
                        signature=("blank_separator",),
                        occurrences=1,
                        line_start=start.start,
                        line_end=previous.end,
                    ),
                    Decimal("0.7"),
                    candidate=ParsePlanKind.LOG,
                )
            start = None
        elif start is None:
            start = line
        elif line.text[0].isspace():
            output.add(
                TextObservation(
                    source_refs=(start.reference, line.reference),
                    role="multiline",
                    signature=("indented_continuation",),
                    occurrences=1,
                    line_start=start.start,
                    line_end=line.end,
                ),
                Decimal("0.7"),
                candidate=ParsePlanKind.LOG,
            )
        elif signatures(line.text)[2]:
            start = line
        previous = line
