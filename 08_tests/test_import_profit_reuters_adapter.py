from __future__ import annotations

import hashlib
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from agri_research_agent.data_sources.navicat_sql_stream import (
    SqlStatement,
    SqlStatementType,
)
from agri_research_agent.import_profit import reuters_adapter as adapter
from agri_research_agent.import_profit.reuters_adapter import (
    CBOT_TABLE,
    FX_TABLE,
    ReutersDdlError,
    ReutersQualityError,
    ReutersValuesError,
    adapt_reuters_sql,
    calculate_lead_months,
    classify_cbot_quality,
    extract_create_columns,
    parse_insert_values,
)


CBOT_MONTHS = (1, 3, 5, 7, 8, 9, 11)
EXPECTED_CBOT_COLUMNS = [
    "Date",
    *[
        f"CBOT大豆 {year}年{month}月"
        for year in range(2000, 2029)
        for month in CBOT_MONTHS
    ],
]
EXPECTED_FX_COLUMNS = [
    "Date",
    "Spot",
    "Fwd_1M",
    "Fwd_2M",
    "Fwd_3M",
    "Fwd_4M",
    "Fwd_5M",
    "Fwd_6M",
    "Fwd_7M",
    "Fwd_8M",
    "Fwd_9M",
    "Fwd_10M",
    "Fwd_11M",
    "Fwd_1Y",
]


def ddl(table: str, columns: list[str]) -> str:
    definitions = []
    for index, column in enumerate(columns):
        data_type = "datetime" if index == 0 else "decimal(18, 6)"
        definitions.append(f"  `{column}` {data_type} DEFAULT NULL COMMENT 'x,y'")
    definitions.append("  PRIMARY KEY (`Date`)")
    definitions.append("  UNIQUE KEY `unique_date` (`Date`)")
    definitions.append("  KEY `date_index` (`Date`)")
    definitions.append("  INDEX `date_index_alias` (`Date` ASC) USING BTREE")
    definitions.append("  CONSTRAINT `date_check` CHECK ((`Date` is not null))")
    return f"CREATE TABLE `{table}` (\n" + ",\n".join(definitions) + "\n);"


def literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return str(value)


def insert(table: str, values: list[object], columns: list[str] | None = None) -> str:
    fields = ""
    if columns is not None:
        fields = " (" + ", ".join(f"`{column}`" for column in columns) + ")"
    return f"INSERT INTO `{table}`{fields} VALUES (" + ", ".join(map(literal, values)) + ");"


def cbot_values(
    market_date: str = "2026-01-02",
    overrides: dict[str, object] | None = None,
) -> list[object]:
    values: list[object] = [market_date, *([None] * 203)]
    for column, value in (overrides or {}).items():
        values[EXPECTED_CBOT_COLUMNS.index(column)] = value
    return values


def fx_values(
    market_date: str = "2026-01-02",
    overrides: dict[str, object] | None = None,
) -> list[object]:
    values: list[object] = [market_date, *[7.0 + tenor / 100 for tenor in range(13)]]
    for column, value in (overrides or {}).items():
        values[EXPECTED_FX_COLUMNS.index(column)] = value
    return values


def synthetic_sql(
    *,
    cbot_columns: list[str] | None = None,
    fx_columns: list[str] | None = None,
    cbot_rows: list[list[object]] | None = None,
    fx_rows: list[list[object]] | None = None,
    reverse_tables: bool = False,
    include_other: bool = True,
) -> str:
    cbot_columns = cbot_columns or EXPECTED_CBOT_COLUMNS
    fx_columns = fx_columns or EXPECTED_FX_COLUMNS
    cbot_rows = cbot_rows or [
        cbot_values(overrides={"CBOT大豆 2027年1月": 1200.5})
    ]
    fx_rows = fx_rows or [fx_values()]
    sections = {
        CBOT_TABLE: "\n".join(
            [ddl(CBOT_TABLE, cbot_columns), *[insert(CBOT_TABLE, row) for row in cbot_rows]]
        ),
        FX_TABLE: "\n".join(
            [ddl(FX_TABLE, fx_columns), *[insert(FX_TABLE, row) for row in fx_rows]]
        ),
    }
    ordered = [FX_TABLE, CBOT_TABLE] if reverse_tables else [CBOT_TABLE, FX_TABLE]
    content = [
        "Navicat Premium Dump SQL",
        "Date: 28/07/2026 09:23:50",
        "SET NAMES utf8mb4;",
    ]
    if include_other:
        content.extend(
            [
                "CREATE TABLE `ignored` (`Date` datetime, `value` int);",
                "INSERT INTO `ignored` VALUES ('2026-01-02', 1);",
            ]
        )
    content.extend(sections[table] for table in ordered)
    return "\n".join(content) + "\n"


