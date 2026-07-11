from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from agri_research_agent.core.paths import (
    BASIS_PROCESSED_DIR,
    CONFIG_DIR,
    PROJECT_ROOT,
)


LOGGER = logging.getLogger(__name__)

BASIS_OUTPUT_COLUMNS = [
    "date",
    "commodity",
    "region",
    "delivery_month",
    "contract_month",
    "basis",
    "futures_price",
    "cash_price",
    "futures_column_index",
    "source_sheet",
]

CASH_PRICE_OUTPUT_COLUMNS = [
    "date",
    "commodity",
    "region",
    "delivery_month",
    "cash_price",
    "source_sheet",
]

SPOT_SPREAD_OUTPUT_COLUMNS = [
    "date",
    "spread_name",
    "spread_value",
    "source_sheet",
]

BASIS_COLUMN_MAP = {
    "日期": "date",
    "区域": "region",
    "提货月": "delivery_month",
    "对应合约月": "contract_month",
    "基差": "basis",
    "盘面价": "futures_price",
    "一口价（元/吨）": "cash_price",
    "对应盘面价列数": "futures_column_index",
}

CASH_PRICE_COLUMN_MAP = {
    "日期": "date",
    "区域": "region",
    "提货月": "delivery_month",
    "一口价（元/吨）": "cash_price",
}

MISSING_VALUE_TOKENS = ["#N/A", "N/A", ""]


def _load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        raise FileNotFoundError(f"基差 Excel 配置文件不存在：{config_path}")

    with config_path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file)

    if not isinstance(config, dict):
        raise ValueError(f"基差 Excel 配置必须是 YAML 对象：{config_path}")

    required_sections = {
        "source_file",
        "basis_sheets",
        "cash_price_sheets",
        "futures_sheet",
        "spot_spread_sheet",
        "excluded_sheets",
    }
    missing_sections = sorted(required_sections - config.keys())
    if missing_sections:
        raise ValueError(f"基差 Excel 配置缺少字段：{', '.join(missing_sections)}")

    source_file = config["source_file"]
    if not isinstance(source_file, dict) or not source_file.get("path"):
        raise ValueError("基差 Excel 配置缺少字段：source_file.path")

    for section_name in ("basis_sheets", "cash_price_sheets"):
        section = config[section_name]
        if not isinstance(section, dict) or not section:
            raise ValueError(f"基差 Excel 配置字段必须是非空对象：{section_name}")
        for sheet_name, sheet_config in section.items():
            if not isinstance(sheet_config, dict):
                raise ValueError(f"工作表配置必须是对象：{section_name}.{sheet_name}")
            missing = [
                field
                for field in ("commodity", "type", "use_columns")
                if not sheet_config.get(field)
            ]
            if missing:
                raise ValueError(
                    f"工作表配置 {section_name}.{sheet_name} 缺少字段："
                    f"{', '.join(missing)}"
                )
            if not isinstance(sheet_config["use_columns"], list):
                raise ValueError(
                    f"工作表配置 {section_name}.{sheet_name}.use_columns 必须是列表"
                )

    futures_config = config["futures_sheet"]
    if not isinstance(futures_config, dict):
        raise ValueError("基差 Excel 配置字段必须是对象：futures_sheet")
    for field in ("sheet_name", "date_column"):
        if not futures_config.get(field):
            raise ValueError(f"基差 Excel 配置缺少字段：futures_sheet.{field}")

    spot_spread_config = config["spot_spread_sheet"]
    if not isinstance(spot_spread_config, dict):
        raise ValueError("基差 Excel 配置字段必须是对象：spot_spread_sheet")
    for field in ("sheet_name", "type", "description"):
        if not spot_spread_config.get(field):
            raise ValueError(f"基差 Excel 配置缺少字段：spot_spread_sheet.{field}")

    if not isinstance(config["excluded_sheets"], list):
        raise ValueError("基差 Excel 配置字段必须是列表：excluded_sheets")

    return config


def _resolve_source_path(config: dict[str, Any]) -> Path:
    source_path = Path(config["source_file"]["path"])
    if not source_path.is_absolute():
        source_path = PROJECT_ROOT / source_path
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(
            "手动维护的国内现货基差 Excel 不存在："
            f"{source_path}。请将文件保持原名“国内现货基差.xlsx”放在配置路径中。"
        )
    return source_path


