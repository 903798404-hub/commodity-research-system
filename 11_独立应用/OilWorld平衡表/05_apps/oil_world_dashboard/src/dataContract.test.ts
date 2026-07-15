import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import type { CombinationData, ReleaseIndex } from "./model";

const root = fileURLToPath(new URL("../../../public/data/oil_world/", import.meta.url));
type AuditedReleaseIndex = ReleaseIndex & { annual_change_count: number };
type ReleaseManifestEntry = {
  release: string;
  mapping_record_count: number;
  numeric_value_count: number;
  annual_change_count: number;
};
const latest = JSON.parse(fs.readFileSync(`${root}/latest.json`, "utf8"));
const releases = JSON.parse(fs.readFileSync(`${root}/releases.json`, "utf8"));
const marchIndex = JSON.parse(fs.readFileSync(`${root}/releases/2026-03/index.json`, "utf8")) as AuditedReleaseIndex;
const index = JSON.parse(fs.readFileSync(`${root}/releases/2026-06/index.json`, "utf8")) as AuditedReleaseIndex;
const comparisonIndex = JSON.parse(
  fs.readFileSync(`${root}/comparisons/2026-03_to_2026-06/index.json`, "utf8"),
);

function readCombination(product: string, region: string): CombinationData {
  const item = index.files.find((entry) => entry.product === product && entry.region === region);
  assert.ok(item);
  return JSON.parse(fs.readFileSync(`${root}/releases/2026-06/${item.path}`, "utf8"));
}

test("默认发布期来自latest且发布清单包含2026-06", () => {
  assert.equal(latest.release, "2026-06");
  const releaseIds = releases.releases.map((item: { release: string }) => item.release);
  assert.deepEqual(releaseIds, [...releaseIds].sort());
  assert.equal(releaseIds.at(-1), latest.release);
});

test("59个组合入口全部可解析", () => {
  assert.equal(index.files.length, 59);
  for (const item of index.files) {
    const payload = JSON.parse(fs.readFileSync(`${root}/releases/2026-06/${item.path}`, "utf8"));
    assert.equal(payload.product, item.product);
    assert.equal(payload.region, item.region);
  }
});

test("651个映射状态与审计一致", () => {
  const expectedStatus = { direct: 300, not_applicable: 182, derived: 79, conflict: 35, missing: 55 };
  assert.equal(marchIndex.mapping_record_count, 651);
  assert.equal(index.mapping_record_count, 651);
  assert.deepEqual(marchIndex.status_counts, expectedStatus);
  assert.deepEqual(index.status_counts, expectedStatus);
});

test("正式发布和季度修正统计使用精确基线", () => {
  assert.equal(marchIndex.numeric_observation_count, 1831);
  assert.equal(marchIndex.annual_change_count, 376);
  assert.equal(index.numeric_observation_count, 1845);
  assert.equal(index.annual_change_count, 376);
  assert.equal(comparisonIndex.comparison_record_count, 1961);
  assert.equal(comparisonIndex.calculated_revision_count, 1717);
  assert.equal(comparisonIndex.null_revision_count, 244);

  const published = new Map<string, ReleaseManifestEntry>(
    releases.releases.map((item: ReleaseManifestEntry) => [item.release, item]),
  );
  assert.deepEqual(
    [published.get("2026-03")?.mapping_record_count, published.get("2026-03")?.numeric_value_count, published.get("2026-03")?.annual_change_count],
    [651, 1831, 376],
  );
  assert.deepEqual(
    [published.get("2026-06")?.mapping_record_count, published.get("2026-06")?.numeric_value_count, published.get("2026-06")?.annual_change_count],
    [651, 1845, 376],
  );
});

test("G2未生成Imports和StocksUseRatio", () => {
  const metrics = new Map(readCombination("Palm Oil", "G2").metrics.map((metric) => [metric.metric, metric]));
  assert.equal(metrics.get("Imports")?.mapping_status, "missing");
  assert.deepEqual(metrics.get("Imports")?.values, {});
  assert.equal(metrics.get("Stocks/Use Ratio")?.mapping_status, "conflict");
});

test("G3库存冲突但安全生产指标已派生", () => {
  const metrics = new Map(readCombination("Soybeans", "G3").metrics.map((metric) => [metric.metric, metric]));
  assert.equal(metrics.get("Production")?.mapping_status, "derived");
  assert.equal(metrics.get("Beginning Stocks")?.mapping_status, "conflict");
  assert.deepEqual(metrics.get("Beginning Stocks")?.values, {});
});

test("Jan-Dec自然年与作物年度Production分别发布", () => {
  const brazil = readCombination("Soybeans", "Brazil");
  assert.ok(brazil.market_year_basis.some((item) => item.includes("Jan–Dec")));
  const production = brazil.metrics.filter((metric) => metric.metric === "Production");
  assert.equal(production.length, 2);
  const calendar = production.find((metric) => metric.period_family === "calendar_year" && metric.source_role === "balance")!;
  const crop = production.find((metric) => metric.period_family === "crop_year" && metric.source_role === "production_table")!;
  assert.equal(calendar.mapping_status, "direct");
  assert.deepEqual(calendar.periods, ["2026", "2025", "2024"]);
  const calendar2026 = calendar.values["2026"];
  assert.ok(calendar2026 !== null && calendar2026 !== undefined && calendar2026 > 0);
  assert.equal(crop.mapping_status, "direct");
  assert.deepEqual(crop.periods, ["2026/27", "2025/26", "2024/25"]);
  const crop202627 = crop.values["2026/27"];
  assert.ok(crop202627 !== null && crop202627 !== undefined && crop202627 > 0);
});

test("预测状态、派生标记和季度修正占位存在", () => {
  const palm = readCombination("Palm Oil", "G2");
  assert.equal(palm.forecast_status["2025/26"], "explicit_forecast");
  const output = palm.metrics.find((metric) => metric.metric === "Product Output")!;
  assert.equal(output.is_derived, true);
  assert.equal(output.quarter_revision, null);
  assert.equal(output.quarter_revision_note, "暂无上一期");
});

test("冲突质量提示只附着于冲突指标", () => {
  const palm = readCombination("Palm Oil", "G2");
  for (const metric of palm.metrics) {
    if (metric.mapping_status === "conflict") assert.match(metric.quality_note, /暂不展示该数值/);
    if (metric.mapping_status === "direct") assert.doesNotMatch(metric.quality_note, /暂不展示该数值/);
  }
});
