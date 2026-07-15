import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import type { CombinationData, ReleaseIndex } from "./model";

const root = fileURLToPath(new URL("../../../public/data/oil_world/", import.meta.url));
const latest = JSON.parse(fs.readFileSync(`${root}/latest.json`, "utf8"));
const releases = JSON.parse(fs.readFileSync(`${root}/releases.json`, "utf8"));
const index = JSON.parse(fs.readFileSync(`${root}/releases/2026-06/index.json`, "utf8")) as ReleaseIndex;

function readCombination(product: string, region: string): CombinationData {
  const item = index.files.find((entry) => entry.product === product && entry.region === region);
  assert.ok(item);
  return JSON.parse(fs.readFileSync(`${root}/releases/2026-06/${item.path}`, "utf8"));
}

test("默认发布期来自latest且发布清单包含2026-06", () => {
  assert.equal(latest.release, "2026-06");
  assert.deepEqual(releases.releases.map((item: { release: string }) => item.release), ["2026-06"]);
});

test("59个组合入口全部可解析", () => {
  assert.equal(index.files.length, 59);
  for (const item of index.files) {
    const payload = JSON.parse(fs.readFileSync(`${root}/releases/2026-06/${item.path}`, "utf8"));
    assert.equal(payload.product, item.product);
    assert.equal(payload.region, item.region);
  }
});

test("649个映射状态与审计一致", () => {
  assert.equal(index.mapping_record_count, 649);
  assert.deepEqual(index.status_counts, { direct: 286, not_applicable: 182, derived: 75, conflict: 51, missing: 55 });
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

test("Jan-Dec口径被披露但冲突值为空", () => {
  const brazil = readCombination("Soybeans", "Brazil");
  assert.ok(brazil.market_year_basis.some((item) => item.includes("Jan–Dec")));
  const production = brazil.metrics.find((metric) => metric.metric === "Production")!;
  assert.equal(production.mapping_status, "conflict");
  assert.deepEqual(production.values, {});
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