def _validate_workbook(
    source_path: Path,
    config: dict[str, Any],
) -> None:
    with pd.ExcelFile(source_path, engine="openpyxl") as workbook:
        available_sheets = set(workbook.sheet_names)
        required_sheets = set(config["basis_sheets"])
        required_sheets.update(config["cash_price_sheets"])
        required_sheets.add(config["futures_sheet"]["sheet_name"])
        required_sheets.add(config["spot_spread_sheet"]["sheet_name"])

        missing_sheets = sorted(required_sheets - available_sheets)
        if missing_sheets:
            raise ValueError(
                f"Excel 文件 {source_path.name} 缺少配置中的工作表："
                f"{', '.join(missing_sheets)}"
            )

        for section_name in ("basis_sheets", "cash_price_sheets"):
            for sheet_name, sheet_config in config[section_name].items():
                header = pd.read_excel(
                    workbook,
                    sheet_name=sheet_name,
                    nrows=0,
                )
                missing_columns = [
                    column
                    for column in sheet_config["use_columns"]
                    if column not in header.columns
                ]
                if missing_columns:
                    raise ValueError(
                        f"工作表“{sheet_name}”缺少配置字段："
                        f"{', '.join(missing_columns)}"
                    )

        futures_sheet = config["futures_sheet"]["sheet_name"]
        futures_date_column = config["futures_sheet"]["date_column"]
        futures_header = pd.read_excel(
            workbook,
            sheet_name=futures_sheet,
            nrows=0,
        )
        if futures_date_column not in futures_header.columns:
            raise ValueError(
                f"工作表“{futures_sheet}”缺少配置字段：{futures_date_column}"
            )


def _normalize_missing_values(dataframe: pd.DataFrame) -> pd.DataFrame:
    cleaned = dataframe.copy()
    missing_tokens = set(MISSING_VALUE_TOKENS)
    object_columns = cleaned.select_dtypes(include=["object"]).columns
    for column in object_columns:
        cleaned[column] = cleaned[column].map(
            lambda value: (
                pd.NA
                if isinstance(value, str) and value.strip() in missing_tokens
                else value.strip()
                if isinstance(value, str)
                else value
            )
        )
    return cleaned


def _standardize_basis_sheet(
    dataframe: pd.DataFrame,
    commodity: str,
    source_sheet: str,
) -> pd.DataFrame:
    cleaned = _normalize_missing_values(dataframe).rename(columns=BASIS_COLUMN_MAP)
    cleaned["date"] = pd.to_datetime(cleaned["date"], errors="coerce")
    cleaned = cleaned.dropna(subset=["date", "region", "cash_price"]).copy()

    for column in (
        "basis",
        "futures_price",
        "cash_price",
        "futures_column_index",
    ):
        cleaned[column] = pd.to_numeric(cleaned[column], errors="coerce")

    cleaned.insert(1, "commodity", commodity)
    cleaned["source_sheet"] = source_sheet
    return cleaned[BASIS_OUTPUT_COLUMNS]


def _standardize_cash_price_sheet(
    dataframe: pd.DataFrame,
    commodity: str,
    source_sheet: str,
) -> pd.DataFrame:
    cleaned = _normalize_missing_values(dataframe).rename(
        columns=CASH_PRICE_COLUMN_MAP
    )
    cleaned["date"] = pd.to_datetime(cleaned["date"], errors="coerce")
    cleaned = cleaned.dropna(subset=["date", "region", "cash_price"]).copy()
    cleaned["cash_price"] = pd.to_numeric(cleaned["cash_price"], errors="coerce")

    cleaned.insert(1, "commodity", commodity)
    cleaned["source_sheet"] = source_sheet
    return cleaned[CASH_PRICE_OUTPUT_COLUMNS]


def _standardize_futures_sheet(
    dataframe: pd.DataFrame,
    date_column: str,
) -> pd.DataFrame:
    cleaned = _normalize_missing_values(dataframe).rename(
        columns={date_column: "date"}
    )
    cleaned["date"] = pd.to_datetime(cleaned["date"], errors="coerce")
    return cleaned.dropna(subset=["date"]).copy()


