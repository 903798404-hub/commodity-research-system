from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agri_research_agent.data_sources.basis_database import DATABASE_COLUMNS
from agri_research_agent.data_sources.basis_sql_loader import (
    EXPECTED_SOURCE_COLUMNS,
    PAGE_KEY,
    STABLE_KEY,
    build_basis_candidate,
)


PRODUCTS = ["大豆油", "菜籽油", "棕榈油", "豆粕", "菜粕"]


def _sql_value(value: object) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _row(
    product: str,
    *,
    date: str = "2026-08-04",
    region: str = "华东",
    delivery: str = "8月",
    contract: str | None = "2609",
    basis: float | None = 10,
    quote_type: str = "现货基差",
    factory: str = "工厂A",
    created_at: str = "2027-01-01 00:00:00",
) -> list[object]:
    return [
        date,
        product,
        f"article-{product}-{factory}-{contract}-{basis}",
        "标题",
        "工厂",
        region,
        "省份",
        factory,
        "合同",
        date[:4],
        delivery,
        "原始价格",
        contract,
        basis,
        8000,
        1,
        created_at,
        quote_type,
        7900,
    ]


def _write_sql(path: Path, rows: list[list[object]]) -> None:
    definitions = {
        "日期": "date NOT NULL",
        "品种": "varchar(20) NOT NULL",
        "文章ID": "varchar(100) NOT NULL",
        "文章标题": "varchar(200) NULL",
        "行类型": "varchar(20) NULL",
        "地区": "varchar(50) NULL",
        "省份": "varchar(50) NULL",
        "工厂": "varchar(100) NULL",
        "合同情况": "varchar(200) NULL",
        "基差合同年": "varchar(20) NULL",
        "基差合同月": "varchar(50) NULL",
        "价格原始": "varchar(100) NULL",
        "期货合约": "varchar(10) NULL",
        "基差": "decimal(10,2) NULL",
        "现货价": "decimal(10,2) NULL",
        "成交量": "decimal(15,2) NULL",
        "created_at": "datetime NULL",
        "报价类别": "varchar(20) NULL",
        "期货收盘价": "decimal(10,2) NULL",
    }
    lines = ["CREATE TABLE `basis_price`  ("]
    for index, column in enumerate(EXPECTED_SOURCE_COLUMNS):
        comma = "," if index < len(EXPECTED_SOURCE_COLUMNS) - 1 else ""
        lines.append(f"  `{column}` {definitions[column]}{comma}")
    lines.append(") ENGINE = InnoDB;")
    lines.extend(
        "INSERT INTO `basis_price` VALUES ("
        + ", ".join(_sql_value(value) for value in row)
        + ");"
        for row in rows
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _representative_rows() -> list[list[object]]:
    rows = [_row(product) for product in PRODUCTS]
    rows.extend(
        [
            _row("大豆油", basis=0, factory="工厂B"),
            _row("大豆油", basis=20, factory="工厂C"),
            _row("大豆油", contract="2701", basis=999, factory="远月工厂"),
            _row("菜籽油", basis=20, factory="工厂B"),
            _row("大豆油", delivery="9月", basis=100, factory="九月工厂"),
            _row("豆粕", basis=None, factory="空基差"),
            _row("豆粕", basis=777, quote_type="远月基差", factory="远月报价"),
            _row("菜粕", basis=888, quote_type="一口价", factory="一口价"),
        ]
    )
    return rows


def test_streams_filters_maps_selects_near_contract_and_medians(tmp_path: Path) -> None:
    source = tmp_path / "basis.sql"
    output = tmp_path / "basis.parquet"
    _write_sql(source, _representative_rows())

    result = build_basis_candidate(source, output)
    data = pd.read_parquet(output)

    assert result["raw_rows"] == 13
    assert result["spot_basis_rows"] == 11
    assert result["null_basis_rows"] == 1
    assert result["far_contract_excluded_rows"] == 1
    assert result["missing_contract_rows"] == 0
    assert result["invalid_contract_rows"] == 0
    assert result["expired_contract_rows"] == 0
    assert result["source_sha256_before"] == result["source_sha256_after"]
    assert data.columns.tolist() == DATABASE_COLUMNS
    assert set(data["commodity"]) == {"一豆", "三菜", "24度", "豆粕", "菜粕"}
    assert set(data["quote_type"]) == {"基差报价"}
    soy_spot = data[data["commodity"].eq("一豆")].iloc[0]
    assert soy_spot["delivery_month"] == "现货"
    assert soy_spot["futures_contract"] == "2609"
    assert soy_spot["basis"] == 15
    rapeseed = data[data["commodity"].eq("三菜")].iloc[0]
    assert rapeseed["basis"] == 15
    assert data["cash_price"].isna().all()
    assert data["futures_price"].isna().all()
    assert data["source_sheet"].str.startswith("basis_price:").all()
    assert set(data["delivery_month"]) == {"现货"}
    assert result["raw_delivery_month_counts"] == {"8月": 9, "9月": 1}
    assert len(result["merged_delivery_month_groups"]) == 1
    assert not data.duplicated(STABLE_KEY).any()
    assert (
        data.groupby(PAGE_KEY, dropna=False)["futures_contract"].nunique() <= 1
    ).all()
    assert pd.Timestamp(data["date"].max()) == pd.Timestamp("2026-08-04")
    # The loader owns this schema contract.  A production/runtime parquet is
    # deliberately not tracked in Git and must not be a required-test oracle.
    assert pq.read_schema(output).field("date").type == pa.timestamp("us")


def test_original_delivery_months_merge_before_near_contract_selection(
    tmp_path: Path,
) -> None:
    source = tmp_path / "basis.sql"
    output = tmp_path / "basis.parquet"
    rows = [_row(product) for product in PRODUCTS]
    rows.extend(
        [
            _row("豆粕", date="2026-05-29", region="华南", delivery="5月", contract="2607", basis=10),
            _row("豆粕", date="2026-05-29", region="华南", delivery="5月", contract="2609", basis=500, factory="远月"),
            _row("豆粕", date="2026-05-29", region="华南", delivery="6月", contract="2607", basis=40, factory="六月"),
        ]
    )
    _write_sql(source, rows)
    result = build_basis_candidate(source, output)
    data = pd.read_parquet(output)
    spot = data[
        data["date"].eq(pd.Timestamp("2026-05-29"))
        & data["commodity"].eq("豆粕")
    ].iloc[0]
    assert (spot["delivery_month"], spot["futures_contract"], spot["basis"]) == (
        "现货",
        "2607",
        25,
    )
    assert result["far_contract_excluded_rows"] == 1
    merged = [
        item
        for item in result["merged_delivery_month_groups"]
        if item["date"] == "2026-05-29" and item["commodity"] == "豆粕"
    ]
    assert merged == [
        {
            "date": "2026-05-29",
            "commodity": "豆粕",
            "region": "华南",
            "quote_type": "基差报价",
            "raw_delivery_months": {"5月": 2, "6月": 1},
            "target_contract": "2607",
        }
    ]


@pytest.mark.parametrize("contract", [None, "bad", "2607"])
def test_group_without_valid_contract_trips_quality_gate(
    tmp_path: Path,
    contract: str | None,
) -> None:
    source = tmp_path / "basis.sql"
    output = tmp_path / "basis.parquet"
    rows = [_row(product) for product in PRODUCTS]
    rows.append(
        _row(
            "豆粕",
            date="2026-08-04",
            region="华南",
            delivery="9月",
            contract=contract,
        )
    )
    _write_sql(source, rows)
    with pytest.raises(ValueError, match="无有效未到期合约"):
        build_basis_candidate(source, output)
    assert not output.exists()


def test_insert_field_count_must_match_create_table(tmp_path: Path) -> None:
    source = tmp_path / "basis.sql"
    output = tmp_path / "basis.parquet"
    rows = [_row(product) for product in PRODUCTS]
    rows[0] = rows[0][:-1]
    _write_sql(source, rows)
    with pytest.raises(ValueError, match="字段数"):
        build_basis_candidate(source, output)
    assert not output.exists()


def test_business_content_is_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "basis.sql"
    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"
    _write_sql(source, _representative_rows())
    build_basis_candidate(source, first)
    build_basis_candidate(source, second)
    pd.testing.assert_frame_equal(pd.read_parquet(first), pd.read_parquet(second))
    assert first.read_bytes() == second.read_bytes()