def write_sql(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "fixture.sql"
    path.write_text(content, encoding="utf-8")
    return path


def make_statement(sql: str, table: str = "t", index: int = 7) -> SqlStatement:
    return SqlStatement(sql, SqlStatementType.INSERT, table, 1, 1, index)


def test_correct_target_ddls_extract_only_business_fields() -> None:
    cbot_statement = SqlStatement(
        ddl(CBOT_TABLE, EXPECTED_CBOT_COLUMNS),
        SqlStatementType.CREATE_TABLE,
        CBOT_TABLE,
        1,
        210,
        1,
    )
    fx_statement = SqlStatement(
        ddl(FX_TABLE, EXPECTED_FX_COLUMNS),
        SqlStatementType.CREATE_TABLE,
        FX_TABLE,
        1,
        20,
        2,
    )
    assert extract_create_columns(cbot_statement) == tuple(EXPECTED_CBOT_COLUMNS)
    assert extract_create_columns(fx_statement) == tuple(EXPECTED_FX_COLUMNS)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda columns: columns[:-1],
        lambda columns: [*columns, "extra"],
        lambda columns: [columns[0], columns[2], columns[1], *columns[3:]],
        lambda columns: [*columns[:-1], columns[1]],
        lambda columns: [columns[0], "CBOT大豆 2000年2月", *columns[2:]],
    ],
)
def test_cbot_ddl_fingerprint_rejects_missing_extra_reordered_duplicate_or_illegal_month(
    tmp_path: Path,
    mutate,
) -> None:
    content = synthetic_sql(cbot_columns=mutate(EXPECTED_CBOT_COLUMNS.copy()))
    with pytest.raises(ReutersDdlError, match="fingerprint mismatch"):
        adapt_reuters_sql(write_sql(tmp_path, content))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda columns: columns[:-1],
        lambda columns: [*columns, "extra"],
        lambda columns: [columns[0], columns[2], columns[1], *columns[3:]],
    ],
)
def test_fx_ddl_fingerprint_rejects_missing_extra_or_reordered_fields(
    tmp_path: Path,
    mutate,
) -> None:
    content = synthetic_sql(fx_columns=mutate(EXPECTED_FX_COLUMNS.copy()))
    with pytest.raises(ReutersDdlError, match="fingerprint mismatch"):
        adapt_reuters_sql(write_sql(tmp_path, content))


def test_values_parser_supports_numbers_null_strings_escapes_and_explicit_fields() -> None:
    columns = ("Date", "a", "b", "c", "d", "e", "f")
    statement = make_statement(
        "INSERT INTO `t` (`f`, `Date`, `b`, `a`, `e`, `d`, `c`) "
        "VALUES ('中文,括号(值);', '2026-01-02', -2, NULL, 1.25e2, "
        "'it\\'s', 'a''b');"
    )
    assert parse_insert_values(statement, columns) == (
        "2026-01-02",
        None,
        -2,
        "a'b",
        "it's",
        125.0,
        "中文,括号(值);",
    )


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("INSERT INTO `t` VALUES (1, 2);", "value count"),
        ("INSERT INTO `t` VALUES (1, 2, 3, 4);", "value count"),
        ("INSERT INTO `t` (`Date`, `unknown`, `a`) VALUES (1, 2, 3);", "unknown"),
        ("INSERT INTO `t` (`Date`, `Date`, `a`) VALUES (1, 2, 3);", "duplicates"),
        ("INSERT INTO `t` VALUES (1, 2, 3), (4, 5, 6);", "multi-row"),
    ],
)
def test_values_parser_rejects_wrong_counts_unknown_duplicate_and_multirow(
    sql: str,
    message: str,
) -> None:
    with pytest.raises(ReutersValuesError, match=message):
        parse_insert_values(make_statement(sql), ("Date", "a", "b"))


def test_adapter_skips_non_targets_and_accepts_target_table_order_changes(tmp_path: Path) -> None:
    result = adapt_reuters_sql(write_sql(tmp_path, synthetic_sql(reverse_tables=True)))
    assert result.manifest_core["target_tables"] == [CBOT_TABLE, FX_TABLE]
    assert result.manifest_core["source_table_rows"] == {CBOT_TABLE: 1, FX_TABLE: 1}
    assert all(record.source_table in {CBOT_TABLE, FX_TABLE} for record in (*result.cbot_records, *result.fx_records))


