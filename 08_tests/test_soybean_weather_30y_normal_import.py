from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "04_scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import import_soybean_weather_30y_normal as normal_import


def _normal_frame() -> pd.DataFrame:
    config = normal_import.load_weather_config(normal_import.CONFIG_FILE)
    rows: list[dict[str, object]] = []
    for metric, unit, source_sheet in (
        ("precipitation", "mm", "美国历史30年平均降雨"),
        ("temperature_max", "degC", "美国历史30年平均最高气温"),
    ):
        for date in pd.date_range("2025-01-01", "2025-12-31", freq="D"):
            for region in config["regions"]:
                rows.append(
                    {
                        "month_day": date.strftime("%m-%d"),
                        "country": "USA",
                        "region": region["key"],
                        "metric": metric,
                        "normal_value": 1.0,
                        "unit": unit,
                        "baseline_label": normal_import.BASELINE_LABEL,
                        "source_workbook_sha256": "a" * 64,
                        "source_sheet": source_sheet,
                    }
                )
    return pd.DataFrame(rows, columns=normal_import.REQUIRED_COLUMNS)


def test_authorized_workbook_target_sheets_and_15_state_static_schema() -> None:
    assert normal_import.TARGET_SHEETS == {
        "美国历史30年平均降雨": ("precipitation", "mm"),
        "美国历史30年平均最高气温": ("temperature_max", "degC"),
    }
    config = normal_import.load_weather_config(normal_import.CONFIG_FILE)
    quality = normal_import.validate_normals(_normal_frame(), config)

    assert quality["row_count"] == 365 * 15 * 2
    assert quality["metric_counts"] == {"precipitation": 365 * 15, "temperature_max": 365 * 15}
    assert quality["month_day_range"] == {"start": "01-01", "end": "12-31"}


def test_30_year_normal_validation_rejects_missing_state_day() -> None:
    config = normal_import.load_weather_config(normal_import.CONFIG_FILE)
    malformed = _normal_frame().iloc[1:].copy()

    with pytest.raises(normal_import.NormalImportError, match="365"):
        normal_import.validate_normals(malformed, config)
