import assert from "node:assert/strict";
import test from "node:test";
import { applyQuarterRevisions } from "./comparison";
import type { CombinationComparison, CombinationData, MetricData } from "./model";

function metric(name: string, status: MetricData["mapping_status"], value: number | null, unit = "1000 T"): MetricData {
  return {
    metric: name,
    mapping_status: status,
    market_year_basis: "Oct–Sept",
    periods: ["2025/26"],
    original_periods: { "2025/26": "25/26F" },
    forecast_status: { "2025/26": "explicit_forecast" },
    original_metric: name,
    values: { "2025/26": value },
    annual_change: null,
    quarter_revision: null,
    quarter_revision_note: "暂无上一期",
    unit,
    original_unit: unit,
    source_report_id: ["AN1"],
    source_report_title: ["title"],
    source_sheet: ["AN1"],
    source_cells: { "2025/26": ["AN1!B3"] },
    source_cell_or_range: "AN1!B3:B3",
    is_derived: status === "derived",
    derivation_method: "",
    derivation_components: "",
    quality_note: "",
    has_footnote: false,
    has_star: false,
  };
}

function payload(metrics: MetricData[]): CombinationData {
  return {
    schema_version: 1,
    release: "2026-06",
    system: "Soybean System",
    product: "Soybeans",
    region: "Global",
    market_year_basis: ["Oct–Sept"],
    periods: ["2025/26"],
    forecast_status: { "2025/26": "explicit_forecast" },
    metrics,
    quality_note: "",
  };
}

test("直接和派生指标读取绝对季度修正，比例使用百分点", () => {
  const data = payload([
    metric("Production", "direct", 120),
    metric("Stocks/Use Ratio", "derived", 12, "%"),
  ]);
  const comparison = {
    records: [
      { metric: "Production", period: "2025/26", quarter_revision: 10, unit: "1000 T" },
      { metric: "Stocks/Use Ratio", period: "2025/26", quarter_revision: -0.5, unit: "percentage points" },
    ],
  } as CombinationComparison;
  const result = applyQuarterRevisions(data, comparison);
  assert.deepEqual(result.metrics[0].quarter_revision, { period: "2025/26", value: 10, unit: "1000 T" });
  assert.deepEqual(result.metrics[1].quarter_revision, { period: "2025/26", value: -0.5, unit: "percentage points" });
});

test("无上一期或不可比记录保持空值，不显示0", () => {
  const data = payload([metric("Imports", "missing", null)]);
  const withoutPrevious = applyQuarterRevisions(data, null);
  assert.equal(withoutPrevious.metrics[0].quarter_revision, null);
  assert.equal(withoutPrevious.metrics[0].quarter_revision_note, "暂无上一期");

  const comparison = {
    records: [{
      metric: "Imports",
      period: "2025/26",
      quarter_revision: null,
      unit: "1000 T",
      quality_note: "missing 状态不计算季度修正。",
    }],
  } as CombinationComparison;
  const unavailable = applyQuarterRevisions(data, comparison);
  assert.equal(unavailable.metrics[0].quarter_revision, null);
  assert.match(unavailable.metrics[0].quarter_revision_note, /不计算/);
});
