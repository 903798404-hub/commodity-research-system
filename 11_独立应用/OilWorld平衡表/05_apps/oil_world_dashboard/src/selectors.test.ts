import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import type { CombinationData, ReleaseIndex } from "./model";
import { cardMetrics, displayUnit, displayValue, formatSignedChange, regionsFor, validSelection, visibleMetrics } from "./selectors";

const publicRoot = fileURLToPath(new URL("../../../public/data/oil_world/", import.meta.url));
const index = JSON.parse(fs.readFileSync(`${publicRoot}/releases/2026-06/index.json`, "utf8")) as ReleaseIndex;

function combination(product: string, region: string) {
  const item = index.files.find((entry) => entry.product === product && entry.region === region)!;
  return JSON.parse(fs.readFileSync(`${publicRoot}/releases/2026-06/${item.path}`, "utf8")) as CombinationData;
}

test("无效旧状态安全回退到第一个有效组合", () => {
  const selection = validSelection(index, { system: "大豆体系", product: "Palm Oil", region: "Other Countries" });
  assert.deepEqual(selection, { system: "大豆体系", product: "Soybeans", region: "Global" });
});

test("产品国家列表来自固定研究范围", () => {
  assert.deepEqual(regionsFor(index, "大豆体系", "Soybeans"), ["Global", "United States", "Brazil", "Argentina", "China", "G3"]);
  assert.deepEqual(regionsFor(index, "棕榈油体系", "Palm Oil"), ["Global", "Indonesia", "Malaysia", "G2", "India"]);
});

test("not_applicable指标不进入普通表格", () => {
  const data = combination("Palm Oil", "Global");
  assert.equal(visibleMetrics(data).some((metric) => metric.mapping_status === "not_applicable"), false);
  assert.equal(visibleMetrics(data).some((metric) => metric.metric === "Product Output"), true);
});

test("数量只在展示层由1000T换算为万吨", () => {
  assert.equal(displayValue(1234, "1000 T"), 123.4);
  assert.equal(displayValue(0, "1000 T"), 0);
  assert.equal(displayValue(null, "1000 T"), null);
  assert.equal(displayUnit("1000 T"), "万吨");
});

test("面积单产比例单位不误换算", () => {
  assert.equal(displayValue(1234, "1000 ha"), 1234);
  assert.equal(displayValue(3.42, "T/ha"), 3.42);
  assert.equal(displayValue(12.3, "%"), 12.3);
  assert.equal(displayUnit("percentage points"), "百分点");
});

test("状态卡按上游与产品指标动态选择", () => {
  assert.equal(cardMetrics(combination("Soybeans", "Global"))[0].metric, "Production");
  assert.equal(cardMetrics(combination("Palm Oil", "Global"))[0].metric, "Product Output");
});

test("变化值保留统一的正负号格式", () => {
  assert.equal(formatSignedChange(12.5, "1000 T"), "+12.5");
  assert.equal(formatSignedChange(-12.5, "1000 T"), "−12.5");
  assert.equal(formatSignedChange(0, "1000 T"), "0");
  assert.equal(formatSignedChange(null, "1000 T"), "—");
});
