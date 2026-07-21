from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pandas as pd
import pytest
from agri_research_agent.data_sources.weather_adapter import load_weather_records


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "04_scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import import_soybean_weather_snapshot as snapshot_import


def _sql_table(table: str, columns: list[str], rows: list[list[object]]) -> str:
    definitions = ",\n  ".join(f"`{column}` double" for column in columns[1:])
    create = f"CREATE TABLE `{table}` (\n  `{columns[0]}` date,\n  {definitions}\n);\n"

    def literal(value: object) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, str):
            return "'" + value + "'"
        return str(value)

    values = ",\n".join("(" + ",".join(literal(value) for value in row) + ")" for row in rows)
    return create + f"INSERT INTO `{table}` VALUES\n{values};\n"


def _minimal_snapshot(path: Path, *, conflict: bool = False) -> None:
    config = snapshot_import.load_weather_config(snapshot_import.CONFIG_FILE)
    statements = ["CREATE TABLE `unrelated_raw_table` (`value` double);", "INSERT INTO `unrelated_raw_table` VALUES (99);"]
    for table, spec in snapshot_import.TARGET_TABLES.items():
        columns = snapshot_import.expected_columns(config, spec)
        row = ["2026-06-22", *range(1, len(columns))]
        rows: list[list[object]] = [row]
        if table == "美国_降雨":
            # A fully empty trailing source row must not become the latest valid date.
            rows.append(["2026-06-23", *([None] * (len(columns) - 1))])
            if conflict:
                rows.append(["2026-06-22", *range(2, len(columns) + 1)])
        statements.append(_sql_table(table, columns, rows))
    path.write_text("\n".join(statements), encoding="utf-8", newline="\n")


@pytest.fixture()
def isolated_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(snapshot_import, "CANDIDATE_ROOT", tmp_path / "candidates")
    monkeypatch.setattr(snapshot_import, "STABLE_FILE", tmp_path / "processed" / "soybean_weather_us.parquet")
    monkeypatch.setattr(snapshot_import, "BACKUP_ROOT", tmp_path / "backups")
    monkeypatch.setattr(snapshot_import, "STATUS_FILE", tmp_path / "status" / "soybean_weather_us.json")
    return tmp_path


def test_target_table_identification_and_utf8_wide_to_long_promotion(isolated_paths: Path) -> None:
    source = isolated_paths / "天气2.0.sql"
    _minimal_snapshot(source)

    result = snapshot_import.import_snapshot(source, run_id="unit-test", promote=True)
    stable = Path(str(result["stable_file"]))
    records = pd.read_parquet(stable)
    status = json.loads(snapshot_import.STATUS_FILE.read_text(encoding="utf-8"))

    assert stable.is_file()
    assert len(records) == len(snapshot_import.TARGET_TABLES) * 15
    assert set(records["country"]) == {"USA"}
    assert set(records["metric"]) == {"precipitation", "temperature_max", "temperature_min", "soil_moisture"}
    assert set(records["model"]) == {"observed", "ECMWF", "GFS"}
    assert set(records["region"]) == {region["key"] for region in snapshot_import.load_weather_config(snapshot_import.CONFIG_FILE)["regions"]}
    assert set(records.loc[records["metric"] == "soil_moisture", "unit"]) == {"原始值，单位待确认"}
    assert status["observed_latest_date"] == "2026-06-22"
    assert status["status"] == "success"
    assert "stable_file_sha256" in status
    assert not any("unrelated" in table for table in result["quality"]["table_ranges"])

    selected = load_weather_records(stable, crop="soybean", country="USA", metric="precipitation")
    assert len(selected) == 15 * 3
    assert set(selected["model"]) == {"observed", "ECMWF", "GFS"}


def test_conflicting_unique_key_stops_before_promotion(isolated_paths: Path) -> None:
    source = isolated_paths / "天气2.0.sql"
    _minimal_snapshot(source, conflict=True)

    with pytest.raises(snapshot_import.SnapshotParseError, match="重复或冲突"):
        snapshot_import.import_snapshot(source, run_id="conflict", promote=True)
    assert not snapshot_import.STABLE_FILE.exists()


def test_non_target_insert_is_not_value_parsed() -> None:
    malformed_unrelated = "INSERT INTO `unrelated_raw_table` VALUES (not_a_supported_literal);"
    assert snapshot_import.insert_table_name(malformed_unrelated) == "unrelated_raw_table"
    assert "unrelated_raw_table" not in snapshot_import.TARGET_TABLES