def _find_spot_spread_date_cell(
    raw_dataframe: pd.DataFrame,
) -> tuple[int, int]:
    date_names = {"日期", "Date", "date"}
    scan_rows = min(20, len(raw_dataframe))
    for row_index in range(scan_rows):
        for column_index, value in enumerate(raw_dataframe.iloc[row_index]):
            if isinstance(value, str) and value.strip() in date_names:
                return row_index, column_index
    raise ValueError(
        "工作表“历史现货价差”无法识别日期列，"
        "日期列名称必须为：日期、Date 或 date"
    )


def _make_unique_names(names: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    unique_names: list[str] = []
    for name in names:
        counts[name] = counts.get(name, 0) + 1
        suffix = f"_{counts[name]}" if counts[name] > 1 else ""
        unique_names.append(f"{name}{suffix}")
    return unique_names


def _standardize_spot_spread_sheet(
    raw_dataframe: pd.DataFrame,
    source_sheet: str,
) -> tuple[pd.DataFrame, int]:
    cleaned_raw = _normalize_missing_values(raw_dataframe)
    header_row, date_column_index = _find_spot_spread_date_cell(cleaned_raw)
    header_rows = cleaned_raw.iloc[: header_row + 1].copy()
    header_rows = header_rows.ffill(axis=1)

    value_column_indexes = [
        index
        for index in range(cleaned_raw.shape[1])
        if index != date_column_index
    ]
    spread_names: list[str] = []
    retained_indexes: list[int] = []
    for column_index in value_column_indexes:
        parts: list[str] = []
        for value in header_rows.iloc[:, column_index].tolist():
            if pd.isna(value):
                continue
            text = str(value).strip()
            if text and text not in parts:
                parts.append(text)
        spread_name = "_".join(parts)
        if spread_name:
            spread_names.append(spread_name)
            retained_indexes.append(column_index)

    spread_names = _make_unique_names(spread_names)
    data_rows = cleaned_raw.iloc[header_row + 1 :].copy()
    date_values = pd.to_datetime(
        data_rows.iloc[:, date_column_index],
        errors="coerce",
    )

    wide = data_rows.iloc[:, retained_indexes].copy()
    wide.columns = spread_names
    wide.insert(0, "date", date_values)
    wide = wide.dropna(subset=["date"])

    long_dataframe = wide.melt(
        id_vars=["date"],
        var_name="spread_name",
        value_name="spread_value",
    )
    long_dataframe = _normalize_missing_values(long_dataframe)
    long_dataframe["spread_value"] = pd.to_numeric(
        long_dataframe["spread_value"],
        errors="coerce",
    )
    long_dataframe = long_dataframe.dropna(
        subset=["date", "spread_name", "spread_value"]
    ).copy()
    long_dataframe["source_sheet"] = source_sheet
    long_dataframe = long_dataframe[SPOT_SPREAD_OUTPUT_COLUMNS]

    if long_dataframe.empty:
        raise ValueError(
            f"工作表“{source_sheet}”的全部价差数据为空，无法生成标准价差表"
        )

    return long_dataframe, int(long_dataframe["spread_name"].nunique())


def _sheet_summary(
    dataframe: pd.DataFrame,
    source_sheet: str,
) -> dict[str, Any]:
    return {
        "source_sheet": source_sheet,
        "rows": len(dataframe),
        "earliest_date": (
            dataframe["date"].min().strftime("%Y-%m-%d")
            if not dataframe.empty
            else None
        ),
        "latest_date": (
            dataframe["date"].max().strftime("%Y-%m-%d")
            if not dataframe.empty
            else None
        ),
    }


def _warn_basis_mismatches(dataframe: pd.DataFrame) -> int:
    comparable = dataframe.dropna(
        subset=["basis", "futures_price", "cash_price"]
    ).copy()
    differences = (
        comparable["cash_price"]
        - comparable["futures_price"]
        - comparable["basis"]
    ).abs()
    mismatch_count = int((differences > 1).sum())
    if mismatch_count:
        LOGGER.warning(
            "basis_long 中有 %s 行不满足 cash_price ≈ futures_price + basis "
            "（绝对误差大于 1），程序将继续输出。",
            mismatch_count,
        )
    return mismatch_count


def _write_csvs(
    basis_long: pd.DataFrame,
    cash_price_long: pd.DataFrame,
    futures_close: pd.DataFrame,
    spot_spread_long: pd.DataFrame,
    output_dir: Path,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_paths = {
        "basis_long": output_dir / "国内现货基差_标准表.csv",
        "cash_price_long": output_dir / "国内现货一口价_标准表.csv",
        "futures_close": output_dir / "内盘期货收盘价.csv",
        "spot_spread": output_dir / "国内现货价差_标准表.csv",
    }
    dataframes = {
        "basis_long": basis_long,
        "cash_price_long": cash_price_long,
        "futures_close": futures_close,
        "spot_spread": spot_spread_long,
    }
    temporary_paths: dict[str, Path] = {}

    try:
        for name, dataframe in dataframes.items():
            temporary_path = output_paths[name].with_suffix(".csv.tmp")
            dataframe.to_csv(
                temporary_path,
                index=False,
                encoding="utf-8-sig",
                date_format="%Y-%m-%d",
            )
            temporary_paths[name] = temporary_path

        for name, temporary_path in temporary_paths.items():
            os.replace(temporary_path, output_paths[name])
    finally:
        for temporary_path in temporary_paths.values():
            if temporary_path.exists():
                temporary_path.unlink()

    return output_paths


def import_basis_excel(
    config_path: Path | str | None = None,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    resolved_config_path = (
        Path(config_path)
        if config_path is not None
        else CONFIG_DIR / "basis_excel.yaml"
    )
    resolved_output_dir = (
        Path(output_dir) if output_dir is not None else BASIS_PROCESSED_DIR
    )

    config = _load_config(resolved_config_path)
    source_path = _resolve_source_path(config)
    _validate_workbook(source_path, config)

    summaries: list[dict[str, Any]] = []
    basis_frames: list[pd.DataFrame] = []
    cash_price_frames: list[pd.DataFrame] = []

    with pd.ExcelFile(source_path, engine="openpyxl") as workbook:
        for sheet_name, sheet_config in config["basis_sheets"].items():
            source_frame = pd.read_excel(
                workbook,
                sheet_name=sheet_name,
                usecols=sheet_config["use_columns"],
            )
            standardized = _standardize_basis_sheet(
                source_frame,
                commodity=str(sheet_config["commodity"]),
                source_sheet=sheet_name,
            )
            basis_frames.append(standardized)
            summaries.append(_sheet_summary(standardized, sheet_name))

        for sheet_name, sheet_config in config["cash_price_sheets"].items():
            source_frame = pd.read_excel(
                workbook,
                sheet_name=sheet_name,
                usecols=sheet_config["use_columns"],
            )
            standardized = _standardize_cash_price_sheet(
                source_frame,
                commodity=str(sheet_config["commodity"]),
                source_sheet=sheet_name,
            )
            cash_price_frames.append(standardized)
            summaries.append(_sheet_summary(standardized, sheet_name))

        futures_config = config["futures_sheet"]
        futures_source = pd.read_excel(
            workbook,
            sheet_name=futures_config["sheet_name"],
        )
        futures_close = _standardize_futures_sheet(
            futures_source,
            date_column=futures_config["date_column"],
        )
        summaries.append(
            _sheet_summary(futures_close, futures_config["sheet_name"])
        )

        spot_spread_config = config["spot_spread_sheet"]
        spot_spread_source = pd.read_excel(
            workbook,
            sheet_name=spot_spread_config["sheet_name"],
            header=None,
        )
        spot_spread_long, spread_indicator_count = (
            _standardize_spot_spread_sheet(
                spot_spread_source,
                source_sheet=spot_spread_config["sheet_name"],
            )
        )
        spot_spread_summary = _sheet_summary(
            spot_spread_long,
            spot_spread_config["sheet_name"],
        )
        spot_spread_summary["spread_indicator_count"] = spread_indicator_count
        summaries.append(spot_spread_summary)

    basis_long = pd.concat(basis_frames, ignore_index=True)
    cash_price_long = pd.concat(cash_price_frames, ignore_index=True)
    mismatch_count = _warn_basis_mismatches(basis_long)
    output_paths = _write_csvs(
        basis_long,
        cash_price_long,
        futures_close,
        spot_spread_long,
        resolved_output_dir,
    )

    return {
        "source_file": source_path,
        **output_paths,
        "sheet_summaries": summaries,
        "spot_spread_summary": spot_spread_summary,
        "basis_mismatch_warnings": mismatch_count,
    }