def test_missing_target_duplicate_create_and_duplicate_source_date_fail(tmp_path: Path) -> None:
    missing_fx = "\n".join(
            [
                "Date: 28/07/2026 09:23:50",
                "SET NAMES utf8mb4;",
                ddl(CBOT_TABLE, EXPECTED_CBOT_COLUMNS),
                insert(CBOT_TABLE, cbot_values()),
            ]
    )
    with pytest.raises(ReutersDdlError, match="missing or duplicated"):
        adapt_reuters_sql(write_sql(tmp_path, missing_fx))

    duplicate_create = synthetic_sql() + ddl(CBOT_TABLE, EXPECTED_CBOT_COLUMNS)
    with pytest.raises(ReutersDdlError, match="duplicate target CREATE"):
        adapt_reuters_sql(write_sql(tmp_path, duplicate_create))

    duplicate_date = synthetic_sql(
        cbot_rows=[
            cbot_values("2026-01-02"),
            cbot_values("2026-01-02", {"CBOT大豆 2027年3月": 1201}),
        ]
    )
    with pytest.raises(ReutersQualityError, match="duplicate source date"):
        adapt_reuters_sql(write_sql(tmp_path, duplicate_date))


def test_invalid_date_fails_with_statement_trace(tmp_path: Path) -> None:
    content = synthetic_sql(cbot_rows=[cbot_values("2026-02-30")])
    with pytest.raises(ReutersQualityError, match="Date value is invalid") as exc_info:
        adapt_reuters_sql(write_sql(tmp_path, content))
    assert exc_info.value.statement_index is not None
    assert exc_info.value.table_name == CBOT_TABLE


def test_cbot_wide_to_long_null_nonpositive_and_exact_lead_rules(tmp_path: Path) -> None:
    content = synthetic_sql(
        cbot_rows=[
            cbot_values(
                "1969-06-26",
                {
                    "CBOT大豆 2007年11月": 633,
                    "CBOT大豆 2000年1月": None,
                },
            ),
            cbot_values(
                "2025-01-02",
                {
                    "CBOT大豆 2027年1月": 1200,
                    "CBOT大豆 2028年3月": 0,
                    "CBOT大豆 2028年5月": -1,
                },
            ),
        ]
    )
    result = adapt_reuters_sql(write_sql(tmp_path, content))
    records = result.cbot_records
    by_key = {
        (record.market_date, record.contract_year, record.contract_month): record
        for record in records
    }
    anomaly = by_key[(date(1969, 6, 26), 2007, 11)]
    normal_early = by_key[(date(2025, 1, 2), 2027, 1)]
    assert anomaly.price_cents_per_bushel == 633
    assert anomaly.lead_months == 461
    assert anomaly.exchange_quality_status == "extreme_source_anomaly"
    assert anomaly.is_usable is False
    assert anomaly.eligible_for_import_profit is False
    assert normal_early.is_usable is True
    assert normal_early.eligible_for_import_profit is False
    assert (
        by_key[(date(2025, 1, 2), 2028, 3)].exchange_quality_status
        == "zero_price"
    )
    assert (
        by_key[(date(2025, 1, 2), 2028, 5)].exchange_quality_status
        == "negative_price"
    )
    assert all(not hasattr(record, "ric") for record in records)
    assert all(record.price_name == "CBOT日度价格" for record in records)
    assert all(record.unit == "cents_per_bushel" for record in records)
    assert result.quality_report["interpolation_performed"] is False


@pytest.mark.parametrize(
    ("lead_months", "status", "is_usable", "eligible"),
    [
        (-1, "invalid_negative_lead", False, False),
        (0, "standard_window", True, True),
        (12, "standard_window", True, True),
        (13, "standard_window", True, False),
        (42, "standard_window", True, False),
        (43, "extended_window_unverified", True, False),
        (48, "extended_window_unverified", True, False),
        (49, "extreme_window_warning", False, False),
        (299, "extreme_window_warning", False, False),
        (300, "extreme_source_anomaly", False, False),
        (461, "extreme_source_anomaly", False, False),
    ],
)
def test_exact_lead_quality_boundaries(
    lead_months: int,
    status: str,
    is_usable: bool,
    eligible: bool,
) -> None:
    assert classify_cbot_quality(lead_months) == (status, is_usable, eligible)


def test_exact_lead_month_calculation_and_old_integer_year_rule_no_longer_classifies(
    tmp_path: Path,
) -> None:
    assert calculate_lead_months(date(2024, 12, 31), 2028, 7) == 43
    assert calculate_lead_months(date(2026, 2, 1), 2026, 1) == -1
    content = synthetic_sql(
        cbot_rows=[
            cbot_values(
                "2022-12-30",
                {"CBOT大豆 2026年1月": 1200},
            )
        ]
    )
    record = adapt_reuters_sql(write_sql(tmp_path, content)).cbot_records[0]
    assert record.lead_months == 37
    assert record.exchange_quality_status == "standard_window"
    assert record.is_usable is True
    assert record.eligible_for_import_profit is False


