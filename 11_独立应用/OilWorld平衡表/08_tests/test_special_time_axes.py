from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREVIEW = ROOT / "06_outputs" / "special_time_axis_preview"
sys.path.insert(0, str(ROOT / "03_src"))

from oil_world_data.release_pipeline import _validate_snapshot  # noqa: E402


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def metrics(release: str, filename: str):
    payload = load(PREVIEW / "releases" / release / "combinations" / filename)
    return {(item["metric"], item["period_family"], item["source_role"]): item for item in payload["metrics"]}


class SpecialTimeAxisTests(unittest.TestCase):
    def test_preview_snapshots_validate(self):
        _validate_snapshot(PREVIEW / "releases" / "2026-03", "2026-03")
        _validate_snapshot(PREVIEW / "releases" / "2026-06", "2026-06")

    def test_two_production_series_have_distinct_stable_keys(self):
        for filename in ("soybeans__brazil.json", "sunflowerseed__argentina.json"):
            data = metrics("2026-06", filename)
            self.assertIn(("Production", "calendar_year", "balance"), data)
            self.assertIn(("Production", "crop_year", "production_table"), data)

    def test_calendar_balance_sources_and_derivations(self):
        brazil = metrics("2026-06", "soybeans__brazil.json")
        self.assertEqual(brazil[("Beginning Stocks", "calendar_year", "balance")]["source_cell_or_range"], "AN51000A!B3:D3")
        self.assertEqual(brazil[("Domestic Consumption", "calendar_year", "balance")]["values"]["2026"], 65030)
        self.assertAlmostEqual(brazil[("Stocks/Use Ratio", "calendar_year", "balance")]["values"]["2026"], 6800 / 65030 * 100)
        self.assertEqual(brazil[("Stocks/Use Ratio", "calendar_year", "balance")]["unit"], "%")

    def test_argentina_forecast_marker_is_preserved(self):
        argentina = metrics("2026-06", "sunflowerseed__argentina.json")
        production = argentina[("Production", "calendar_year", "balance")]
        self.assertEqual(production["original_periods"]["2026"], "Jan Dec 2026F")
        self.assertEqual(production["forecast_status"]["2026"], "explicit_forecast")

    def test_comparison_never_crosses_period_families(self):
        comparison = load(PREVIEW / "comparisons" / "2026-03_to_2026-06" / "combinations" / "soybeans__brazil.json")
        keys = {(record["metric"], record["period"], record["period_family"], record["source_role"]) for record in comparison["records"]}
        self.assertIn(("Production", "2026", "calendar_year", "balance"), keys)
        self.assertIn(("Production", "2025/26", "crop_year", "production_table"), keys)
        self.assertEqual(len(keys), len(comparison["records"]))

    def test_nonoverlapping_crop_years_are_not_calculated(self):
        comparison = load(PREVIEW / "comparisons" / "2026-03_to_2026-06" / "combinations" / "sunflowerseed__argentina.json")
        records = {(r["metric"], r["period"], r["period_family"]): r for r in comparison["records"]}
        for period in ("2026/27", "2023/24"):
            record = records[("Production", period, "crop_year")]
            self.assertIsNone(record["quarter_revision"])
            self.assertEqual(record["comparison_status"], "value_missing")

    def test_ratio_revision_is_percentage_points(self):
        comparison = load(PREVIEW / "comparisons" / "2026-03_to_2026-06" / "combinations" / "sunflowerseed__argentina.json")
        record = next(r for r in comparison["records"] if r["metric"] == "Stocks/Use Ratio" and r["period"] == "2026")
        self.assertEqual(record["unit"], "percentage points")
        self.assertAlmostEqual(record["quarter_revision"], 3.26887972)

    def test_mapping_counts(self):
        index = load(PREVIEW / "releases" / "2026-06" / "index.json")
        self.assertEqual(index["mapping_record_count"], 651)
        self.assertEqual(index["status_counts"], {"direct": 300, "not_applicable": 182, "derived": 79, "conflict": 35, "missing": 55})

    def test_other_57_business_payloads_are_unchanged(self):
        audit = load(PREVIEW / "special_time_axis_audit.json")
        self.assertTrue(audit["passed"])
        self.assertEqual(audit["other_57_business_differences"], [])


if __name__ == "__main__":
    unittest.main()
