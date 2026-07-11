from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "03_src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agri_research_agent.data_sources.basis_excel_loader import (  # noqa: E402
    BASIS_OUTPUT_COLUMNS,
    CASH_PRICE_OUTPUT_COLUMNS,
    SPOT_SPREAD_OUTPUT_COLUMNS,
    import_basis_excel,
)


BASIS_COLUMNS = [
    "日期",
    "区域",
    "提货月",
    "对应合约月",
    "基差",
    "盘面价",
    "一口价（元/吨）",
    "对应盘面价列数",
]
CASH_COLUMNS = ["日期", "区域", "提货月", "一口价（元/吨）"]


def _write_config(path: Path, source_file: Path) -> Path:
    config = {
        "source_file": {
            "path": str(source_file),
            "description": "测试文件",
        },
        "basis_sheets": {
            "豆粕基差": {
                "commodity": "豆粕",
                "type": "basis",
                "use_columns": BASIS_COLUMNS,
            }
        },
        "cash_price_sheets": {
            "一葵价格": {
                "commodity": "一葵",
                "type": "cash_price",
                "use_columns": CASH_COLUMNS,
            }
        },
        "futures_sheet": {
            "sheet_name": "内盘期货收盘价",
            "date_column": "Date",
        },
        "spot_spread_sheet": {
            "sheet_name": "历史现货价差",
            "type": "spot_spread",
            "description": "测试价差数据",
        },
        "excluded_sheets": ["国内现货价格分析"],
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
    missing_basis_column: bool = False,
    missing_spread_date: bool = False,
    empty_spread_values: bool = False,
) -> None:
    basis_data = {
        "日期": ["2026-06-01", "2026-06-02", None],
        "区域": ["华东", "华南", "华东"],
        "提货月": ["现货", "7月", "现货"],
        "对应合约月": [9, 9, 9],
        "基差": [100, "#N/A", 10],
        "盘面价": [3000, 3010, 3000],
        "一口价（元/吨）": [3102, 3020, 3010],
        "对应盘面价列数": [4, 4, 4],
    }
    if missing_basis_column:
        basis_data.pop("区域")

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(basis_data).to_excel(
            writer,
            sheet_name="豆粕基差",
            index=False,
        )
        pd.DataFrame(
            {
                "日期": ["2026-06-01", "2026-06-02", "2026-06-03"],
                "区域": ["华东", "", "华南"],
                "提货月": ["现货", "现货", "现货"],
                "一口价（元/吨）": [9000, 9100, "N/A"],
            }
        ).to_excel(writer, sheet_name="一葵价格", index=False)
        pd.DataFrame(
            {
                "Date": ["2026-06-01", None],
                "DCE_豆粕_09月": [3000, 3010],
            }
        ).to_excel(writer, sheet_name="内盘期货收盘价", index=False)
        spread_date_name = "交易日" if missing_spread_date else "日期"
        spread_values = (
            [[None, None], ["#N/A", "N/A"]]
            if empty_spread_values
            else [[500, 120], [510, None]]
        )
        pd.DataFrame(
            [
                ["现货", "华东", None],
                [spread_date_name, "豆粕-菜粕", "一豆-24度"],
                ["2026-06-01", *spread_values[0]],
                ["2026-06-02", *spread_values[1]],
            ]
        ).to_excel(
            writer,
            sheet_name="历史现货价差",
            index=False,
            header=False,
        )
        pd.DataFrame({"任意字段": [1]}).to_excel(
            writer,
            sheet_name="国内现货价格分析",
            index=False,
        )