def test_warning_categories_do_not_fail_but_unknown_extreme_does(
    tmp_path: Path,
) -> None:
    warning_content = synthetic_sql(
        cbot_rows=[
            cbot_values(
                "1969-06-26",
                {"CBOT大豆 2007年11月": 633},
            ),
            cbot_values(
                "2024-12-31",
                {
                    "CBOT大豆 2028年7月": 1050.25,
                },
            ),
            cbot_values(
                "2002-06-21",
                {"CBOT大豆 2006年7月": 600},
            ),
        ]
    )
    warning_result = adapt_reuters_sql(write_sql(tmp_path, warning_content))
    assert warning_result.quality_report["candidate_status"] == "passed_with_warnings"
    assert warning_result.quality_report["candidate_pass"] is True
    assert warning_result.quality_report["known_extreme_anomaly_count"] == 1
    assert warning_result.quality_report["unknown_extreme_anomaly_count"] == 0
    assert {
        warning["code"] for warning in warning_result.quality_report["warnings"]
    } >= {
        "extended_window_unverified",
        "extreme_window_warning",
        "known_extreme_source_anomaly",
    }

    unknown_content = synthetic_sql(
        cbot_rows=[
            cbot_values(
                "1969-06-26",
                {"CBOT大豆 2008年7月": 633},
            )
        ]
    )
    unknown_result = adapt_reuters_sql(write_sql(tmp_path, unknown_content))
    assert unknown_result.quality_report["candidate_status"] == "failed"
    assert unknown_result.quality_report["candidate_pass"] is False
    assert unknown_result.quality_report["unknown_extreme_anomaly_count"] == 1


def test_clean_candidate_status_is_passed(tmp_path: Path) -> None:
    result = adapt_reuters_sql(write_sql(tmp_path, synthetic_sql()))
    assert result.quality_report["candidate_status"] == "passed"
    assert result.quality_report["fatal_issues"] == []
    assert result.quality_report["warnings"] == []


def test_fx_maps_spot_through_one_year_without_interpolation(tmp_path: Path) -> None:
    content = synthetic_sql(
        fx_rows=[fx_values(overrides={"Fwd_5M": None, "Fwd_6M": 0, "Fwd_7M": -1})]
    )
    result = adapt_reuters_sql(write_sql(tmp_path, content))
    by_tenor = {record.tenor_months: record for record in result.fx_records}
    assert sorted(by_tenor) == [0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12]
    assert by_tenor[0].source_column == "Spot"
    assert by_tenor[12].source_column == "Fwd_1Y"
    assert by_tenor[6].quality_status == "zero_fx"
    assert by_tenor[7].quality_status == "negative_fx"
    assert 5 not in by_tenor
    assert result.quality_report["interpolation_performed"] is False


def test_traceability_sorting_sha_and_bounded_quality_samples(tmp_path: Path) -> None:
    zero_columns = [f"CBOT大豆 2000年{month}月" for month in CBOT_MONTHS]
    content = synthetic_sql(
        cbot_rows=[
            cbot_values(
                "2026-01-03",
                {column: 0 for column in zero_columns},
            ),
            cbot_values("2026-01-02", {"CBOT大豆 2027年1月": 1200}),
        ]
    )
    path = write_sql(tmp_path, content)
    result = adapt_reuters_sql(path)
    keys = [
        (record.market_date, record.contract_year, record.contract_month)
        for record in result.cbot_records
    ]
    assert keys == sorted(keys)
    expected_sha = hashlib.sha256(path.read_bytes()).hexdigest().upper()
    assert all(record.source_snapshot_sha256 == expected_sha for record in result.cbot_records)
    assert all(record.source_statement_index > 0 for record in result.cbot_records)
    samples = result.quality_report["quality_issue_samples"][f"{CBOT_TABLE}:zero_price"]
    assert len(samples) == 5


def test_duplicate_standard_key_guard_is_explicit() -> None:
    source = adapter.SourceIdentity("fixture.sql", 1, "A" * 64, "2026-01-01T00:00:00+00:00", None)
    statement = make_statement("INSERT INTO `x` VALUES (1);", CBOT_TABLE, 9)
    with pytest.raises(ReutersQualityError, match="duplicate CBOT standard key"):
        adapter._normalize_cbot_row(
            statement,
            date(2026, 1, 2),
            tuple(EXPECTED_CBOT_COLUMNS),
            tuple(cbot_values(overrides={"CBOT大豆 2027年1月": 1200})),
            source,
            [],
            {(date(2026, 1, 2), 2027, 1)},
            Counter(),
            Counter(),
            {},
        )
