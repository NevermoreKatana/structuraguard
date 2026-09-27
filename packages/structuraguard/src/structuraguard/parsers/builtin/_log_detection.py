"""Bounded ISO syslog envelope; сообщение остаётся непрозрачным текстом."""

import re

ISO_SYSLOG_EVENT = re.compile(
    r"^(?P<timestamp>[0-9]{4}-[0-9]{2}-[0-9]{2}[ T]"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})?)"
    r"[ \t]+(?P<host>[A-Za-z0-9_.-]{1,255})"
    r"[ \t]+(?P<service>[A-Za-z0-9_./-]{1,128})"
    r"(?:\[(?P<pid>[0-9]{1,20})\])?:[ \t]+"
)


def has_syslog_envelopes(lines: tuple[str, ...]) -> bool:
    """Подтвердить минимум две целые строки без header, fences и внешней таблицы."""

    candidates = tuple(line for line in lines if line.strip())
    return len(candidates) >= 2 and all(
        ISO_SYSLOG_EVENT.match(line) is not None for line in candidates
    )
