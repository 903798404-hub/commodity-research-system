from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from agri_research_agent.import_profit.config import (
    ContractOverrideConfig,
    ContractOverrideRule,
    load_soybean_config,
)
from agri_research_agent.import_profit.historical_dce_adapter import (
    HISTORICAL_DCE_CONTINUOUS_SCHEMA,
    PRICE_TYPE,
    SOURCE_TABLE,
    HistoricalDceError,
    adapt_historical_dce_sql,
    resolve_historical_dce_points,
)
from agri_research_agent.import_profit.models import BusinessKey, DceContract


REPOSITORY = Path(__file__).resolve().parents[1]
CONFIG = REPOSITORY / "02_configs" / "import_profit_soybean.yaml"
FIELDS = [
    "Date",
    "DCE_豆粕_01月",
    "DCE_豆粕_05月",
    "DCE_豆粕_09月",
    "DCE_豆油_01月",
    "DCE_豆油_05月",
    "DCE_豆油_09月",
]


def literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "'" + value + "'"
    return str(value)


def sql_fixture(
    path: Path,
    *,
    fields: list[str] | None = None,
    rows: list[list[object]] | None = None,
) -> Path:
    columns = list(fields or FIELDS)
    columns.append("DCE_豆粕_仓单")
    definitions = [
        f"`{column}` {'datetime' if index == 0 else 'decimal(18, 4)'} DEFAULT NULL"
        for index, column in enumerate(columns)
    ]
    source_rows = rows or [
        ["2026-06-10 00:00:00", 3001, 3005, 3009, 8001, 8005, 8009],
        ["2026-06-11 00:00:00", None, 3015, 3019, None, 8015, 8019],
    ]
    statements = [
        f"CREATE TABLE `{SOURCE_TABLE}` (\n  "
        + ",\n  ".join(definitions)
        + "\n);"
    ]
    for row in source_rows:
        complete = [*row, None]
        statements.append(
            f"INSERT INTO `{SOURCE_TABLE}` VALUES ("
            + ", ".join(literal(item) for item in complete)
            + ");"
        )
    path.write_text("\n".join(statements), encoding="utf-8")
    return path


def key(
    business_date: date,
    shipment_year: int,
    shipment_month: int,
    *,
    origin: str = "brazil",
) -> BusinessKey:
    config = load_soybean_config(CONFIG)
    return BusinessKey(
        business_date=business_date,
        commodity="soybean",
        origin=origin,
        shipment_year=shipment_year,
        shipment_month=shipment_month,
        allowed_origins=config.origin_codes,
        expected_commodity=config.commodity,
    )


def test_correct_ddl_extra_fields_null_and_all_six_continuous_series(
    tmp_path: Path,
) -> None:
    result = adapt_historical_dce_sql(sql_fixture(tmp_path / "dce.sql"))
    assert result.manifest_core["source_row_count"] == 2
    assert result.manifest_core["standard_record_count"] == 10
    assert result.quality_report["price_counts"] == {
        "valid": 10,
        "null": 2,
        "zero": 0,
        "negative": 0,
        "invalid": 0,
    }
    assert result.quality_report["candidate_status"] == "passed_with_warnings"
    assert result.quality_report["date_range"] == {
        "earliest": "2026-06-10",
        "latest": "2026-06-11",
    }
    assert list(HISTORICAL_DCE_CONTINUOUS_SCHEMA.names) == [
        "business_date",
        "instrument",
        "delivery_month",
        "price_cny_per_tonne",
        "price_type",
        "source",
        "source_table",
        "source_column",
        "source_snapshot_sha256",
    ]
    assert {item.delivery_month for item in result.records} == {1, 5, 9}
    assert {item.instrument for item in result.records} == {"soymeal", "soyoil"}
    assert all(item.price_type == PRICE_TYPE for item in result.records)
    assert all(item.source == "reuters_sql" for item in result.records)
    assert all(item.source_table == SOURCE_TABLE for item in result.records)
    assert [item.key for item in result.records] == sorted(
        item.key for item in result.records
    )


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        (FIELDS[:-1], "ddl_missing_required_columns"),
        ([FIELDS[0], FIELDS[2], FIELDS[1], *FIELDS[3:]], "ddl_required_column_order_mismatch"),
    ],
)
def test_missing_or_reordered_target_fields_are_fatal(
    tmp_path: Path, fields: list[str], code: str
) -> None:
    rows = [["2026-06-10", *range(1, len(fields))]]
    with pytest.raises(HistoricalDceError) as error:
        adapt_historical_dce_sql(
            sql_fixture(tmp_path / "invalid-ddl.sql", fields=fields, rows=rows)
        )
    assert any(
        issue["code"] == code
        for issue in error.value.quality_report["fatal_issues"]
    )


@pytest.mark.parametrize(
    ("replacement", "code"),
    [
        (0, "dce_zero_price"),
        (-1, "dce_negative_price"),
        ("bad", "dce_invalid_price"),
    ],
)
def test_zero_negative_or_non_numeric_price_is_fatal(
    tmp_path: Path, replacement: object, code: str
) -> None:
    rows = [["2026-06-10", replacement, 2, 3, 4, 5, 6]]
    with pytest.raises(HistoricalDceError) as error:
        adapt_historical_dce_sql(
            sql_fixture(tmp_path / "invalid-price.sql", rows=rows)
        )
    assert any(
        issue["code"] == code
        for issue in error.value.quality_report["fatal_issues"]
    )


