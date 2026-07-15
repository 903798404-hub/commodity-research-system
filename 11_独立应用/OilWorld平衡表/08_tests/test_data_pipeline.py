from __future__ import annotations

import hashlib
import json
import sys
import unittest
from collections import Counter
from pathlib import Path

import openpyxl


ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "01_data" / "releases" / "2026-06"
PUBLIC_RELEASE = ROOT / "public" / "data" / "oil_world" / "releases" / "2026-06"
WORKBOOK = ROOT / "01_原始资料" / "2026-06" / "油世界季度表-June 2026.xlsx"
EXPECTED_HASH = "f113830abd5feab1fc12b9e4cf3327f90c8e4f6b834d74515650faff3a9dc20e"
sys.path.insert(0, str(ROOT / "03_src"))

from oil_world_data.generator import _numeric_or_none  # noqa: E402


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class OilWorldDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = load_json(RELEASE / "index.json")
        cls.combinations = {
            (item["product"], item["region"]): load_json(RELEASE / item["path"])
            for item in cls.index["files"]
        }

    def test_01_fixed_product_and_region_scope(self):
        expected = {
            "Soybeans": ["Global", "United States", "Brazil", "Argentina", "China", "G3"],
            "Soybean Oil": ["Global", "United States", "Brazil", "Argentina", "China", "G3"],
            "Soybean Meal": ["Global", "United States", "Brazil", "Argentina", "China", "G3"],
            "Rapeseed / Canola": ["Global", "Canada", "European Union", "Australia", "China", "Russia", "Ukraine"],
            "Rapeseed Oil": ["Global", "Canada", "European Union", "Australia", "China", "Russia", "Ukraine"],
            "Rapeseed Meal": ["Global", "Canada", "European Union", "Australia", "China", "Russia", "Ukraine"],
            "Sunflowerseed": ["Global", "Russia", "Ukraine", "European Union", "Argentina"],
            "Sunflower Oil": ["Global", "Russia", "Ukraine", "European Union", "Argentina"],
            "Sunflower Meal": ["Global", "Russia", "Ukraine", "European Union", "Argentina"],
            "Palm Oil": ["Global", "Indonesia", "Malaysia", "G2", "India"],
        }
        actual = {product["label"]: product["regions"] for system in self.index["systems"] for product in system["products"]}
        self.assertEqual(actual, expected)
        self.assertEqual(self.index["combination_count"], 59)

    def test_02_all_mapping_statuses_match_audit(self):
        counts = Counter(
            metric["mapping_status"]
            for payload in self.combinations.values()
            for metric in payload["metrics"]
        )
        self.assertEqual(
            counts,
            Counter({"direct": 286, "derived": 75, "missing": 55, "not_applicable": 182, "conflict": 51}),
        )

    def test_03_not_applicable_is_separate_from_missing(self):
        payload = self.combinations[("Palm Oil", "Indonesia")]
        statuses = {metric["metric"]: metric["mapping_status"] for metric in payload["metrics"]}
        self.assertEqual(statuses["Production"], "not_applicable")
        self.assertEqual(statuses["Imports"], "missing")

    def test_04_blank_and_zero_are_distinct(self):
        values = [
            value
            for payload in self.combinations.values()
            for metric in payload["metrics"]
            for value in metric["values"].values()
        ]
        self.assertTrue(any(value is None for value in values))
        self.assertIsNone(_numeric_or_none(None, "test!A1"))
        self.assertEqual(_numeric_or_none(0, "test!A2"), 0.0)

    def test_05_domestic_consumption_derivation(self):
        payload = self.combinations[("Soybeans", "United States")]
        metric = next(item for item in payload["metrics"] if item["metric"] == "Domestic Consumption")
        workbook = openpyxl.load_workbook(WORKBOOK, read_only=True, data_only=True)
        sheet = workbook["AN40502B"]
        expected = float(sheet["B7"].value) + float(sheet["B8"].value)
        self.assertEqual(metric["values"]["2026/27"], expected)
        self.assertTrue(metric["is_derived"])

    def test_06_g2_only_generates_audited_safe_metrics(self):
        metrics = {item["metric"]: item for item in self.combinations[("Palm Oil", "G2")]["metrics"]}
        safe = {"Beginning Stocks", "Product Output", "Exports", "Domestic Consumption", "Ending Stocks"}
        self.assertTrue(all(metrics[name]["mapping_status"] == "derived" for name in safe))
        self.assertEqual(metrics["Imports"]["mapping_status"], "missing")
        self.assertEqual(metrics["Stocks/Use Ratio"]["mapping_status"], "conflict")
        self.assertEqual(metrics["Imports"]["values"], {})

    def test_07_g3_only_generates_audited_safe_metrics(self):
        soybean = {item["metric"]: item for item in self.combinations[("Soybeans", "G3")]["metrics"]}
        for name in ["Production", "Imports", "Exports", "Crush", "Area Harvested", "Yield"]:
            self.assertEqual(soybean[name]["mapping_status"], "derived")
        for name in ["Beginning Stocks", "Domestic Consumption", "Ending Stocks", "Stocks/Use Ratio"]:
            self.assertEqual(soybean[name]["mapping_status"], "conflict")
            self.assertEqual(soybean[name]["values"], {})

    def test_08_only_complete_annual_periods_are_published(self):
        forbidden = ("Apr Sept", "Oct Mar", "Apr–Sept", "Oct–Mar")
        for payload in self.combinations.values():
            for metric in payload["metrics"]:
                for original_period in metric["original_periods"].values():
                    self.assertFalse(any(token in original_period for token in forbidden))
                self.assertTrue(all(period.startswith("20") for period in metric["periods"]))

    def test_09_natural_year_conflicts_are_disclosed(self):
        brazil = self.combinations[("Soybeans", "Brazil")]
        argentina = self.combinations[("Sunflowerseed", "Argentina")]
        self.assertTrue(any("Jan–Dec" in basis for basis in brazil["market_year_basis"]))
        self.assertTrue(any("Jan–Dec" in basis for basis in argentina["market_year_basis"]))
        brazil_production = next(item for item in brazil["metrics"] if item["metric"] == "Production")
        self.assertEqual(brazil_production["mapping_status"], "conflict")
        self.assertEqual(brazil_production["values"], {})

    def test_10_annual_change_is_absolute_difference(self):
        payload = self.combinations[("Palm Oil", "Global")]
        metric = next(item for item in payload["metrics"] if item["metric"] == "Product Output")
        change = metric["annual_change"]
        self.assertEqual(change["value"], metric["values"][change["current_period"]] - metric["values"][change["previous_period"]])
        self.assertEqual(change["unit"], "1000 T")

    def test_11_ratio_change_uses_percentage_points(self):
        payload = self.combinations[("Soybeans", "United States")]
        metric = next(item for item in payload["metrics"] if item["metric"] == "Stocks/Use Ratio")
        self.assertEqual(metric["annual_change"]["unit"], "percentage points")
        self.assertAlmostEqual(
            metric["annual_change"]["value"],
            metric["values"][metric["annual_change"]["current_period"]]
            - metric["values"][metric["annual_change"]["previous_period"]],
            places=7,
        )

    def test_12_quarter_revision_is_null(self):
        for payload in self.combinations.values():
            for metric in payload["metrics"]:
                self.assertIsNone(metric["quarter_revision"])
                self.assertEqual(metric["quarter_revision_note"], "暂无上一期")

    def test_13_source_traceability_is_complete(self):
        for payload in self.combinations.values():
            for metric in payload["metrics"]:
                if metric["mapping_status"] in {"direct", "derived"}:
                    self.assertTrue(metric["source_report_id"])
                    self.assertTrue(metric["source_sheet"])
                    self.assertTrue(metric["source_cell_or_range"])
                    self.assertEqual(set(metric["periods"]), set(metric["source_cells"]))

    def test_14_thirty_source_cell_checks_pass(self):
        report = load_json(RELEASE / "spot_check_report.json")
        self.assertEqual(report["count"], 30)
        self.assertTrue(report["passed"])
        self.assertEqual(sum(item["passed"] for item in report["checks"]), 30)

    def test_15_source_workbook_hash_is_unchanged(self):
        self.assertEqual(sha256(WORKBOOK), EXPECTED_HASH)
        quality = load_json(RELEASE / "quality_report.json")
        self.assertEqual(quality["source_workbook_sha256"], EXPECTED_HASH)

    def test_16_public_release_mirrors_internal_release(self):
        self.assertEqual(load_json(RELEASE / "index.json"), load_json(PUBLIC_RELEASE / "index.json"))
        for item in self.index["files"]:
            self.assertTrue((PUBLIC_RELEASE / item["path"]).exists())
            self.assertEqual(sha256(RELEASE / item["path"]), sha256(PUBLIC_RELEASE / item["path"]))

    def test_17_latest_and_release_list_include_backfill_without_moving_latest(self):
        latest = load_json(ROOT / "public" / "data" / "oil_world" / "latest.json")
        releases = load_json(ROOT / "public" / "data" / "oil_world" / "releases.json")
        self.assertEqual(latest["release"], "2026-06")
        self.assertEqual(
            [item["release"] for item in releases["releases"]],
            ["2026-03", "2026-06"],
        )
        self.assertEqual(releases["releases"][0]["next_release"], "2026-06")
        self.assertEqual(releases["releases"][1]["previous_release"], "2026-03")

    def test_18_all_index_files_exist_and_are_valid(self):
        self.assertEqual(len(self.index["files"]), 59)
        for item in self.index["files"]:
            payload = load_json(RELEASE / item["path"])
            self.assertEqual(payload["release"], "2026-06")
            self.assertEqual((payload["system"], payload["product"], payload["region"]), (item["system"], item["product"], item["region"]))


if __name__ == "__main__":
    unittest.main()
