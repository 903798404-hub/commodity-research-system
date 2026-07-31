"""Streaming lexical reader for Navicat/MySQL SQL dump files.

This module recognizes SQL structure only.  It never executes SQL and has no
knowledge of Reuters fields, market data, or downstream storage.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Iterator


DEFAULT_CHUNK_SIZE = 64 * 1024
DEFAULT_MAX_STATEMENT_CHARS = 1024 * 1024
MAX_ERROR_SUMMARY_CHARS = 120


class SqlStatementType(StrEnum):
    SET = "set"
    DROP_TABLE = "drop_table"
    CREATE_TABLE = "create_table"
    INSERT = "insert"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class SqlStatement:
    sql: str
    statement_type: SqlStatementType
    table_name: str | None
    start_line: int
    end_line: int
    statement_index: int


class SqlStreamError(ValueError):
    """Base error with bounded, auditable source location metadata."""

    def __init__(
        self,
        *,
        path: Path,
        line_number: int,
        statement_index: int,
        reason: str,
        safe_summary: str = "",
    ) -> None:
        self.path = path
        self.line_number = line_number
        self.statement_index = statement_index
        self.reason = reason
        self.safe_summary = _bounded_summary(safe_summary)
        summary = f"; near {self.safe_summary!r}" if self.safe_summary else ""
        super().__init__(
            f"{reason}: path={path}, line={line_number}, "
            f"statement_index={statement_index}{summary}"
        )


class SqlFileError(SqlStreamError):
    pass


class SqlEncodingError(SqlStreamError):
    pass


class SqlSyntaxStateError(SqlStreamError):
    pass


class SqlStatementTooLargeError(SqlStreamError):
    pass


class _LexState(StrEnum):
    NORMAL = "normal"
    SINGLE_QUOTE = "single_quote"
    DOUBLE_QUOTE = "double_quote"
    BACKTICK = "backtick"
    DASH_COMMENT = "dash_comment"
    HASH_COMMENT = "hash_comment"
    BLOCK_COMMENT = "block_comment"


class _PeekableCharacters:
    def __init__(self, values: Iterator[tuple[str, int]]) -> None:
        self._values = values
        self._next: tuple[str, int] | None = None
        self._ended = False

    def peek(self) -> tuple[str, int] | None:
        if self._next is None and not self._ended:
            try:
                self._next = next(self._values)
            except StopIteration:
                self._ended = True
        return self._next

    def pop(self) -> tuple[str, int]:
        item = self.peek()
        if item is None:
            raise StopIteration
        self._next = None
        return item


def iter_sql_statements(
    path: str | Path,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_statement_chars: int = DEFAULT_MAX_STATEMENT_CHARS,
) -> Iterator[SqlStatement]:
    """Yield clean SQL statements from a dump without loading the whole file.

    Double quotes are treated as string delimiters, matching the reviewed
    Navicat/MySQL dump convention.  Backticks are always identifier delimiters.
    A semicolon terminates a statement only in the normal lexical state.
    """

    source = Path(path)
    _validate_positive_integer(chunk_size, "chunk_size", source)
    _validate_positive_integer(max_statement_chars, "max_statement_chars", source)
    if not source.exists():
        raise SqlFileError(
            path=source,
            line_number=1,
            statement_index=1,
            reason="SQL file does not exist",
        )
    if not source.is_file():
        raise SqlFileError(
            path=source,
            line_number=1,
            statement_index=1,
            reason="SQL path is not a regular file",
        )

    try:
        binary = source.open("rb")
    except OSError as exc:
        raise SqlFileError(
            path=source,
            line_number=1,
            statement_index=1,
            reason=f"SQL file is not readable: {exc.__class__.__name__}",
        ) from exc

    with binary:
        yield from _parse_stream(
            binary,
            source,
            chunk_size=chunk_size,
            max_statement_chars=max_statement_chars,
        )


def _parse_stream(
    binary: BinaryIO,
    path: Path,
    *,
    chunk_size: int,
    max_statement_chars: int,
) -> Iterator[SqlStatement]:
    characters = _PeekableCharacters(_iter_decoded_characters(binary, chunk_size))
    state = _LexState.NORMAL
    buffer: list[str] = []
    start_line: int | None = None
    end_line = 1
    current_line = 1
    statement_index = 1
    segment_chars = 0

    def consume() -> tuple[str, int]:
        nonlocal current_line, segment_chars
        char, line = characters.pop()
        current_line = line
        segment_chars += 1
        if segment_chars > max_statement_chars:
            raise SqlStatementTooLargeError(
                path=path,
                line_number=line,
                statement_index=statement_index,
                reason=f"SQL statement exceeds {max_statement_chars} characters",
                safe_summary="".join(buffer[-MAX_ERROR_SUMMARY_CHARS:]),
            )
        return char, line

    def append_sql(char: str, line: int, *, quoted: bool = False) -> None:
        nonlocal start_line, end_line
        if start_line is None:
            if char.isspace():
                return
            start_line = line
        buffer.append(char)
        if quoted or not char.isspace():
            end_line = line

    def separate_comment() -> None:
        if buffer and not buffer[-1].isspace():
            buffer.append(" ")

    def emit() -> SqlStatement | None:
        nonlocal buffer, start_line, end_line, statement_index
        sql = "".join(buffer).strip()
        buffer = []
        if not sql or start_line is None:
            start_line = None
            return None
        statement_type, table_name = classify_sql_statement(sql)
        result = SqlStatement(
            sql=sql,
            statement_type=statement_type,
            table_name=table_name,
            start_line=start_line,
            end_line=end_line,
            statement_index=statement_index,
        )
        statement_index += 1
        start_line = None
        end_line = current_line
        return result

    try:
        while characters.peek() is not None:
            char, line = consume()

            if state is _LexState.DASH_COMMENT or state is _LexState.HASH_COMMENT:
                if char == "\n":
                    state = _LexState.NORMAL
                    if buffer:
                        append_sql(char, line)
                continue

            if state is _LexState.BLOCK_COMMENT:
                if char == "*" and _peek_char(characters) == "/":
                    consume()
                    state = _LexState.NORMAL
                continue

            if state in {
                _LexState.SINGLE_QUOTE,
                _LexState.DOUBLE_QUOTE,
                _LexState.BACKTICK,
            }:
                append_sql(char, line, quoted=True)
                quote = {
                    _LexState.SINGLE_QUOTE: "'",
                    _LexState.DOUBLE_QUOTE: '"',
                    _LexState.BACKTICK: "`",
                }[state]
                if char == "\\":
                    if characters.peek() is not None:
                        escaped, escaped_line = consume()
                        append_sql(escaped, escaped_line, quoted=True)
                    continue
                if char == quote:
                    if _peek_char(characters) == quote:
                        doubled, doubled_line = consume()
                        append_sql(doubled, doubled_line, quoted=True)
                    else:
                        state = _LexState.NORMAL
                continue

            if char == "-" and _peek_char(characters) == "-":
                second, second_line = consume()
                following = characters.peek()
                if following is None or following[0].isspace():
                    separate_comment()
                    state = _LexState.DASH_COMMENT
                else:
                    append_sql(char, line)
                    append_sql(second, second_line)
                continue
            if char == "#":
                separate_comment()
                state = _LexState.HASH_COMMENT
                continue
            if char == "/" and _peek_char(characters) == "*":
                consume()
                separate_comment()
                state = _LexState.BLOCK_COMMENT
                continue
            if char == "'":
                append_sql(char, line, quoted=True)
                state = _LexState.SINGLE_QUOTE
                continue
            if char == '"':
                append_sql(char, line, quoted=True)
                state = _LexState.DOUBLE_QUOTE
                continue
            if char == "`":
                append_sql(char, line, quoted=True)
                state = _LexState.BACKTICK
                continue
            if char == ";":
                append_sql(char, line)
                result = emit()
                segment_chars = 0
                if result is not None:
                    yield result
                continue
            append_sql(char, line)
    except UnicodeDecodeError as exc:
        raise SqlEncodingError(
            path=path,
            line_number=current_line,
            statement_index=statement_index,
            reason="SQL file is not valid UTF-8",
            safe_summary="".join(buffer[-MAX_ERROR_SUMMARY_CHARS:]),
        ) from exc

    if state in {
        _LexState.SINGLE_QUOTE,
        _LexState.DOUBLE_QUOTE,
        _LexState.BACKTICK,
        _LexState.BLOCK_COMMENT,
    }:
        label = {
            _LexState.SINGLE_QUOTE: "single-quoted string",
            _LexState.DOUBLE_QUOTE: "double-quoted string",
            _LexState.BACKTICK: "backtick identifier",
            _LexState.BLOCK_COMMENT: "block comment",
        }[state]
        raise SqlSyntaxStateError(
            path=path,
            line_number=current_line,
            statement_index=statement_index,
            reason=f"unclosed {label}",
            safe_summary="".join(buffer[-MAX_ERROR_SUMMARY_CHARS:]),
        )
    result = emit()
    if result is not None:
        yield result


def _iter_decoded_characters(
    binary: BinaryIO,
    chunk_size: int,
) -> Iterator[tuple[str, int]]:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    line_number = 1
    while True:
        chunk = binary.read(chunk_size)
        if not chunk:
            break
        text = decoder.decode(chunk, final=False)
        for char in text:
            yield char, line_number
            if char == "\n":
                line_number += 1
    text = decoder.decode(b"", final=True)
    for char in text:
        yield char, line_number
        if char == "\n":
            line_number += 1


def classify_sql_statement(sql: str) -> tuple[SqlStatementType, str | None]:
    stripped = sql.lstrip()
    if re.match(r"SET\b", stripped, flags=re.IGNORECASE):
        return SqlStatementType.SET, None

    patterns = (
        (
            SqlStatementType.DROP_TABLE,
            r"DROP\s+TABLE(?:\s+IF\s+EXISTS)?\s+",
        ),
        (
            SqlStatementType.CREATE_TABLE,
            r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+",
        ),
        (
            SqlStatementType.INSERT,
            r"INSERT\s+INTO\s+",
        ),
    )
    for statement_type, pattern in patterns:
        match = re.match(pattern, stripped, flags=re.IGNORECASE)
        if match is None:
            continue
        table_name = _parse_qualified_identifier(stripped, match.end())
        if table_name is None:
            return SqlStatementType.OTHER, None
        return statement_type, table_name
    return SqlStatementType.OTHER, None


def _parse_qualified_identifier(sql: str, position: int) -> str | None:
    first = _parse_identifier_part(sql, position)
    if first is None:
        return None
    first_value, position = first
    position = _skip_whitespace(sql, position)
    if position >= len(sql) or sql[position] != ".":
        return first_value
    second_position = _skip_whitespace(sql, position + 1)
    second = _parse_identifier_part(sql, second_position)
    if second is None:
        return None
    second_value, _ = second
    return f"{first_value}.{second_value}"


def _parse_identifier_part(sql: str, position: int) -> tuple[str, int] | None:
    position = _skip_whitespace(sql, position)
    if position >= len(sql):
        return None
    if sql[position] == "`":
        values: list[str] = []
        cursor = position + 1
        while cursor < len(sql):
            char = sql[cursor]
            if char == "\\" and cursor + 1 < len(sql):
                values.append(sql[cursor + 1])
                cursor += 2
                continue
            if char == "`":
                if cursor + 1 < len(sql) and sql[cursor + 1] == "`":
                    values.append("`")
                    cursor += 2
                    continue
                return "".join(values), cursor + 1
            values.append(char)
            cursor += 1
        return None
    match = re.match(r"[\w$]+", sql[position:], flags=re.UNICODE)
    if match is None:
        return None
    return match.group(0), position + match.end()


def _skip_whitespace(value: str, position: int) -> int:
    while position < len(value) and value[position].isspace():
        position += 1
    return position


def _peek_char(characters: _PeekableCharacters) -> str | None:
    item = characters.peek()
    return None if item is None else item[0]


def _validate_positive_integer(value: object, name: str, path: Path) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SqlStreamError(
            path=path,
            line_number=1,
            statement_index=1,
            reason=f"{name} must be a positive integer",
        )


def _bounded_summary(value: str) -> str:
    compact = " ".join(value.split())
    if len(compact) <= MAX_ERROR_SUMMARY_CHARS:
        return compact
    return compact[-MAX_ERROR_SUMMARY_CHARS:]