def test_duplicate_date_and_standard_keys_are_fatal(tmp_path: Path) -> None:
    rows = [
        ["2026-06-10", 1, 2, 3, 4, 5, 6],
        ["2026-06-10", 11, 12, 13, 14, 15, 16],
    ]
    with pytest.raises(HistoricalDceError) as error:
        adapt_historical_dce_sql(
            sql_fixture(tmp_path / "duplicate.sql", rows=rows)
        )
    assert error.value.quality_report["duplicate_date_count"] == 1
    assert error.value.quality_report["duplicate_key_count"] == 6


def test_resolution_uses_exact_date_mapping_full_contracts_and_deduplication(
    tmp_path: Path,
) -> None:
    result = adapt_historical_dce_sql(sql_fixture(tmp_path / "dce.sql"))
    config = load_soybean_config(CONFIG)
    keys = (
        key(date(2026, 6, 10), 2026, 12),
        key(date(2026, 6, 10), 2027, 1),
        key(date(2026, 6, 10), 2026, 8),
        key(date(2026, 6, 10), 2026, 9, origin="us_gulf"),
    )
    original = tuple(keys)
    resolved = resolve_historical_dce_points(
        keys, config=config, continuous_points=result.records
    )
    assert keys == original
    by_code = {item.contract_code: item for item in resolved}
    assert set(by_code) == {
        "M2605", "Y2605", "M2701", "Y2701", "M2705", "Y2705"
    }
    assert by_code["M2605"].price_cny_per_tonne == 3005
    assert by_code["Y2605"].price_cny_per_tonne == 8005
    assert by_code["M2701"].price_cny_per_tonne == 3001
    assert by_code["Y2701"].price_cny_per_tonne == 8001
    assert by_code["M2705"].price_cny_per_tonne == 3005
    assert by_code["Y2705"].price_cny_per_tonne == 8005
    assert all(item.source == "reuters_sql" for item in resolved)
    assert all(item.price_type == PRICE_TYPE for item in resolved)
    assert all(
        item.contract_identity_status == "continuous_inferred"
        for item in resolved
    )
    assert all(item.source_contract_code is None for item in resolved)
    assert all(
        item.quote_date_evidence_status == "source_confirmed"
        and item.is_usable
        for item in resolved
    )
    assert {
        (item.contract_code, item.source_delivery_month)
        for item in resolved
    } == {
        ("M2605", 5),
        ("Y2605", 5),
        ("M2701", 1),
        ("Y2701", 1),
        ("M2705", 5),
        ("Y2705", 5),
    }
    assert [item.key for item in resolved] == sorted(item.key for item in resolved)


def test_historical_resolution_uses_effective_override_delivery_month(tmp_path):
    result = adapt_historical_dce_sql(sql_fixture(tmp_path / "dce.sql"))
    config = load_soybean_config(CONFIG)
    overridden = replace(
        config,
        contract_override=ContractOverrideConfig(
            True,
            (
                ContractOverrideRule(
                    origin="brazil",
                    shipment_year=2026,
                    shipment_month=12,
                    effective_from_business_date=date(2026, 6, 10),
                    effective_to_business_date=None,
                    cbot_contract=None,
                    soymeal_contract=DceContract.soymeal(2028, 9),
                    soyoil_contract=None,
                    reason="source contract anomaly",
                ),
            ),
        ),
    )
    business_key = key(date(2026, 6, 10), 2026, 12)

    resolved = resolve_historical_dce_points(
        [business_key],
        config=overridden,
        continuous_points=result.records,
    )
    by_code = {item.contract_code: item for item in resolved}

    assert by_code["M2809"].price_cny_per_tonne == 3009
    assert by_code["M2809"].contract_identity_status == "continuous_inferred"
    assert "M2701" not in by_code
    assert by_code["Y2701"].price_cny_per_tonne == 8001


def test_missing_exact_date_price_is_not_filled_and_extra_source_dates_do_not_expand(
    tmp_path: Path,
) -> None:
    result = adapt_historical_dce_sql(sql_fixture(tmp_path / "dce.sql"))
    config = load_soybean_config(CONFIG)
    resolved = resolve_historical_dce_points(
        (
            key(date(2026, 6, 11), 2026, 12),
            key(date(2026, 6, 11), 2027, 1),
        ),
        config=config,
        continuous_points=result.records,
    )
    assert {item.contract_code for item in resolved} == {"M2705", "Y2705"}
    assert all(item.business_date == date(2026, 6, 11) for item in resolved)
    assert {item.price_cny_per_tonne for item in resolved} == {3015, 8015}
    assert not any(item.business_date == date(2026, 6, 10) for item in resolved)


def test_empty_explicit_business_keys_generate_no_resolved_points(
    tmp_path: Path,
) -> None:
    result = adapt_historical_dce_sql(sql_fixture(tmp_path / "dce.sql"))
    assert (
        resolve_historical_dce_points(
            (), config=load_soybean_config(CONFIG), continuous_points=result.records
        )
        == ()
    )
