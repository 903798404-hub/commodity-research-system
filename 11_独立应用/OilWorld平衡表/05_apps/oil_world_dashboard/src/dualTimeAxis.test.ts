import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import type { CombinationComparison, CombinationData, ReleaseIndex } from "./model";
import { applyQuarterRevisions } from "./comparison";
import {
  axisLabel,
  calendarBalanceMetrics,
  cardMetrics,
  cropProductionMetrics,
  hasDualTimeAxes,
  metricDisplayLabel,
  metricStableKey,
  periodHeaderLabel,
  periodsForMetrics,
  quarterRevisionDisplay,
} from "./selectors";

const previewRoot = fileURLToPath(new URL("../../../06_outputs/special_time_axis_preview/", import.meta.url));

function readJson<T>(path: string): T {
  return JSON.parse(fs.readFileSync(path, "utf8")) as T;
}

function releaseIndex(release: string): ReleaseIndex {
  return readJson(`${previewRoot}/releases/${release}/index.json`);
}

function combination(release: string, product: string, region: string): CombinationData {
  const index = releaseIndex(release);
  const file = index.files.find((item) => item.product === product && item.region === region);
  assert.ok(file);
  return readJson(`${previewRoot}/releases/${release}/${file.path}`);
}

function comparison(product: string, region: string): CombinationComparison {
  const index = readJson<ReleaseIndex>(`${previewRoot}/comparisons/2026-03_to_2026-06/index.json`);
  const file = index.files.find((item) => item.product === product && item.region === region);
  assert.ok(file);
  return readJson(`${previewRoot}/comparisons/2026-03_to_2026-06/${file.path}`);
}

test("同一组合保留两条Production", () => {
  const data = combination("2026-06", "Soybeans", "Brazil");
  assert.equal(data.metrics.filter((metric) => metric.metric === "Production").length, 2);
});

test("自然年Production位于自然年供需区块", () => {
  const rows = calendarBalanceMetrics(combination("2026-06", "Soybeans", "Brazil"));
  const production = rows.find((metric) => metric.metric === "Production");
  assert.equal(production?.period_family, "calendar_year");
  assert.equal(production?.source_role, "balance");
  assert.equal(rows.map((metric) => metric.metric).join("|"), "Beginning Stocks|Production|Imports|Exports|Crush|Domestic Consumption|Ending Stocks|Stocks/Use Ratio");
});

test("作物年度Production位于生产区块", () => {
  const rows = cropProductionMetrics(combination("2026-06", "Soybeans", "Brazil"));
  assert.deepEqual(rows.map((metric) => metric.metric), ["Production", "Area Harvested", "Yield"]);
  assert.equal(rows[0].period_family, "crop_year");
  assert.equal(rows[0].source_role, "production_table");
});

test("两个Production具有不同稳定键", () => {
  const metrics = combination("2026-06", "Soybeans", "Brazil").metrics.filter((metric) => metric.metric === "Production");
  assert.equal(new Set(metrics.map(metricStableKey)).size, 2);
  assert.deepEqual(metrics.map((metric) => metricDisplayLabel(metric, true)).sort(), ["Production（作物年度）", "Production（自然年）"]);
});

test("自然年表头显示2026F、2025、2024", () => {
  const periods = periodsForMetrics(calendarBalanceMetrics(combination("2026-06", "Soybeans", "Brazil")));
  assert.deepEqual(periods.map((period, index) => periodHeaderLabel(period, index, true)), ["2026F", "2025", "2024"]);
});

test("Argentina的2026F保留明确预测状态", () => {
  const production = calendarBalanceMetrics(combination("2026-06", "Sunflowerseed", "Argentina"))[1];
  assert.equal(production.forecast_status["2026"], "explicit_forecast");
  assert.equal(periodHeaderLabel("2026", 0, true), "2026F");
});

test("作物年度表头按发布期滚动", () => {
  assert.deepEqual(periodsForMetrics(cropProductionMetrics(combination("2026-03", "Soybeans", "Brazil"))), ["2025/26", "2024/25", "2023/24"]);
  assert.deepEqual(periodsForMetrics(cropProductionMetrics(combination("2026-06", "Soybeans", "Brazil"))), ["2026/27", "2025/26", "2024/25"]);
});

