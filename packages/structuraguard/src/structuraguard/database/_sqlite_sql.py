"""Ограниченный lexical разбор сохранённых SQLite DDL; текст не исполняется.

PRAGMA даёт keys и flags, но не тексты CHECK/generated/expression indexes.
Здесь нужны только границы выражений, а не интерпретация SQL или regex-equivalence.
"""

from __future__ import annotations

from dataclasses import dataclass

from structuraguard.exceptions import DatabaseInspectionError


def unsupported() -> DatabaseInspectionError:
    return DatabaseInspectionError(
        error_code="DATABASE_METADATA_UNSUPPORTED",
        message="Metadata нельзя полностью и безопасно представить.",
    )


@dataclass(frozen=True, slots=True)
class Token:
    text: str
    start: int
    end: int
    depth: int
    quoted: bool = False

    def keyword(self, text: str) -> bool:
        return not self.quoted and self.text.upper() == text


def tokenize(sql: str) -> tuple[Token, ...]:
    tokens: list[Token] = []
    offset = 0
    depth = 0
    while offset < len(sql):
        character = sql[offset]
        if character.isspace():
            offset += 1
            continue
        if sql.startswith("--", offset):
            end = sql.find("\n", offset + 2)
            offset = len(sql) if end == -1 else end + 1
            continue
        if sql.startswith("/*", offset):
            end = sql.find("*/", offset + 2)
            if end == -1:
                raise unsupported()
            offset = end + 2
            continue
        start = offset
        if character in "'\"`[":
            closing = "]" if character == "[" else character
            offset += 1
            content: list[str] = []
            while offset < len(sql):
                if sql[offset] == closing:
                    if closing != "]" and sql[offset : offset + 2] == closing * 2:
                        content.append(closing)
                        offset += 2
                        continue
                    offset += 1
                    break
                content.append(sql[offset])
                offset += 1
            else:
                raise unsupported()
            tokens.append(Token("".join(content), start, offset, depth, True))
            continue
        if character.isalnum() or character in "_$":
            offset += 1
            while offset < len(sql) and (sql[offset].isalnum() or sql[offset] in "_$"):
                offset += 1
            tokens.append(Token(sql[start:offset], start, offset, depth))
            continue
        if character == ")":
            depth -= 1
            if depth < 0:
                raise unsupported()
        tokens.append(Token(character, start, start + 1, depth))
        if character == "(":
            depth += 1
            if depth > 64:
                raise unsupported()
        offset += 1
    if depth:
        raise unsupported()
    return tuple(tokens)


def group_end(tokens: tuple[Token, ...], start: int) -> int:
    if start >= len(tokens) or tokens[start].text != "(" or tokens[start].quoted:
        raise unsupported()
    for index in range(start + 1, len(tokens)):
        if (
            tokens[index].text == ")"
            and not tokens[index].quoted
            and tokens[index].depth == tokens[start].depth
        ):
            return index
    raise unsupported()


def expression(sql: str, tokens: tuple[Token, ...], start: int) -> str:
    end = group_end(tokens, start)
    result = sql[tokens[start].end : tokens[end].start].strip()
    if not result:
        raise unsupported()
    return result


@dataclass(frozen=True, slots=True)
class TableDefinition:
    checks: tuple[str, ...]
    generated: tuple[tuple[str, str], ...]
    autoincrement: bool
    strict: bool
    without_rowid: bool


def table_definition(sql: str, column_names: frozenset[str]) -> TableDefinition:
    tokens = tokenize(sql)
    if any(token.keyword("VIRTUAL") for token in tokens[:3]):
        raise unsupported()
    start = next(
        (i for i, token in enumerate(tokens) if token.text == "(" and not token.quoted),
        -1,
    )
    if start < 0:
        raise unsupported()
    end = group_end(tokens, start)
    checks: list[str] = []
    generated: list[tuple[str, str]] = []
    current_column: str | None = None
    at_start = True
    autoincrement = False
    for index in range(start + 1, end):
        token = tokens[index]
        if token.depth != 1:
            continue
        # PRAGMA теряет эти свойства: публикация каталога скрыла бы schema drift.
        # COLLATE внутри CHECK/generated/index expressions уже сохраняется текстом.
        if (
            token.keyword("DEFERRABLE")
            or token.keyword("COLLATE")
            or (
                token.keyword("ON")
                and index + 1 < end
                and tokens[index + 1].keyword("CONFLICT")
            )
        ):
            raise unsupported()
        if token.text == "," and not token.quoted:
            at_start = True
            current_column = None
            continue
        if at_start:
            table_constraint = any(
                token.keyword(keyword)
                for keyword in ("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN")
            )
            current_column = (
                token.text
                if token.text in column_names and not table_constraint
                else None
            )
            at_start = False
            if current_column is not None:
                continue
        if token.keyword("CHECK"):
            checks.append(expression(sql, tokens, index + 1))
        if token.keyword("AS") and current_column is not None:
            generated.append((current_column, expression(sql, tokens, index + 1)))
        if token.keyword("AUTOINCREMENT"):
            autoincrement = True
    suffix = tokens[end + 1 :]
    return TableDefinition(
        checks=tuple(sorted(checks)),
        generated=tuple(generated),
        autoincrement=autoincrement,
        strict=any(token.keyword("STRICT") for token in suffix),
        without_rowid=any(token.keyword("WITHOUT") for token in suffix),
    )


def index_definition(sql: str) -> tuple[tuple[str, ...], str | None]:
    tokens = tokenize(sql)
    start = next(
        (i for i, token in enumerate(tokens) if token.text == "(" and not token.quoted),
        -1,
    )
    if start < 0:
        raise unsupported()
    end = group_end(tokens, start)
    pieces: list[str] = []
    offset = tokens[start].end
    for token in tokens[start + 1 : end]:
        if token.depth == 1 and token.text == "," and not token.quoted:
            pieces.append(sql[offset : token.start].strip())
            offset = token.end
    pieces.append(sql[offset : tokens[end].start].strip())
    predicate = None
    for token in tokens[end + 1 :]:
        if token.keyword("WHERE"):
            predicate = sql[token.end :].strip().removesuffix(";").rstrip()
            break
    if not all(pieces):
        raise unsupported()
    return tuple(pieces), predicate
