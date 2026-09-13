"""Закрытое regex-подмножество с линейным matching по длине строки."""

import re


def safe_pattern(pattern: str, max_chars: int) -> bool:
    """Литералы/ASCII classes; одна variable repetition только после ^.

    Нет групп, alternation, lookaround, backrefs, dot, flags и повторов повторов.
    При одном variable atom anchored search делает не более O(n * pattern_length)
    шагов; остальные atoms фиксированы. Python re timeout не предполагается.
    """
    if len(pattern) > max_chars or not pattern.isascii():
        return False
    pos = int(pattern.startswith("^"))
    end = len(pattern) - int(pattern.endswith("$") and not pattern.endswith("\\$"))
    variable = 0
    while pos < end:
        char = pattern[pos]
        if char == "[":
            stop = pattern.find("]", pos + 1)
            if stop < 0 or stop >= end:
                return False
            body = pattern[pos + 1 : stop]
            if (
                not body
                or any(c in body for c in "\\[]")
                or any(sequence in body for sequence in ("&&", "||", "~~", "--"))
            ):
                return False
            pos = stop + 1
        elif char == "\\":
            pos += 1
            if pos >= end or pattern[pos] not in r"^$[]{}()+*?.|\-":
                return False
            pos += 1
        elif char in "^$.*+?{}()|]" or ord(char) < 32:
            return False
        else:
            pos += 1
        if pos < end and pattern[pos] in "*+?{":
            if pattern[pos] == "{":
                stop = pattern.find("}", pos + 1)
                if stop < 0:
                    return False
                bounds = pattern[pos + 1 : stop].split(",")
                if (
                    len(bounds) > 2
                    or any(
                        not b.isascii() or not b.isdigit() or len(b) > 3 for b in bounds
                    )
                    or any(int(b) > 256 for b in bounds)
                ):
                    return False
                if len(bounds) == 2 and int(bounds[0]) > int(bounds[1]):
                    return False
                variable += int(len(bounds) == 2 and bounds[0] != bounds[1])
                pos = stop + 1
            else:
                variable += 1
                pos += 1
            if variable and (variable > 1 or not pattern.startswith("^")):
                return False
    try:
        re.compile(pattern, flags=re.ASCII)
    except re.error:
        return False
    return True
