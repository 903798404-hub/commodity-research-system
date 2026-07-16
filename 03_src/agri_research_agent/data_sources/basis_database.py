from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from agri_research_agent.core.paths import (
    BASIS_DATABASE_FILE,
    CONFIG_DIR,
    PROJECT_ROOT,
)


DATABASE_COLUMNS = [
    "date",
    "commodity",
    "region",
    "quote_type",
    "delivery_month",
    "futures_contract",
    "cash_price",
    "futures_price",
    "basis",
    "source_sheet",
]

COLUMN_ALIASES = {
    "日期": "date",
    "date": "date",
    "品种": "commodity",
    "commodity": "commodity",
    "区域": "region",
    "地区": "region",
    "region": "region",
    "报价类型": "quote_type",
    "quote_type": "quote_type",
    "提货月": "delivery_month",
    "delivery_month": "delivery_month",
    "对应合约月": "futures_contract",
    "期货合约": "futures_contract",
    "contract_month": "futures_contract",
    "futures_contract": "futures_contract",
    "一口价（元/吨）": "cash_price",
    "现货价": "cash_price",
    "cash_price": "cash_price",
    "盘面价": "futures_price",
    "期货价": "futures_price",
    "futures_price": "futures_price",
    "基差": "basis",
    "basis": "basis",
}

TEXT_REQUIRED_COLUMNS = [
    "date",
    "commodity",
    "region",
    "quote_type",
    "delivery_month",
]


def _load_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file)
    if not isinstance(config, dict):
        raise ValueError(f"基差配置必须是 YAML 对象：{config_path}")
    return config


def _resolve_source_path(
    config: dict[str, Any],
    source_path: Path | str | None = None,
) -> Path:
    if source_path is None:
        source_file = config.get("source_file", {})
        source_path = Path(str(source_file.get("path", "")))
    else:
        source_path = Path(source_path)
    if not source_path.is_absolute():
        source_path = PROJECT_ROOT / source_path
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"国内现货基差 Excel 不存在：{source_path}")
    return source_path


def _clean_text(series: pd.Series) -> pd.Series:
    return series.astype("string").str.strip().replace(
        {"": pd.NA, "#N/A": pd.NA, "N/A": pd.NA}
    )


def _format_contract(value: object) -> str | pd.NA:
    if pd.isna(value):
        return pd.NA
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):02d}月"
    if isinstance(value, int):
        return f"{value:02d}月"
    text = str(value).strip()
    if not text or text in {"#N/A", "N/A"}:
        return pd.NA
    return text