test("两个时间轴不共享季度修正", () => {
  const enriched = applyQuarterRevisions(
    combination("2026-06", "Soybeans", "Brazil"),
    comparison("Soybeans", "Brazil"),
  );
  const calendarProduction = calendarBalanceMetrics(enriched).find((metric) => metric.metric === "Production")!;
  const cropProduction = cropProductionMetrics(enriched).find((metric) => metric.metric === "Production")!;
  assert.equal(calendarProduction.quarter_revision?.period, "2026");
  assert.equal(calendarProduction.quarter_revision?.value, 1800);
  assert.equal(cropProduction.quarter_revision, null);
});

test("趋势选择器的两条Production保持独立时间轴", () => {
  const productions = combination("2026-06", "Soybeans", "Brazil").metrics.filter((metric) => metric.metric === "Production");
  assert.deepEqual(new Set(productions.map(axisLabel)), new Set(["自然年（Jan–Dec）", "Oil World作物年度"]));
  assert.ok(productions.every((metric) => metric.periods.every((period) => metric.values[period] !== undefined)));
});

test("只有两个组合启用双时间轴页面", () => {
  const index = releaseIndex("2026-06");
  const dual = index.files.filter((file) => {
    const data = readJson<CombinationData>(`${previewRoot}/releases/2026-06/${file.path}`);
    return hasDualTimeAxes(data);
  });
  assert.deepEqual(dual.map((item) => `${item.product}/${item.region}`).sort(), ["Soybeans/Brazil", "Sunflowerseed/Argentina"]);
});

test("2026-03没有上一期时季度修正显示短横线", () => {
  const data = applyQuarterRevisions(combination("2026-03", "Sunflowerseed", "Argentina"), null);
  for (const metric of cropProductionMetrics(data)) {
    assert.deepEqual(quarterRevisionDisplay(metric), { text: "—", hasValue: false });
  }
});

test("2026-06自然年季度修正正常显示", () => {
  const brazil = applyQuarterRevisions(combination("2026-06", "Soybeans", "Brazil"), comparison("Soybeans", "Brazil"));
  const argentina = applyQuarterRevisions(combination("2026-06", "Sunflowerseed", "Argentina"), comparison("Sunflowerseed", "Argentina"));
  assert.equal(calendarBalanceMetrics(brazil).find((metric) => metric.metric === "Ending Stocks")?.quarter_revision?.value, 1800);
  assert.equal(calendarBalanceMetrics(argentina).find((metric) => metric.metric === "Crush")?.quarter_revision?.value, -90);
});

test("1920宽屏表格最小宽度无需触发横向滚动", () => {
  const css = fs.readFileSync(fileURLToPath(new URL("./styles.css", import.meta.url)), "utf8");
  assert.match(css, /\.app-shell \{ width: min\(1520px, calc\(100% - 48px\)\)/);
  assert.match(css, /table \{ width: 100%; min-width: 920px;/);
});

test("900px以下徽标换行且表格使用独立滚动容器", () => {
  const css = fs.readFileSync(fileURLToPath(new URL("./styles.css", import.meta.url)), "utf8");
  assert.match(css, /@media \(max-width: 900px\)/);
  assert.match(css, /\.dual-basis-summary \{ justify-content: flex-start; text-align: left; \}/);
  assert.match(css, /\.table-scroll\s*\{[^}]*overflow-x:\s*auto;/);
});

test("临时数据开关只在开发服务器启用且首次加载沿用正式指针", () => {
  const viteConfig = fs.readFileSync(fileURLToPath(new URL("../vite.config.ts", import.meta.url)), "utf8");
  const appSource = fs.readFileSync(fileURLToPath(new URL("./App.tsx", import.meta.url)), "utf8");
  assert.match(viteConfig, /process\.env\.OIL_WORLD_PREVIEW_DATA_ROOT/);
  assert.match(viteConfig, /apply: "serve"/);
  assert.match(appSource, /loadBootstrap\(BASE_URL\)/);
  assert.doesNotMatch(appSource, /Brazil|Argentina/);
  assert.deepEqual(cardMetrics(combination("2026-06", "Soybeans", "Brazil")).map((metric) => metric.period_family), Array(5).fill("calendar_year"));
});
