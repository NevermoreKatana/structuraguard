#!/usr/bin/env python3
"""Точечно выводит разделы большого Markdown-ТЗ по заголовкам."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True)
class Section:
    """Диапазон одного Markdown-раздела."""

    level: int
    title: str
    start: int
    end: int


def parse_sections(lines: list[str]) -> list[Section]:
    """Строит диапазоны заголовков с учётом их уровня."""

    headings: list[tuple[int, str, int]] = []
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if match:
            headings.append((len(match.group(1)), match.group(2), index))

    sections: list[Section] = []
    for position, (level, title, start) in enumerate(headings):
        end = len(lines)
        for next_level, _, next_start in headings[position + 1 :]:
            if next_level <= level:
                end = next_start
                break
        sections.append(
            Section(
                level=level,
                title=title,
                start=start,
                end=end,
            )
        )
    return sections


def normalize(value: str) -> str:
    """Нормализует запрос и заголовок для нечувствительного поиска."""

    return " ".join(value.casefold().split())


def resolve_specification_path(raw_path: str) -> Path:
    """Находит ТЗ из текущего каталога либо относительно корня набора."""

    path = Path(raw_path).expanduser()
    if path.is_absolute() or path.is_file():
        return path

    repository_candidate = Path(__file__).resolve().parents[1] / path
    if repository_candidate.is_file():
        return repository_candidate
    return path


def find_sections(
    sections: list[Section],
    query: str,
) -> list[Section]:
    """Сначала ищет точный префикс, затем безопасный substring fallback."""

    needle = normalize(query)
    normalized_titles = [(section, normalize(section.title)) for section in sections]
    precise_matches = [
        section
        for section, title in normalized_titles
        if title == needle
        or title.startswith(f"{needle}.")
        or title.startswith(f"{needle} ")
    ]
    if precise_matches:
        return precise_matches
    return [section for section, title in normalized_titles if needle in title]


def build_argument_parser() -> argparse.ArgumentParser:
    """Создаёт CLI parser."""

    parser = argparse.ArgumentParser(
        description=(
            "Выводит только выбранные разделы ТЗ, чтобы не загружать весь документ."
        )
    )
    parser.add_argument(
        "queries",
        nargs="*",
        help=("Подстроки заголовков, например 'FR-003' или 'M5. Database Inspector'."),
    )
    parser.add_argument(
        "--file",
        default="StructuraGuard_SDK_Technical_Specification.md",
        help="Путь к Markdown-ТЗ.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Показать список заголовков.",
    )
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()

    path = resolve_specification_path(args.file)
    if not path.is_file():
        print(f"Ошибка: файл ТЗ не найден: {path}", file=sys.stderr)
        return 2

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    sections = parse_sections(lines)

    if args.list:
        for section in sections:
            indent = "  " * (section.level - 1)
            print(f"{section.start + 1:>5}  {indent}{section.title}")
        return 0

    if not args.queries:
        parser.error("Укажите хотя бы один заголовок либо --list.")

    selected: list[Section] = []
    for query in args.queries:
        matches = find_sections(sections, query)
        if not matches:
            print(
                f"Предупреждение: раздел не найден: {query}",
                file=sys.stderr,
            )
            continue
        selected.extend(matches)

    unique = {(section.start, section.end): section for section in selected}
    ordered = sorted(unique.values(), key=lambda section: section.start)
    if not ordered:
        return 1

    for index, section in enumerate(ordered):
        if index:
            print("\n---\n")
        sys.stdout.writelines(lines[section.start : section.end])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
