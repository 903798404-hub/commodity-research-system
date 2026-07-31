from __future__ import annotations

from pathlib import Path

import pytest

from agri_research_agent.data_sources.navicat_sql_stream import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_MAX_STATEMENT_CHARS,
    SqlEncodingError,
    SqlFileError,
    SqlStatementTooLargeError,
    SqlStatementType,
    SqlStreamError,
    SqlSyntaxStateError,
    iter_sql_statements,
)


def write_bytes(tmp_path: Path, value: bytes, name: str = "dump.sql") -> Path:
    path = tmp_path / name
    path.write_bytes(value)
    return path


def write_sql(tmp_path: Path, value: str, *, newline: str | None = None) -> Path:
    if newline is not None:
        value = value.replace("\n", newline)
    return write_bytes(tmp_path, value.encode("utf-8"))


def parse(tmp_path: Path, value: str, *, chunk_size: int = 7):
    return list(iter_sql_statements(write_sql(tmp_path, value), chunk_size=chunk_size))


def test_resource_defaults_are_bounded_and_above_reviewed_real_insert() -> None:
    assert DEFAULT_CHUNK_SIZE == 64 * 1024
    assert DEFAULT_MAX_STATEMENT_CHARS == 1024 * 1024
    assert DEFAULT_MAX_STATEMENT_CHARS > 2147


@pytest.mark.parametrize("text", ["", "-- only comment\n", "# only comment", "/* only; comment */"])
def test_empty_and_comment_only_files_return_no_statements(tmp_path: Path, text: str) -> None:
    assert parse(tmp_path, text) == []


def test_basic_statement_types_and_table_names(tmp_path: Path) -> None:
    statements = parse(
        tmp_path,
        """
SET NAMES utf8mb4;
DROP TABLE IF EXISTS `旧表`;
CREATE TABLE `中文表` (`值` varchar(20));
INSERT INTO `中文表` VALUES (1);
ANALYZE TABLE `中文表`;
""",
    )
    assert [item.statement_type for item in statements] == [
        SqlStatementType.SET,
        SqlStatementType.DROP_TABLE,
        SqlStatementType.CREATE_TABLE,
        SqlStatementType.INSERT,
        SqlStatementType.OTHER,
    ]
    assert [item.table_name for item in statements] == [None, "旧表", "中文表", "中文表", None]


def test_explicit_columns_schema_qualified_and_case_insensitive_names(tmp_path: Path) -> None:
    statements = parse(
        tmp_path,
        """
create table `行情`.`日报` (`字段1` int);
InSeRt InTo `行情`.`日报` (`字段1`, `字段2`) VALUES (1, 2);
drop table if exists bare_schema.bare_table;
""",
    )
    assert [(item.statement_type, item.table_name) for item in statements] == [
        (SqlStatementType.CREATE_TABLE, "行情.日报"),
        (SqlStatementType.INSERT, "行情.日报"),
        (SqlStatementType.DROP_TABLE, "bare_schema.bare_table"),
    ]


def test_multiple_statements_single_line_cross_line_and_no_final_semicolon(tmp_path: Path) -> None:
    statements = parse(
        tmp_path,
        "SET NAMES utf8; INSERT\nINTO `t` VALUES\n(1); INSERT INTO `t` VALUES (2)",
        chunk_size=3,
    )
    assert len(statements) == 3
    assert statements[0].statement_type is SqlStatementType.SET
    assert statements[1].statement_type is SqlStatementType.INSERT
    assert statements[2].statement_type is SqlStatementType.INSERT
    assert statements[2].sql.endswith("VALUES (2)")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_lf_crlf_line_numbers_and_no_trailing_newline(tmp_path: Path, newline: str) -> None:
    path = write_sql(
        tmp_path,
        "-- comment\nINSERT INTO `t` VALUES (\n1\n);",
        newline=newline,
    )
    statement = list(iter_sql_statements(path, chunk_size=2))[0]
    assert statement.start_line == 2
    assert statement.end_line == 4
    assert statement.statement_index == 1


@pytest.mark.parametrize(
    "literal",
    [
        "'abc;def'",
        '"abc;def"',
        "'it\\\\\\'s valid'",
        "'backslash\\\\\\\\value'",
        "'中文；分号'",
        "'line1\\nline2'",
        "'a''b'",
        "'a,b'",
        "'a(b)c'",
    ],
)
def test_semicolons_escapes_doubled_quotes_and_text_values_stay_in_one_statement(
    tmp_path: Path,
    literal: str,
) -> None:
    statements = parse(tmp_path, f"INSERT INTO `t` VALUES ({literal}, NULL, -1, 1.2e-3);", chunk_size=1)
    assert len(statements) == 1
    assert statements[0].statement_type is SqlStatementType.INSERT
    assert statements[0].table_name == "t"


def test_backtick_semicolon_is_identifier_content_not_statement_end(tmp_path: Path) -> None:
    statements = parse(tmp_path, "INSERT INTO `table;name` (`column;name`) VALUES (1);", chunk_size=1)
    assert len(statements) == 1
    assert statements[0].table_name == "table;name"


def test_line_and_block_comment_semicolons_do_not_end_statement(tmp_path: Path) -> None:
    statements = parse(
        tmp_path,
        """
INSERT /* block ; comment */ INTO `t`
VALUES (1) -- line ; comment
;
# separate ; comment
INSERT INTO `t` VALUES (2);
""",
        chunk_size=2,
    )
    assert len(statements) == 2
    assert all(item.statement_type is SqlStatementType.INSERT for item in statements)


