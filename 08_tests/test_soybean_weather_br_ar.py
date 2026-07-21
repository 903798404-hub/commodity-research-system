from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import Workbook


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "04_scripts"
APPS_DIR = PROJECT_ROOT / "05_apps"
SRC_DIR = PROJECT_ROOT / "03_src"
for directory in (SCRIPTS_DIR, APPS_DIR, SRC_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import import_soybean_weather_30y_normal as normal_import
import import_soybean_weather_snapshot as snapshot_import
import soybean_weather_page
from agri_research_agent.weather.soybean_weather import (
    add_season_columns,
    load_weather_config,
    region_weights,
    weighted_values,
)


CONFIGS = {
    "BRA": PROJECT_ROOT / "02_configs" / "soybean_weather_br.yaml",
    "ARG": PROJECT_ROOT / "02_configs" / "soybean_weather_ar.yaml",
}


def _sql_table(table: str, columns: list[str]) -> str:
    definitions = ",\n  ".join(f"`{column}` double" for column in columns[1:])
    row = ["'2026-06-16'", *[str(index) for index in range(1, len(columns))]]
    return f"CREATE TABLE `{table}` (\n  `{columns[0]}` date,\n  {definitions}\n);\nINSERT INTO `{table}` VALUES\n(" + ",".join(row) + ");\n"


@pytest.mark.parametrize("country, expected_count, expected_coverage", [("BRA", 9, 88.8), ("ARG", 4, 87.0)])
def test_approved_weights_use_fixed_denominator_without_missing_region_renormalization(country: str, expected_count: int, expected_coverage: float) -> None:
    config = load_weather_config(CONFIGS[country])
    regions = region_weights(config)
    assert len(regions) == expected_count
    assert regions["weight"].sum() == pytest.approx(expected_coverage)
    values = pd.Series({str(region.key): 10.0 for region in regions.iloc[:-1].itertuples(index=False)})
    weighted, actual_coverage = weighted_values(values, config)
    assert weighted == pytest.approx(10.0 * actual_coverage / expected_coverage)
    assert actual_coverage == pytest.approx(expected_coverage - float(regions.iloc[-1].weight))


@pytest.mark.parametrize(
    "country, sample_date, expected_season, reference_start, reference_end",
    [
        ("BRA", "2026-02-28", "2025/2026", "2000-09-01", "2001-06-30"),
        ("ARG", "2026-02-28", "2025/2026", "2000-11-01", "2001-06-30"),
    ],
)
def test_cross_year_season_labels_reference_axes_and_leap_day(country: str, sample_date: str, expected_season: str, reference_start: str, reference_end: str) -> None:
    config = load_weather_config(CONFIGS[country])
    records = pd.DataFrame({"date": pd.to_datetime([sample_date, "2024-02-29"]), "value": [1.0, 2.0]})
    enriched = add_season_columns(records, config)
    assert enriched.loc[0, "season"] == expected_season
    start, end = soybean_weather_page._reference_window(
        str(config["chart_windows"]["cumulative_rain"]["start"]),
        str(config["chart_windows"]["cumulative_rain"]["end"]),
    )
    assert (str(start.date()), str(end.date())) == (reference_start, reference_end)
    assert soybean_weather_page._reference_date(pd.Timestamp("2024-02-29"), str(config["season_start_month_day"])) == pd.Timestamp("2001-02-28")


@pytest.mark.parametrize("country", ["BRA", "ARG"])
def test_configured_snapshot_import_uses_only_display_regions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, country: str) -> None:
    config_file = CONFIGS[country]
    config = load_weather_config(config_file)
    tables = snapshot_import.target_tables(config)
    source = tmp_path / f"{country}.sql"
    source.write_text("\n".join(_sql_table(table, snapshot_import.expected_columns(config, spec)) for table, spec in tables.items()), encoding="utf-8")
    monkeypatch.setattr(
        snapshot_import,
        "_country_paths",
        lambda _config_file, _config: (tmp_path / "candidates", tmp_path / "stable.parquet", tmp_path / "backups", tmp_path / "status.json", country.lower()),
    )
    result = snapshot_import.import_snapshot(source, run_id="unit", promote=True, config_file=config_file)
    records = pd.read_parquet(Path(result["stable_file"]))
    assert set(records["country"]) == {country}
    assert set(records["region"]) == set(region["key"] for region in config["regions"])
    assert len(records) == len(tables) * len(config["regions"])


@pytest.mark.parametrize("country", ["BRA", "ARG"])
def test_configured_normal_import_extracts_static_daily_display_regions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, country: str) -> None:
    config_file = CONFIGS[country]
    config = load_weather_config(config_file)
    workbook = Workbook()
    workbook.remove(workbook.active)
    for sheet_name in normal_import.target_sheets(config):
        sheet = workbook.create_sheet(sheet_name)
        source_regions = config.get("source_regions", config["regions"])
        sheet.append(["日期", *[region["display_name"] for region in source_regions]])
        for date in pd.date_range("2025-01-01", "2025-12-31", freq="D"):
            sheet.append([date.to_pydatetime(), *[1.0] * len(source_regions)])
    source = tmp_path / f"{country}.xlsx"
    workbook.save(source)
    monkeypatch.setattr(
        normal_import,
        "_country_paths",
        lambda _config_file, _config: (tmp_path / "candidates", tmp_path / "stable.parquet", tmp_path / "backups", tmp_path / "status.json", country.lower()),
    )
    result = normal_import.import_workbook(source, run_id="unit", promote_stable=True, config_file=config_file)
    records = pd.read_parquet(Path(result["stable"]))
    assert set(records["country"]) == {country}
    assert len(records) == 365 * len(config["regions"]) * 2