def _standardize_sheet(
    dataframe: pd.DataFrame,
    *,
    commodity: str,
    quote_type: str,
    source_sheet: str,
) -> tuple[pd.DataFrame, dict[str, int]]:
    cleaned = dataframe.rename(
        columns={
            column: COLUMN_ALIASES.get(str(column).strip(), str(column).strip())
            for column in dataframe.columns
        }
    ).copy()
    cleaned = cleaned.loc[:, ~cleaned.columns.duplicated()].copy()

    for column in DATABASE_COLUMNS:
        if column not in cleaned.columns:
            cleaned[column] = pd.NA

    cleaned["commodity"] = commodity
    cleaned["quote_type"] = quote_type
    cleaned["source_sheet"] = source_sheet
    for column in ("commodity", "region", "quote_type", "delivery_month"):
        cleaned[column] = _clean_text(cleaned[column])
    cleaned["futures_contract"] = cleaned["futures_contract"].map(
        _format_contract
    ).astype("string")

    original_date = cleaned["date"].copy()
    cleaned["date"] = pd.to_datetime(cleaned["date"], errors="coerce")
    invalid_date_count = int(
        (original_date.notna() & cleaned["date"].isna()).sum()
    )

    invalid_numeric_counts: dict[str, int] = {}
    for column in ("cash_price", "futures_price", "basis"):
        original = cleaned[column].copy()
        cleaned[column] = pd.to_numeric(cleaned[column], errors="coerce")
        invalid_numeric_counts[column] = int(
            (original.notna() & cleaned[column].isna()).sum()
        )

    if quote_type == "基差报价":
        missing_basis = cleaned["basis"].isna()
        calculable = (
            missing_basis
            & cleaned["cash_price"].notna()
            & cleaned["futures_price"].notna()
        )
        cleaned.loc[calculable, "basis"] = (
            cleaned.loc[calculable, "cash_price"]
            - cleaned.loc[calculable, "futures_price"]
        )
        comparable = cleaned.dropna(
            subset=["cash_price", "futures_price", "basis"]
        )
        mismatch = (
            comparable["cash_price"]
            - comparable["futures_price"]
            - comparable["basis"]
        ).abs()
        mismatch_count = int((mismatch > 1).sum())
        if mismatch_count:
            raise ValueError(
                f"工作表“{source_sheet}”有 {mismatch_count} 行基差不满足"
                "“现货价 - 期货价”，请先修正原始 Excel"
            )
        required_columns = [
            *TEXT_REQUIRED_COLUMNS,
            "futures_contract",
            "cash_price",
            "futures_price",
            "basis",
        ]
    else:
        calculable = pd.Series(False, index=cleaned.index)
        required_columns = [*TEXT_REQUIRED_COLUMNS, "cash_price"]

    missing_required = cleaned[required_columns].isna().any(axis=1)
    dropped_missing_count = int(missing_required.sum())
    cleaned = cleaned.loc[~missing_required, DATABASE_COLUMNS].copy()

    duplicate_columns = [
        "date",
        "commodity",
        "region",
        "quote_type",
        "delivery_month",
        "futures_contract",
    ]
    duplicate_mask = cleaned.duplicated(duplicate_columns, keep="last")
    duplicate_count = int(duplicate_mask.sum())
    cleaned = cleaned.loc[~duplicate_mask].copy()

    return cleaned, {
        "source_rows": len(dataframe),
        "output_rows": len(cleaned),
        "invalid_dates": invalid_date_count,
        "invalid_cash_prices": invalid_numeric_counts["cash_price"],
        "invalid_futures_prices": invalid_numeric_counts["futures_price"],
        "invalid_basis_values": invalid_numeric_counts["basis"],
        "calculated_basis_rows": int(calculable.sum()),
        "dropped_missing_rows": dropped_missing_count,
        "duplicate_rows": duplicate_count,
    }


def build_basis_database(
    config_path: Path | str | None = None,
    output_path: Path | str | None = None,
    source_path: Path | str | None = None,
) -> dict[str, Any]:
    resolved_config = (
        Path(config_path)
        if config_path is not None
        else CONFIG_DIR / "basis_excel.yaml"
    )
    resolved_output = (
        Path(output_path) if output_path is not None else BASIS_DATABASE_FILE
    )
    config = _load_config(resolved_config)
    resolved_source = _resolve_source_path(config, source_path)

    frames: list[pd.DataFrame] = []
    sheet_summaries: list[dict[str, Any]] = []
    with pd.ExcelFile(resolved_source, engine="openpyxl") as workbook:
        for section_name, quote_type in (
            ("basis_sheets", "基差报价"),
            ("cash_price_sheets", "一口价"),
        ):
            for sheet_name, sheet_config in config.get(
                section_name, {}
            ).items():
                source = pd.read_excel(workbook, sheet_name=sheet_name)
                standardized, summary = _standardize_sheet(
                    source,
                    commodity=str(sheet_config["commodity"]),
                    quote_type=quote_type,
                    source_sheet=sheet_name,
                )
                frames.append(standardized)
                sheet_summaries.append(
                    {"source_sheet": sheet_name, **summary}
                )

    if not frames:
        raise ValueError("配置中没有可导入的基差或一口价工作表")

    database = pd.concat(frames, ignore_index=True)
    database = database.sort_values(
        ["date", "commodity", "region", "quote_type", "delivery_month"]
    ).reset_index(drop=True)
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = resolved_output.with_suffix(".parquet.tmp")
    try:
        database.to_parquet(temporary_path, index=False)
        os.replace(temporary_path, resolved_output)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    return {
        "source_file": resolved_source,
        "output_file": resolved_output,
        "rows": len(database),
        "basis_rows": int(database["basis"].notna().sum()),
        "cash_price_rows": int(database["cash_price"].notna().sum()),
        "earliest_date": database["date"].min(),
        "latest_date": database["date"].max(),
        "sheet_summaries": sheet_summaries,
    }