def test_imports_chinese_filename_and_standardizes_outputs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source_file = tmp_path / "国内现货基差.xlsx"
    _write_workbook(source_file)
    config_path = _write_config(tmp_path, source_file)
    output_dir = tmp_path / "processed"

    with caplog.at_level(logging.WARNING):
        result = import_basis_excel(config_path, output_dir)

    basis_long = pd.read_csv(result["basis_long"], encoding="utf-8-sig")
    cash_price_long = pd.read_csv(
        result["cash_price_long"],
        encoding="utf-8-sig",
    )
    futures_close = pd.read_csv(
        result["futures_close"],
        encoding="utf-8-sig",
    )
    spot_spread = pd.read_csv(
        result["spot_spread"],
        encoding="utf-8-sig",
    )

    assert source_file.name == "国内现货基差.xlsx"
    assert result["basis_long"].name == "国内现货基差_标准表.csv"
    assert result["cash_price_long"].name == "国内现货一口价_标准表.csv"
    assert result["futures_close"].name == "内盘期货收盘价.csv"
    assert result["spot_spread"].name == "国内现货价差_标准表.csv"
    for legacy_name in (
        "basis_long.csv",
        "cash_price_long.csv",
        "futures_close.csv",
        "spot_spread_long.csv",
    ):
        assert not (output_dir / legacy_name).exists()
    assert basis_long.columns.tolist() == BASIS_OUTPUT_COLUMNS
    assert basis_long["date"].tolist() == ["2026-06-01", "2026-06-02"]
    assert basis_long["commodity"].tolist() == ["豆粕", "豆粕"]
    assert basis_long["source_sheet"].unique().tolist() == ["豆粕基差"]
    assert pd.isna(basis_long.loc[1, "basis"])

    assert cash_price_long.columns.tolist() == CASH_PRICE_OUTPUT_COLUMNS
    assert len(cash_price_long) == 1
    assert cash_price_long.loc[0, "commodity"] == "一葵"
    assert cash_price_long.loc[0, "cash_price"] == 9000

    assert futures_close.columns.tolist() == ["date", "DCE_豆粕_09月"]
    assert futures_close["date"].tolist() == ["2026-06-01"]

    assert spot_spread.columns.tolist() == SPOT_SPREAD_OUTPUT_COLUMNS
    assert spot_spread["date"].tolist() == [
        "2026-06-01",
        "2026-06-02",
        "2026-06-01",
    ]
    assert spot_spread["spread_name"].tolist() == [
        "华东_豆粕-菜粕",
        "华东_豆粕-菜粕",
        "华东_一豆-24度",
    ]
    assert spot_spread["spread_value"].tolist() == [500, 510, 120]
    assert spot_spread["source_sheet"].unique().tolist() == ["历史现货价差"]
    assert result["spot_spread_summary"]["spread_indicator_count"] == 2
    assert result["basis_mismatch_warnings"] == 1
    assert "绝对误差大于 1" in caplog.text


def test_missing_excel_raises_file_not_found(tmp_path: Path) -> None:
    missing_source = tmp_path / "国内现货基差.xlsx"
    config_path = _write_config(tmp_path, missing_source)

    with pytest.raises(FileNotFoundError, match="国内现货基差 Excel 不存在"):
        import_basis_excel(config_path, tmp_path / "processed")


def test_missing_config_field_raises_clear_error(tmp_path: Path) -> None:
    config_path = tmp_path / "basis_excel.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "source_file": {"description": "缺少 path"},
                "basis_sheets": {},
                "cash_price_sheets": {},
                "futures_sheet": {},
                "spot_spread_sheet": {},
                "excluded_sheets": [],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"source_file\.path"):
        import_basis_excel(config_path, tmp_path / "processed")


def test_missing_excel_column_raises_clear_error(tmp_path: Path) -> None:
    source_file = tmp_path / "国内现货基差.xlsx"
    _write_workbook(source_file, missing_basis_column=True)
    config_path = _write_config(tmp_path, source_file)

    with pytest.raises(ValueError, match="豆粕基差.*区域"):
        import_basis_excel(config_path, tmp_path / "processed")


def test_missing_configured_sheet_raises_clear_error(tmp_path: Path) -> None:
    source_file = tmp_path / "国内现货基差.xlsx"
    _write_workbook(source_file)
    config_path = _write_config(tmp_path, source_file)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["basis_sheets"]["不存在的基差表"] = config["basis_sheets"].pop(
        "豆粕基差"
    )
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="不存在的基差表"):
        import_basis_excel(config_path, tmp_path / "processed")


def test_spot_spread_missing_date_column_raises_clear_error(
    tmp_path: Path,
) -> None:
    source_file = tmp_path / "国内现货基差.xlsx"
    _write_workbook(source_file, missing_spread_date=True)
    config_path = _write_config(tmp_path, source_file)

    with pytest.raises(ValueError, match="无法识别日期列"):
        import_basis_excel(config_path, tmp_path / "processed")


def test_spot_spread_all_empty_raises_clear_error(tmp_path: Path) -> None:
    source_file = tmp_path / "国内现货基差.xlsx"
    _write_workbook(source_file, empty_spread_values=True)
    config_path = _write_config(tmp_path, source_file)

    with pytest.raises(ValueError, match="全部价差数据为空"):
        import_basis_excel(config_path, tmp_path / "processed")
