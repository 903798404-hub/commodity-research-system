from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "04_scripts" / "weather" / "refresh_weather_from_sql.py"
SPEC = importlib.util.spec_from_file_location("weather_sql_refresh", SCRIPT_PATH)
assert SPEC and SPEC.loader
weather_refresh = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = weather_refresh
SPEC.loader.exec_module(weather_refresh)


def _target_for_us() -> weather_refresh.DailyTarget:
    config_path = PROJECT_ROOT / "02_configs" / "soybean_weather_us.yaml"
    config = weather_refresh.load_weather_config(config_path)
    return weather_refresh.DailyTarget(
        config_path=config_path,
        relative_path=Path("soybean/us/soybean_weather_us.parquet"),
        config=config,
        table_specs=dict(weather_refresh.TARGET_TABLES),
        reference_schema={},
        reference_columns=[],
    )


def test_us_sql_v2_country_prefixed_columns_map_to_existing_contract() -> None:
    target = _target_for_us()
    table_name = "美国_降雨"
    expected = weather_refresh.expected_columns(target.config, target.table_specs[table_name])
    actual = [expected[0], *(f"美国_{column.lstrip('_')}" for column in expected[1:])]

    assert weather_refresh._contract_columns(target, table_name, actual) == expected


def test_unknown_sql_column_shape_is_rejected() -> None:
    target = _target_for_us()
    with pytest.raises(weather_refresh.SnapshotParseError, match="受控映射"):
        weather_refresh._contract_columns(target, "美国_降雨", ["日期", "美国_不存在的地区"])


def test_quote_aware_parser_handles_chinese_null_negative_and_escaped_comma() -> None:
    parsed = weather_refresh.parse_insert_statement(
        "INSERT INTO `中国_测试` VALUES ('2026-08-05','O\\'Brien, north',NULL,-1.25);"
    )

    assert parsed == ("中国_测试", [["2026-08-05", "O'Brien, north", None, -1.25]])


def test_static_baseline_copy_is_byte_preserving(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(weather_refresh, "STATIC_BASELINE_FILES", ("normal.parquet",))
    source = tmp_path / "source"
    source.mkdir()
    baseline = source / "normal.parquet"
    pd.DataFrame({"month_day": ["01-01", "12-31"], "normal_value": [1.0, 2.0]}).to_parquet(baseline, index=False)

    next_dir, before = weather_refresh._prepare_next(tmp_path / "processed", source)
    after = weather_refresh._parquet_metadata(next_dir / "normal.parquet", date_column="month_day")

    assert before["normal.parquet"]["sha256"] == after["sha256"]
    assert before["normal.parquet"]["min_month_day"] == "01-01"
    assert before["normal.parquet"]["max_month_day"] == "12-31"


def test_promotion_failure_restores_previous_current(tmp_path: Path) -> None:
    processed = tmp_path / "processed"
    current = processed / "current"
    next_dir = processed / "next"
    current.mkdir(parents=True)
    next_dir.mkdir(parents=True)
    (current / "old.txt").write_text("old", encoding="utf-8")
    (next_dir / "new.txt").write_text("new", encoding="utf-8")
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("block", encoding="utf-8")

    with pytest.raises(FileExistsError):
        weather_refresh._promote(processed, next_dir, blocker / "status.json", {"status": "success"})

    assert (processed / "current" / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (processed / "current" / "new.txt").exists()