def test_minus_expression_is_not_mistaken_for_dash_comment(tmp_path: Path) -> None:
    statement = parse(tmp_path, "SELECT 3--2;", chunk_size=1)[0]
    assert statement.statement_type is SqlStatementType.OTHER
    assert statement.sql == "SELECT 3--2;"


def test_navicat_record_comment_does_not_hide_first_insert(tmp_path: Path) -> None:
    statement = parse(
        tmp_path,
        """-- ----------------------------
-- Records of table xxx
-- ----------------------------
INSERT INTO `xxx` VALUES (1);
""",
        chunk_size=4096,
    )[0]
    assert statement.statement_type is SqlStatementType.INSERT
    assert statement.table_name == "xxx"
    assert statement.start_line == 4


def test_all_37_navicat_table_comments_preserve_each_first_insert(tmp_path: Path) -> None:
    groups = []
    for index in range(37):
        groups.append(
            "-- ----------------------------\n"
            f"-- Records of table table_{index:02d}\n"
            "-- ----------------------------\n"
            f"INSERT INTO `table_{index:02d}` VALUES ({index});\n"
        )
    statements = parse(tmp_path, "".join(groups), chunk_size=11)
    assert len(statements) == 37
    assert [statement.table_name for statement in statements] == [
        f"table_{index:02d}" for index in range(37)
    ]
    assert [statement.statement_index for statement in statements] == list(range(1, 38))


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 8])
def test_comment_delimiters_quotes_escapes_and_utf8_cross_chunk_boundaries(
    tmp_path: Path,
    chunk_size: int,
) -> None:
    sql = (
        "-- comment\n"
        "/* block comment */\n"
        "INSERT INTO `中文表` VALUES ('跨块中文', 'it\\\\\\'s', 'a''b', \"x;y\");"
    )
    statements = parse(tmp_path, sql, chunk_size=chunk_size)
    assert len(statements) == 1
    assert statements[0].statement_type is SqlStatementType.INSERT
    assert statements[0].table_name == "中文表"


def test_multiline_string_keeps_statement_line_range(tmp_path: Path) -> None:
    statement = parse(tmp_path, "INSERT INTO `t` VALUES ('line1\nline2');", chunk_size=1)[0]
    assert statement.start_line == 1
    assert statement.end_line == 2


@pytest.mark.parametrize(
    ("sql", "label"),
    [
        ("INSERT INTO `t` VALUES ('open);", "single-quoted string"),
        ('INSERT INTO `t` VALUES ("open);', "double-quoted string"),
        ("INSERT INTO `open VALUES (1);", "backtick identifier"),
        ("/* open comment", "block comment"),
    ],
)
def test_unclosed_lexical_states_fail_with_bounded_metadata(
    tmp_path: Path,
    sql: str,
    label: str,
) -> None:
    path = write_sql(tmp_path, sql)
    with pytest.raises(SqlSyntaxStateError, match=label) as exc_info:
        list(iter_sql_statements(path, chunk_size=1))
    error = exc_info.value
    assert error.path == path
    assert error.line_number >= 1
    assert error.statement_index == 1
    assert len(error.safe_summary) <= 120
    assert len(str(error)) < 500


def test_invalid_utf8_fails_without_replacement(tmp_path: Path) -> None:
    path = write_bytes(tmp_path, b"INSERT INTO `t` VALUES ('ok');\xff")
    with pytest.raises(SqlEncodingError, match="not valid UTF-8") as exc_info:
        list(iter_sql_statements(path, chunk_size=1))
    assert exc_info.value.path == path


def test_missing_file_and_non_file_path_fail_clearly(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sql"
    with pytest.raises(SqlFileError, match="does not exist"):
        list(iter_sql_statements(missing))
    with pytest.raises(SqlFileError, match="not a regular file"):
        list(iter_sql_statements(tmp_path))


def test_statement_size_guard_bounds_unterminated_accumulation(tmp_path: Path) -> None:
    path = write_sql(tmp_path, "INSERT INTO `t` VALUES ('1234567890');")
    with pytest.raises(SqlStatementTooLargeError, match="exceeds 10 characters") as exc_info:
        list(iter_sql_statements(path, chunk_size=2, max_statement_chars=10))
    assert exc_info.value.statement_index == 1
    assert len(exc_info.value.safe_summary) <= 120


@pytest.mark.parametrize("chunk_size", [0, -1, True, 1.5, "64"])
def test_invalid_chunk_size_is_rejected(tmp_path: Path, chunk_size: object) -> None:
    path = write_sql(tmp_path, "SET NAMES utf8;")
    with pytest.raises(SqlStreamError, match="chunk_size must be a positive integer"):
        list(iter_sql_statements(path, chunk_size=chunk_size))  # type: ignore[arg-type]


def test_statement_indices_and_comment_adjusted_start_lines_are_stable(tmp_path: Path) -> None:
    statements = parse(
        tmp_path,
        "-- heading\nSET NAMES utf8;\n\n/* table */\nINSERT INTO `t` VALUES (1);",
        chunk_size=4,
    )
    assert [(item.statement_index, item.start_line, item.end_line) for item in statements] == [
        (1, 2, 2),
        (2, 5, 5),
    ]


def test_iterator_is_lazy_and_does_not_open_file_until_iteration(tmp_path: Path) -> None:
    path = tmp_path / "created_later.sql"
    iterator = iter_sql_statements(path, chunk_size=1)
    path.write_text("SET NAMES utf8;", encoding="utf-8")
    statements = list(iterator)
    assert len(statements) == 1
    assert statements[0].statement_type is SqlStatementType.SET
