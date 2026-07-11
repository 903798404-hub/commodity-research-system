from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest
import yaml

from agri_research_agent.data_sources.basis_upload import update_basis_from_upload


def _workbook_bytes(cash_price: float, futures_price: float) -> bytes:
    frame = pd.DataFrame(
        {
            "日期": ["2026-06-20"],
            "区域": ["华东"],
            "提货月": ["现货"],
            "对应合约月": ["09月"],
            "盘面价": [futures_price],
            "一口价（元/吨）": [cash_price],
            "基差": [cash_price - futures_price],
        }
    )
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="豆粕基差", index=False)
    return buffer.getvalue()


def _write_config(path: Path, source_path: Path) -> None:
    config = {
        "source_file": {
            "path": str(source_path),
        },
        "basis_sheets": {
            "豆粕基差": {
                "commodity": "豆粕",
                "header_row": 0,
                "date_column": "日期",
                "region_column": "区域",
                "delivery_month_column": "提货月",
                "contract_month_column": "对应合约月",
                "futures_price_column": "盘面价",
                "cash_price_column": "一口价（元/吨）",
                "basis_column": "基差",
                "quote_type": "基差",
            }
        },
    }
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")


def test_upload_replaces_excel_backs_up_and_builds_parquet(tmp_path: Path) -> None:
    source_path = tmp_path / "国内现货基差.xlsx"
    backup_dir = tmp_path / "backup"
    config_path = tmp_path / "basis_excel.yaml"
    output_path = tmp_path / "basis_quotes.parquet"
    old_bytes = _workbook_bytes(3000, 2900)
    new_bytes = _workbook_bytes(3100, 2950)
    source_path.write_bytes(old_bytes)
    _write_config(config_path, source_path)

    result = update_basis_from_upload(
        new_bytes,
        "每日更新.xlsx",
        source_path=source_path,
        backup_dir=backup_dir,
        config_path=config_path,
        output_path=output_path,
        now=datetime(2026, 6, 20, 9, 30, 15),
    )

    backup_path = backup_dir / "国内现货基差_20260620_093015.xlsx"
    assert source_path.read_bytes() == new_bytes
    assert backup_path.read_bytes() == old_bytes
    assert output_path.exists()
    assert result["rows"] == 1
    assert result["latest_date"].strftime("%Y-%m-%d") == "2026-06-20"
    assert result["backup_file"] == backup_path

    parquet = pd.read_parquet(output_path)
    assert parquet.loc[0, "cash_price"] == 3100
    assert parquet.loc[0, "futures_price"] == 2950
    assert parquet.loc[0, "basis"] == 150


def test_invalid_xlsx_does_not_replace_original(tmp_path: Path) -> None:
    source_path = tmp_path / "国内现货基差.xlsx"
    original = _workbook_bytes(3000, 2900)
    source_path.write_bytes(original)

    with pytest.raises(ValueError, match="无法读取"):
        update_basis_from_upload(
            b"not an xlsx file",
            "每日更新.xlsx",
            source_path=source_path,
            backup_dir=tmp_path / "backup",
            config_path=tmp_path / "missing.yaml",
            output_path=tmp_path / "basis_quotes.parquet",
        )

    assert source_path.read_bytes() == original
    assert not list((tmp_path / "backup").glob("*.xlsx"))


def test_import_failure_restores_original_excel(tmp_path: Path) -> None:
    source_path = tmp_path / "国内现货基差.xlsx"
    backup_dir = tmp_path / "backup"
    config_path = tmp_path / "basis_excel.yaml"
    original = _workbook_bytes(3000, 2900)
    source_path.write_bytes(original)
    _write_config(config_path, source_path)

    broken_frame = pd.DataFrame({"日期": ["2026-06-20"]})
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        broken_frame.to_excel(writer, sheet_name="错误工作表", index=False)

    with pytest.raises(Exception):
        update_basis_from_upload(
            buffer.getvalue(),
            "每日更新.xlsx",
            source_path=source_path,
            backup_dir=backup_dir,
            config_path=config_path,
            output_path=tmp_path / "basis_quotes.parquet",
            now=datetime(2026, 6, 20, 10, 0, 0),
        )

    assert source_path.read_bytes() == original
    assert (backup_dir / "国内现货基差_20260620_100000.xlsx").exists()
