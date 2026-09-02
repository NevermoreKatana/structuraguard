#!/usr/bin/env python3
"""Проверяет структуру, размер контекста и базовую безопасность Codex-набора."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import sys
import tomllib

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LINK_RE = re.compile(r"\((references|scripts)/([^)#]+)(?:#[^)]+)?\)")
PLAIN_REFERENCE_RE = re.compile(r"(?<![\w/])(references/[A-Za-z0-9_.\-/]+)")
DANGEROUS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bcurl\b[^\n|]*\|\s*(?:sh|bash)\b", re.I), "download-and-execute pipeline"),
    (re.compile(r"\bwget\b[^\n|]*\|\s*(?:sh|bash)\b", re.I), "download-and-execute pipeline"),
    (re.compile(r"\brm\s+-rf\s+/(?:\s|$)", re.I), "destructive root deletion"),
    (re.compile(r"\bgit\s+add\s+\.\s*$", re.I | re.M), "broad git staging"),
    (re.compile(r"--no-verify\b", re.I), "bypassing repository checks"),
    (re.compile(r"\bshell\s*=\s*True\b"), "shell=True"),
    (re.compile(r"\b(?:eval|exec)\s*\("), "dynamic code execution"),
)
CONFIG_ENUMS: dict[str, set[str]] = {
    "model_verbosity": {"low", "medium", "high"},
    "model_reasoning_summary": {"auto", "concise", "detailed", "none"},
    "model_reasoning_effort": {"minimal", "low", "medium", "high", "xhigh"},
    "plan_mode_reasoning_effort": {"none", "minimal", "low", "medium", "high", "xhigh"},
    "personality": {"none", "friendly", "pragmatic"},
}


@dataclass(frozen=True)
class Issue:
    """Одна обнаруженная проблема набора."""

    level: str
    path: Path
    message: str


def parse_frontmatter(path: Path) -> tuple[dict[str, str], str, str]:
    """Возвращает плоские обязательные поля frontmatter и тело Skill."""

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("нет открывающего YAML frontmatter")

    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration as error:
        raise ValueError("нет закрывающего YAML frontmatter") from error

    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"\'')

    body = "\n".join(lines[end + 1 :])
    return metadata, body, text


def referenced_paths(skill_dir: Path, text: str) -> set[Path]:
    """Находит только skill-local references и scripts."""

    found: set[Path] = set()
    for match in LINK_RE.finditer(text):
        found.add(skill_dir / match.group(1) / match.group(2))
    for match in PLAIN_REFERENCE_RE.finditer(text):
        found.add(skill_dir / match.group(1))
    return found


def validate_openai_metadata(skill_dir: Path) -> list[Issue]:
    """Проверяет минимальную UI-метаинформацию без внешнего YAML parser."""

    path = skill_dir / "agents" / "openai.yaml"
    if not path.is_file():
        return [Issue("WARNING", path, "нет optional UI metadata")]

    text = path.read_text(encoding="utf-8")
    issues: list[Issue] = []
    if "\t" in text:
        issues.append(Issue("ERROR", path, "YAML содержит tab indentation"))
    if not re.search(r"(?m)^interface:\s*$", text):
        issues.append(Issue("ERROR", path, "отсутствует interface"))
    if not re.search(r"(?m)^\s{2}display_name:\s*\S", text):
        issues.append(Issue("ERROR", path, "отсутствует interface.display_name"))
    if not re.search(r"(?m)^\s{2}short_description:\s*\S", text):
        issues.append(Issue("ERROR", path, "отсутствует interface.short_description"))
    return issues


def validate_config(root: Path) -> list[Issue]:
    """Проверяет репозиторную конфигурацию Codex и значения enum."""

    path = root / ".codex" / "config.toml"
    if not path.is_file():
        return [Issue("WARNING", path, "репозиторный config.toml не найден")]

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        return [Issue("ERROR", path, f"некорректный TOML: {error}")]

    issues: list[Issue] = []
    for key, allowed in CONFIG_ENUMS.items():
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, str) or value not in allowed:
            issues.append(
                Issue(
                    "ERROR",
                    path,
                    f"{key}={value!r}; допустимо: {', '.join(sorted(allowed))}",
                )
            )
    return issues


def validate_skill_docs(root: Path, names: set[str]) -> list[Issue]:
    """Проверяет, что обзор и trigger matrix перечисляют все навыки."""

    paths = (
        root / ".agents" / "skills" / "README.md",
        root / "docs" / "codex" / "SKILL_TRIGGER_MATRIX.md",
    )
    issues: list[Issue] = []
    for path in paths:
        if not path.is_file():
            issues.append(Issue("WARNING", path, "документ не найден"))
            continue
        text = path.read_text(encoding="utf-8")
        for name in sorted(names):
            if f"`{name}`" not in text:
                issues.append(Issue("WARNING", path, f"не упомянут skill {name}"))
    return issues


def validate(root: Path) -> list[Issue]:
    """Выполняет все локальные проверки набора."""

    issues: list[Issue] = []
    skills_root = root / ".agents" / "skills"
    if not skills_root.is_dir():
        return [Issue("ERROR", skills_root, "каталог skills не найден")]

    names: dict[str, Path] = {}
    descriptions: list[str] = []

    for skill_dir in sorted(path for path in skills_root.iterdir() if path.is_dir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            issues.append(Issue("ERROR", skill_dir, "нет SKILL.md"))
            continue

        try:
            metadata, body, text = parse_frontmatter(skill_file)
        except ValueError as error:
            issues.append(Issue("ERROR", skill_file, str(error)))
            continue

        name = metadata.get("name", "")
        description = metadata.get("description", "")

        if not name:
            issues.append(Issue("ERROR", skill_file, "отсутствует name"))
        elif name != skill_dir.name:
            issues.append(
                Issue("ERROR", skill_file, f"name '{name}' не совпадает с каталогом")
            )
        elif len(name) > 64 or not NAME_RE.fullmatch(name):
            issues.append(
                Issue("ERROR", skill_file, "name нарушает Agent Skills naming rules")
            )

        if name in names:
            issues.append(
                Issue("ERROR", skill_file, f"дубликат name; первый: {names[name]}")
            )
        elif name:
            names[name] = skill_file

        if not description:
            issues.append(Issue("ERROR", skill_file, "отсутствует description"))
        elif len(description) > 1024:
            issues.append(Issue("ERROR", skill_file, "description длиннее 1024 символов"))
        else:
            descriptions.append(description)

        if re.search(r"(?m)^ {4,}#{1,6}\s", body):
            issues.append(
                Issue(
                    "ERROR",
                    skill_file,
                    "заголовок ошибочно отформатирован как code block",
                )
            )

        line_count = text.count("\n") + 1
        if line_count > 500:
            issues.append(
                Issue("WARNING", skill_file, f"{line_count} строк; рекомендовано менее 500")
            )
        if len(text.encode("utf-8")) > 20_000:
            issues.append(
                Issue(
                    "WARNING",
                    skill_file,
                    "SKILL.md больше 20 KB; вынесите детали в references",
                )
            )

        referenced = referenced_paths(skill_dir, text)
        for target in sorted(referenced):
            if not target.exists():
                issues.append(
                    Issue(
                        "ERROR",
                        skill_file,
                        f"не найдена ссылка: {target.relative_to(skill_dir)}",
                    )
                )

        references_dir = skill_dir / "references"
        if references_dir.is_dir():
            for target in sorted(path for path in references_dir.rglob("*") if path.is_file()):
                if target not in referenced:
                    issues.append(
                        Issue(
                            "WARNING",
                            target,
                            "reference не упомянут в SKILL.md",
                        )
                    )

        for pattern, label in DANGEROUS_PATTERNS:
            if pattern.search(text):
                issues.append(
                    Issue("ERROR", skill_file, f"опасный шаблон: {label}")
                )

        issues.extend(validate_openai_metadata(skill_dir))

    metadata_chars = sum(len(name) for name in names) + sum(
        len(description) for description in descriptions
    )
    if metadata_chars > 8_000:
        issues.append(
            Issue(
                "WARNING",
                skills_root,
                f"metadata занимают {metadata_chars} символов; возможна обрезка",
            )
        )

    agents = root / "AGENTS.md"
    if not agents.is_file():
        issues.append(Issue("ERROR", agents, "корневой AGENTS.md не найден"))
    elif agents.stat().st_size > 32 * 1024:
        issues.append(
            Issue("WARNING", agents, "AGENTS.md превышает стандартный лимит 32 KiB")
        )

    specification = root / "StructuraGuard_SDK_Technical_Specification.md"
    if not specification.is_file():
        issues.append(Issue("WARNING", specification, "полное ТЗ не найдено"))

    issues.extend(validate_config(root))
    issues.extend(validate_skill_docs(root, set(names)))
    return issues


def display_path(path: Path, root: Path) -> Path:
    """Показывает локальный путь, когда файл находится внутри root."""

    try:
        return path.relative_to(root)
    except ValueError:
        return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Валидирует StructuraGuard Codex pack.")
    parser.add_argument("root", nargs="?", default=".", help="Корень репозитория.")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    issues = validate(root)
    errors = 0
    warnings = 0
    for issue in issues:
        print(f"{issue.level}: {display_path(issue.path, root)}: {issue.message}")
        errors += issue.level == "ERROR"
        warnings += issue.level == "WARNING"

    skills_root = root / ".agents" / "skills"
    skill_count = (
        len([path for path in skills_root.iterdir() if path.is_dir()])
        if skills_root.is_dir()
        else 0
    )
    if errors:
        print(f"FAIL: skills={skill_count}, errors={errors}, warnings={warnings}")
        return 1

    print(f"OK: skills={skill_count}, errors=0, warnings={warnings}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
