from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from agri_research_agent.data_sources.basis_database import (
    DATABASE_COLUMNS,
    build_basis_database,
)


def _write_config(path: Path, source_file: Path) -> Path:
    config = {
        "source_file": {"path": str(source_file)},
        "basis_sheets": {
            "豆粕基差": {"commodity": "豆粕", "type": "basis"}
        },
        "cash_price_sheets": {
            "一葵价格": {"commodity": "一葵", "type": "cash_price"}
        },
    }
    config_path = path / "basis_excel.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return config_path


def _write_workbook(
    path: Path,
    *,
    include_basis: bool = False,
    mismatched_basis: bool = False,
) -> None:
    basis_data: dict[str, list[object]] = {
        "日期": ["2026-06-01", "错误日期", "2026-06-01"],
        "区域": ["华东", "华南", "华东"],
        "提货月": ["现货", "现货", "现货"],
        "对应合约月": [9, 9, 9],
        "盘面价": [3000, 3010, 3000],
        "一口价（元/吨）": [3100, "非数字", 3100],
    }
    if include_basis:
        basis_data["基差"] = [999 if mismatched_basis else 100, 10, 100]

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(basis_data).to_excel(
            writer, sheet_name="豆粕基差", index=False
        )
        pd.DataFrame(
            {
                "日期": ["2026-06-01"],
                "区域": ["华东"],
                "提货月": ["现货"],
                "一口价（元/吨）": [9000],
            }
        ).to_excel(writer, sheet_name="一葵价格", index=False)


def test_builds_database_and_calculates_missing_basis(tmp_path: Path) -> None:
    source = tmp_path / "国内现货基差.xlsx"
    _write_workbook(source)
    config = _write_config(tmp_path, source)
    output = tmp_path / "basis_quotes.parquet"

    result = build_basis_database(config, output)
    data = pd.read_parquet(output)

    assert result["rows"] == 2
    assert data.columns.tolist() == DATABASE_COLUMNS
    assert data["quote_type"].tolist() == ["一口价", "基差报价"]
    basis_row = data[data["quote_type"] == "基差报价"].iloc[0]
    assert basis_row["basis"] == 100
    assert basis_row["futures_contract"] == "09月"
    assert result["sheet_summaries"][0]["invalid_dates"] == 1
    assert result["sheet_summaries"][0]["duplicate_rows"] == 1


def test_rejects_mismatched_existing_basis(tmp_path: Path) -> None:
    source = tmp_path / "国内现货基差.xlsx"
    _write_workbook(source, include_basis=True, mismatched_basis=True)
    config = _write_config(tmp_path, source)

    with pytest.raises(ValueError, match="现货价 - 期货价"):
        build_basis_database(config, tmp_path / "basis_quotes.parquet")
