import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { loadBootstrap } from "./api";
import type { CombinationData, ReleaseIndex } from "./model";
import {
  PRESENTATION_SLIDES,
  isPresentationRoute,
  pageFromSearch,
  pageIndexForKey,
  releaseFromSearch,
  searchForRelease,
  searchForSlide,
} from "./presentation/config";
import {
  metricsInOrder,
  presentationCellValue,
  presentationMetricLabel,
  recentAvailablePeriods,
} from "./presentation/selectors";
import {
  PRESENTATION_PERIOD_BASIS,
  presentationPeriodBasis,
  presentationPeriodFooterDetails,
} from "./presentation/presentationPeriodBasis";
import {
  applyPresentationStockUsageRatio,
  PRESENTATION_STOCK_USAGE_FOOTER_NOTE,
  PRESENTATION_STOCK_USAGE_RATIO,
} from "./presentation/presentationStockUsageRatio";
import {
  displayUnit,
  displayValue,
  metricStableKey,
  periodHeaderLabel,
  quarterRevisionDisplay,
} from "./selectors";

const publicRoot = fileURLToPath(new URL("../../../public/data/oil_world/", import.meta.url));
const expectedRegions = ["Global", "United States", "Brazil", "Argentina", "China", "G3"];
const regionTableSource = fs.readFileSync(fileURLToPath(new URL("./presentation/components/RegionResearchTable.tsx", import.meta.url)), "utf8");
const slideTabsSource = fs.readFileSync(fileURLToPath(new URL("./presentation/components/PresentationSlideTabs.tsx", import.meta.url)), "utf8");
const navigationSource = fs.readFileSync(fileURLToPath(new URL("./presentation/components/PresentationNavigation.tsx", import.meta.url)), "utf8");

function readJson<T>(path: string): T {
  return JSON.parse(fs.readFileSync(path, "utf8")) as T;
}

function releaseIndex(release: string): ReleaseIndex {
  return readJson(`${publicRoot}/releases/${release}/index.json`);
}

function combination(release: string, region: string): CombinationData {
  return productCombination(release, "Soybeans", region);
}

function productCombination(release: string, product: string, region: string): CombinationData {
  const index = releaseIndex(release);
  const file = index.files.find((item) => item.product === product && item.region === region);
  assert.ok(file);
  return readJson(`${publicRoot}/releases/${release}/${file.path}`);
}

function presentationProductCombination(
  release: string,
  product: string,
  region: string,
  periodFamily?: string,
  sourceRole?: string,
): CombinationData {
  const current = productCombination(release, product, region);
  const previous = release === "2026-06" ? productCombination("2026-03", product, region) : null;
  return applyPresentationStockUsageRatio(current, previous, { periodFamily, sourceRole });
}

function presentationRatio(data: CombinationData) {
  const ratio = data.metrics.find((metric) => metric.metric === PRESENTATION_STOCK_USAGE_RATIO);
  assert.ok(ratio);
  return ratio;
}

function roundedPercent(value: number | null | undefined): number | null {
  return typeof value === "number" ? Number(value.toFixed(2)) : null;
}

test("/presentation可访问且根路由仍属于详细看板", () => {
  assert.equal(isPresentationRoute("/presentation"), true);
  assert.equal(isPresentationRoute("/research/oil-world/presentation/"), true);
  assert.equal(isPresentationRoute("/"), false);
});

test("默认发布期读取latest且latest使用no-store", async () => {
  const calls: Array<{ url: string; cache?: RequestCache }> = [];
  const fetcher: typeof fetch = async (input, init) => {
    const url = String(input);
    calls.push({ url, cache: init?.cache });
    if (url.endsWith("latest.json")) return Response.json({ release: "2026-06" });
    if (url.endsWith("releases.json")) return Response.json({ releases: [{ release: "2026-06", label: "June 2026", available: true }] });
    return Response.json({ release: "2026-06", systems: [], files: [] });
  };
  const result = await loadBootstrap("/", { fetcher, delays: [] });
  assert.equal(result.latest.release, "2026-06");
  assert.equal(calls.find((call) => call.url.endsWith("latest.json"))?.cache, "no-store");
  assert.equal(calls.find((call) => call.url.includes("releases/2026-06/"))?.cache, "default");
});

test("发布期可通过release查询参数切换", () => {
  assert.equal(releaseFromSearch("?release=2026-03", ["2026-03", "2026-06"], "2026-06"), "2026-03");
  assert.equal(searchForRelease("?view=table", "2026-03"), "?view=table&release=2026-03");
});

test("页面状态可由同一查询参数恢复且默认回到第1页", () => {
  assert.equal(pageFromSearch("?slide=soybeans-production-conditions", PRESENTATION_SLIDES), 1);
  assert.equal(pageFromSearch("?slide=soybean-oil-balance", PRESENTATION_SLIDES), 2);
  assert.equal(pageFromSearch("?slide=soybean-meal-balance", PRESENTATION_SLIDES), 3);
  assert.equal(pageFromSearch("", PRESENTATION_SLIDES), 0);
  assert.equal(searchForSlide("?release=2026-06", "soybeans-balance"), "?release=2026-06&slide=soybeans-balance");
});

test("刷新初始化会重新读取latest", async () => {
  let latestCalls = 0;
  const fetcher: typeof fetch = async (input) => {
    const url = String(input);
    if (url.endsWith("latest.json")) { latestCalls += 1; return Response.json({ release: "2026-06" }); }
    if (url.endsWith("releases.json")) return Response.json({ releases: [] });
    return Response.json({ release: "2026-06", systems: [], files: [] });
  };
  await loadBootstrap("/", { fetcher, delays: [] });
  await loadBootstrap("/", { fetcher, delays: [] });
  assert.equal(latestCalls, 2);
});

test("第1页为Oil World大豆年度供需", () => {
  assert.equal(PRESENTATION_SLIDES[0].slideId, "soybeans-balance");
  assert.equal(PRESENTATION_SLIDES[0].slideType, "balance");
  assert.equal(PRESENTATION_SLIDES[0].title, "Oil World 大豆年度供需");
  assert.equal(PRESENTATION_SLIDES[0].shortTitle, "大豆供需");
  assert.equal(PRESENTATION_SLIDES[0].pageNumber, 1);
  assert.equal(PRESENTATION_SLIDES[0].layoutOrder, 0);
});

test("第2页为Oil World大豆生产条件", () => {
  assert.equal(PRESENTATION_SLIDES[1].slideId, "soybeans-production-conditions");
  assert.equal(PRESENTATION_SLIDES[1].slideType, "production-conditions");
  assert.equal(PRESENTATION_SLIDES[1].shortTitle, "大豆生产");
  assert.equal(PRESENTATION_SLIDES[1].pageNumber, 2);
  assert.equal(PRESENTATION_SLIDES[1].layoutOrder, 1);
  assert.deepEqual(PRESENTATION_SLIDES[1].metrics, ["Production", "Area Harvested", "Yield"]);
});

test("四页配置顺序和页码固定", () => {
  assert.deepEqual(PRESENTATION_SLIDES.map((slide) => ({
    slideId: slide.slideId,
    shortTitle: slide.shortTitle,
    pageNumber: slide.pageNumber,
    layoutOrder: slide.layoutOrder,
  })), [
    { slideId: "soybeans-balance", shortTitle: "大豆供需", pageNumber: 1, layoutOrder: 0 },
    { slideId: "soybeans-production-conditions", shortTitle: "大豆生产", pageNumber: 2, layoutOrder: 1 },
    { slideId: "soybean-oil-balance", shortTitle: "豆油供需", pageNumber: 3, layoutOrder: 2 },
    { slideId: "soybean-meal-balance", shortTitle: "豆粕供需", pageNumber: 4, layoutOrder: 3 },
  ]);
});

test("第3页和第4页使用共享供需模板及指定标题", () => {
  const expectedMetrics = [
    "Beginning Stocks",
    "Product Output",
    "Imports",
    "Exports",
    "Domestic Consumption",
    "Ending Stocks",
    PRESENTATION_STOCK_USAGE_RATIO,
  ];
  const oil = PRESENTATION_SLIDES[2];
  const meal = PRESENTATION_SLIDES[3];
  assert.equal(oil.slideType, "balance");
  assert.equal(oil.product, "Soybean Oil");
  assert.equal(oil.productLabel, "豆油");
  assert.equal(oil.title, "Oil World 豆油年度供需");
  assert.equal(oil.subtitle, "高密度季度研究演示 · Soybean Oil · 六地区");
  assert.deepEqual(oil.metrics, expectedMetrics);
  assert.equal(meal.slideType, "balance");
  assert.equal(meal.product, "Soybean Meal");
  assert.equal(meal.productLabel, "豆粕");
  assert.equal(meal.title, "Oil World 豆粕年度供需");
  assert.equal(meal.subtitle, "高密度季度研究演示 · Soybean Meal · 六地区");
  assert.deepEqual(meal.metrics, expectedMetrics);
});

test("豆油豆粕六地区明确选择marketing_year加balance", () => {
  for (const slide of PRESENTATION_SLIDES.slice(2)) {
    assert.deepEqual(slide.regions.map((region) => region.region), expectedRegions);
    assert.ok(slide.regions.every((region) => region.periodFamily === "marketing_year" && region.sourceRole === "balance"));
    for (const region of slide.regions) {
      const data = presentationProductCombination("2026-06", slide.product, region.region, region.periodFamily, region.sourceRole);
      const rows = metricsInOrder(data, slide.metrics, region.periodFamily, region.sourceRole);
      assert.equal(rows.length, 7);
      assert.ok(rows.every((metric) => metric.period_family === "marketing_year" && metric.source_role === "balance"));
    }
  }
});

test("Product Output仅在演示文案显示为产量且内部指标不变", () => {
  assert.equal(presentationMetricLabel("Product Output"), "产量");
  for (const product of ["Soybean Oil", "Soybean Meal"]) {
    const metric = productCombination("2026-06", product, "Global").metrics.find((item) => item.metric === "Product Output");
    assert.ok(metric);
    assert.equal(metric.metric, "Product Output");
  }
});

test("四个演示页均包含六个地区", () => {
  assert.equal(PRESENTATION_SLIDES.length, 4);
  assert.ok(PRESENTATION_SLIDES.every((slide) => slide.regions.length === 6));
});

test("六地区顺序固定", () => {
  for (const slide of PRESENTATION_SLIDES) {
    assert.deepEqual([...slide.regions].sort((a, b) => a.layoutOrder - b.layoutOrder).map((item) => item.region), expectedRegions);
  }
});

test("第1页普通地区沿用正式市场年度记录且不转换为自然年", () => {
  const global = PRESENTATION_SLIDES[0].regions[0];
  assert.equal(global.periodFamily, undefined);
  assert.equal(global.sourceRole, undefined);
  const data = combination("2026-06", "Global");
  assert.equal(data.metrics.find((metric) => metric.metric === "Imports")?.period_family, "marketing_year");
  assert.equal(data.metrics.some((metric) => metric.period_family === "calendar_year"), false);
});

test("Brazil第1页只使用calendar_year加balance", () => {
  const brazil = PRESENTATION_SLIDES[0].regions.find((item) => item.region === "Brazil")!;
  assert.equal(brazil.periodFamily, "calendar_year");
  assert.equal(brazil.sourceRole, "balance");
});

test("月份口径配置具有可追溯身份和来源", () => {
  assert.equal(PRESENTATION_PERIOD_BASIS.length, 24);
  for (const basis of PRESENTATION_PERIOD_BASIS) {
    assert.ok(["Soybeans", "Soybean Oil", "Soybean Meal"].includes(basis.product));
    assert.ok(basis.region);
    assert.ok(basis.period_family);
    assert.ok(basis.source_role);
    assert.ok(basis.display_label);
    assert.ok(basis.source_report_id.length > 0);
    assert.ok(basis.source_note);
  }
});

test("四页每张地区表均显示具体月份、未注明或混合口径", () => {
  for (const release of ["2026-03", "2026-06"]) {
    for (const slide of PRESENTATION_SLIDES) {
      for (const region of slide.regions) {
        const rows = metricsInOrder(combination(release, region.region), slide.metrics, region.periodFamily, region.sourceRole);
        const label = presentationPeriodBasis(slide, region, rows).resolved_label;
        assert.match(label, /(?:[A-Z][a-z]{2}–[A-Z][a-z]{2}|起止月原表未注明|混合口径)/);
        assert.notEqual(label, "Oil World作物年度");
      }
    }
  }
});

test("Brazil供需为Jan–Dec自然年且生产条件不借用该口径", () => {
  const balanceSlide = PRESENTATION_SLIDES[0];
  const productionSlide = PRESENTATION_SLIDES[1];
  const balanceRegion = balanceSlide.regions.find((item) => item.region === "Brazil")!;
  const productionRegion = productionSlide.regions.find((item) => item.region === "Brazil")!;
  const data = combination("2026-06", "Brazil");
  const balanceRows = metricsInOrder(data, balanceSlide.metrics, balanceRegion.periodFamily, balanceRegion.sourceRole);
  const productionRows = metricsInOrder(data, productionSlide.metrics, productionRegion.periodFamily, productionRegion.sourceRole);
  assert.equal(presentationPeriodBasis(balanceSlide, balanceRegion, balanceRows).resolved_label, "Jan–Dec｜自然年");
  assert.equal(presentationPeriodBasis(productionSlide, productionRegion, productionRows).resolved_label, "起止月原表未注明｜Oil World作物年度");
});

test("美国、阿根廷和中国供需优先读取正式period_basis", () => {
  const expected = new Map([
    ["United States", "Sep–Aug｜Oil World作物年度"],
    ["Argentina", "Apr–Mar｜Oil World作物年度"],
    ["China", "Sep–Aug｜Oil World作物年度"],
  ]);
  const slide = PRESENTATION_SLIDES[0];
  for (const [regionName, label] of expected) {
    const region = slide.regions.find((item) => item.region === regionName)!;
    const rows = metricsInOrder(combination("2026-06", regionName), slide.metrics, region.periodFamily, region.sourceRole);
    assert.equal(presentationPeriodBasis(slide, region, rows).resolved_label, label);
  }
});

test("Global和G3不伪装成单一月份口径", () => {
  const balanceSlide = PRESENTATION_SLIDES[0];
  const expected = new Map([
    ["Global", "混合口径｜供需 Sep–Aug · 贸易 Oct–Sep"],
    ["G3", "混合口径｜US Sep–Aug · BR Jan–Dec · AR Apr–Mar"],
  ]);
  for (const [regionName, expectedLabel] of expected) {
    const region = balanceSlide.regions.find((item) => item.region === regionName)!;
    const rows = metricsInOrder(combination("2026-06", regionName), balanceSlide.metrics, region.periodFamily, region.sourceRole);
    assert.equal(presentationPeriodBasis(balanceSlide, region, rows).resolved_label, expectedLabel);
  }
  const details = presentationPeriodFooterDetails(balanceSlide, balanceSlide.regions).join(" ");
  assert.match(details, /US Sep–Aug · BR Jan–Dec · AR Apr–Mar/);
  assert.match(details, /Global.*Sep–Aug.*Oct–Sep/);
});

test("生产表收获期不会被当作年度起止月", () => {
  const slide = PRESENTATION_SLIDES[1];
  const labels = slide.regions.map((region) => {
    const rows = metricsInOrder(combination("2026-06", region.region), slide.metrics, region.periodFamily, region.sourceRole);
    return presentationPeriodBasis(slide, region, rows).resolved_label;
  });
  assert.ok(labels.slice(0, 5).every((label) => label === "起止月原表未注明｜Oil World作物年度"));
  assert.equal(labels[5], "混合口径｜成员起止月原表未注明");
  assert.match(presentationPeriodFooterDetails(slide, slide.regions).join(" "), /收获期：US Sep–Nov · BR Jan–Mar · AR Apr–May/);
});

test("豆油豆粕六地区月份口径来自集中配置且与正式字段一致", () => {
  for (const release of ["2026-03", "2026-06"]) {
    for (const slide of PRESENTATION_SLIDES.slice(2)) {
      for (const region of slide.regions) {
        const data = presentationProductCombination(release, slide.product, region.region, region.periodFamily, region.sourceRole);
        const rows = metricsInOrder(data, slide.metrics, region.periodFamily, region.sourceRole);
        const basis = presentationPeriodBasis(slide, region, rows);
        assert.equal(basis.resolved_label, "Oct–Sep｜Oil World作物年度");
        assert.equal(basis.start_month, "Oct");
        assert.equal(basis.end_month, "Sep");
        assert.ok(rows.every((metric) => metric.period_basis === "Oct–Sept"));
      }
    }
  }
});

test("豆油豆粕March无网页计算季度修正且June读取上一发布期组成项", () => {
  for (const product of ["Soybean Oil", "Soybean Meal"]) {
    const march = presentationProductCombination("2026-03", product, "Global", "marketing_year", "balance");
    const june = presentationProductCombination("2026-06", product, "Global", "marketing_year", "balance");
    assert.equal(quarterRevisionDisplay(march.metrics.find((metric) => metric.metric === PRESENTATION_STOCK_USAGE_RATIO)!).text, "—");
    assert.equal(june.metrics.find((metric) => metric.metric === PRESENTATION_STOCK_USAGE_RATIO)?.quarter_revision?.unit, "percentage points");
  }
});

test("豆粕G3缺失和冲突指标保持—且不补0", () => {
  const g3 = presentationProductCombination("2026-06", "Soybean Meal", "G3", "marketing_year", "balance");
  for (const name of ["Imports", PRESENTATION_STOCK_USAGE_RATIO]) {
    const metric = g3.metrics.find((item) => item.metric === name)!;
    assert.ok(["missing", "conflict"].includes(metric.mapping_status));
    const period = metric.periods[0] ?? "2025/26";
    assert.equal(presentationCellValue(metric, period), "—");
    assert.notEqual(presentationCellValue(metric, period), "0");
  }
});

test("发布期切换保留第3页和第4页slide状态", () => {
  assert.equal(searchForRelease("?release=2026-06&slide=soybean-oil-balance", "2026-03"), "?release=2026-03&slide=soybean-oil-balance");
  assert.equal(searchForRelease("?release=2026-06&slide=soybean-meal-balance", "2026-03"), "?release=2026-03&slide=soybean-meal-balance");
});

test("顶部显示年度口径审计说明", () => {
  const header = fs.readFileSync(fileURLToPath(new URL("./presentation/components/PresentationHeader.tsx", import.meta.url)), "utf8");
  assert.match(header, /年度口径：各地区按原始Oil World报表口径，详见地区标题。/);
});

test("Brazil自然年表头是2026F、2025、2024", () => {
  const slide = PRESENTATION_SLIDES[0];
  const brazil = slide.regions.find((item) => item.region === "Brazil")!;
  const rows = metricsInOrder(combination("2026-06", "Brazil"), slide.metrics, brazil.periodFamily, brazil.sourceRole);
  assert.deepEqual(recentAvailablePeriods(rows).map((period, index) => periodHeaderLabel(period, index, true)), ["2026F", "2025", "2024"]);
});

test("Brazil供需表没有混入crop_year Production", () => {
  const rows = metricsInOrder(combination("2026-06", "Brazil"), ["Production"], "calendar_year", "balance");
  assert.equal(rows.length, 1);
  assert.equal(rows[0].period_family, "calendar_year");
  assert.equal(rows[0].source_role, "balance");
});

test("第2页Brazil使用crop_year加production_table", () => {
  const slide = PRESENTATION_SLIDES[1];
  const brazil = slide.regions.find((item) => item.region === "Brazil")!;
  const rows = metricsInOrder(combination("2026-06", "Brazil"), slide.metrics, brazil.periodFamily, brazil.sourceRole);
  assert.deepEqual(rows.map((metric) => metric.metric), ["Production", "Area Harvested", "Yield"]);
  assert.ok(rows.every((metric) => metric.period_family === "crop_year" && metric.source_role === "production_table"));
});

test("Brazil两条Production稳定键不同", () => {
  const rows = combination("2026-06", "Brazil").metrics.filter((metric) => metric.metric === "Production");
  assert.equal(new Set(rows.map(metricStableKey)).size, 2);
});

test("G3 conflict继续显示—", () => {
  const stocks = combination("2026-06", "G3").metrics.find((metric) => metric.metric === "Ending Stocks")!;
  assert.equal(stocks.mapping_status, "conflict");
  assert.equal(presentationCellValue(stocks, "2025/26"), "—");
});

test("1000 T只在页面换算为万吨", () => {
  assert.equal(displayValue(1234, "1000 T"), 123.4);
  assert.equal(displayUnit("1000 T"), "万吨");
});

test("Area Harvested单位为千公顷", () => {
  assert.equal(displayUnit("1000 ha"), "千公顷");
});

test("Yield单位为吨每公顷", () => {
  assert.equal(displayUnit("T/ha"), "吨/公顷");
});

test("网页计算库存使用比年度变化和季度修正使用百分点", () => {
  const data = presentationProductCombination("2026-06", "Soybeans", "Brazil", "calendar_year", "balance");
  const ratio = metricsInOrder(data, [PRESENTATION_STOCK_USAGE_RATIO], "calendar_year", "balance")[0];
  assert.equal(ratio.annual_change?.unit, "percentage points");
  assert.equal(ratio.quarter_revision?.unit, "percentage points");
  assert.match(quarterRevisionDisplay(ratio).text, /百分点$/);
});

test("缺失值显示—而不补0", () => {
  const conflict = combination("2026-06", "G3").metrics.find((metric) => metric.mapping_status === "conflict")!;
  assert.equal(presentationCellValue(conflict, conflict.periods[0] ?? "2026/27"), "—");
  assert.notEqual(presentationCellValue(conflict, conflict.periods[0] ?? "2026/27"), "0");
});

test("左右方向键翻页正常且不越界", () => {
  assert.equal(pageIndexForKey("ArrowRight", 0, 4), 1);
  assert.equal(pageIndexForKey("ArrowRight", 1, 4), 2);
  assert.equal(pageIndexForKey("ArrowRight", 2, 4), 3);
  assert.equal(pageIndexForKey("ArrowLeft", 3, 4), 2);
  assert.equal(pageIndexForKey("ArrowLeft", 1, 4), 0);
  assert.equal(pageIndexForKey("ArrowRight", 3, 4), 3);
});

test("全屏按钮和Fullscreen API存在", () => {
  const navigation = fs.readFileSync(fileURLToPath(new URL("./presentation/components/PresentationNavigation.tsx", import.meta.url)), "utf8");
  const app = fs.readFileSync(fileURLToPath(new URL("./presentation/PresentationApp.tsx", import.meta.url)), "utf8");
  assert.match(navigation, />全屏</);
  assert.match(app, /requestFullscreen/);
});

test("1920×1080画布无横向滚动", () => {
  const css = fs.readFileSync(fileURLToPath(new URL("./styles.css", import.meta.url)), "utf8");
  assert.match(css, /\.presentation-viewport\s*\{[\s\S]*?overflow:\s*hidden;/);
  assert.match(css, /\.presentation-table-grid\s*\{[\s\S]*?grid-template-columns:\s*repeat\(2/);
  assert.match(css, /grid-template-rows:\s*repeat\(3/);
});

test("1366×768等比例保持16比9", () => {
  const css = fs.readFileSync(fileURLToPath(new URL("./styles.css", import.meta.url)), "utf8");
  assert.match(css, /aspect-ratio:\s*16 \/ 9/);
  assert.match(css, /width:\s*min\(100vw, calc\(100dvh \* 16 \/ 9\)\)/);
});

test("详细看板路由不受影响", () => {
  const main = fs.readFileSync(fileURLToPath(new URL("./main.tsx", import.meta.url)), "utf8");
  assert.match(main, /isPresentationRoute\(window\.location\.pathname\) \? <PresentationApp \/> : <App \/>/);
});

test("第一版不再包含旧大指标卡和主趋势图组件", () => {
  const components = fileURLToPath(new URL("./presentation/components/", import.meta.url));
  const app = fs.readFileSync(fileURLToPath(new URL("./presentation/PresentationApp.tsx", import.meta.url)), "utf8");
  assert.equal(fs.existsSync(`${components}/OverviewMetricCard.tsx`), false);
  assert.equal(fs.existsSync(`${components}/PresentationTrendChart.tsx`), false);
  assert.doesNotMatch(app, /OverviewMetricCard|PresentationTrendChart|StandardBalanceSlide/);
});

test("2026-03作物年度列按正式发布期滚动", () => {
  const slide = PRESENTATION_SLIDES[1];
  const brazil = slide.regions.find((item) => item.region === "Brazil")!;
  const rows = metricsInOrder(combination("2026-03", "Brazil"), slide.metrics, brazil.periodFamily, brazil.sourceRole);
  assert.deepEqual(recentAvailablePeriods(rows), ["2025/26", "2024/25", "2023/24"]);
});

test("季度修正和年度变化均位于所有动态年度之前", () => {
  const header = regionTableSource.slice(regionTableSource.indexOf("<thead>"), regionTableSource.indexOf("</thead>"));
  const indicator = header.indexOf("<th>指标</th>");
  const revision = header.indexOf("季度修正");
  const annual = header.indexOf("年度变化");
  const periods = header.indexOf("periods.map");
  assert.ok(indicator < revision && revision < annual && annual < periods);
});

test("普通地区表列顺序为指标、季度修正、年度变化、市场年度", () => {
  const rows = metricsInOrder(combination("2026-06", "Global"), PRESENTATION_SLIDES[0].metrics);
  assert.deepEqual(["指标", "季度修正", "年度变化", ...recentAvailablePeriods(rows)], ["指标", "季度修正", "年度变化", "2026/27", "2025/26", "2024/25"]);
});

test("Brazil自然年表列顺序为指标、季度修正、年度变化、2026F、2025、2024", () => {
  const slide = PRESENTATION_SLIDES[0];
  const region = slide.regions.find((item) => item.region === "Brazil")!;
  const rows = metricsInOrder(combination("2026-06", "Brazil"), slide.metrics, region.periodFamily, region.sourceRole);
  const periods = recentAvailablePeriods(rows).map((period, index) => periodHeaderLabel(period, index, true));
  assert.deepEqual(["指标", "季度修正", "年度变化", ...periods], ["指标", "季度修正", "年度变化", "2026F", "2025", "2024"]);
});

test("生产条件表列顺序为变化列在前、作物年度在后", () => {
  const slide = PRESENTATION_SLIDES[1];
  const region = slide.regions.find((item) => item.region === "Brazil")!;
  const rows = metricsInOrder(combination("2026-06", "Brazil"), slide.metrics, region.periodFamily, region.sourceRole);
  assert.deepEqual(["指标", "季度修正", "年度变化", ...recentAvailablePeriods(rows)], ["指标", "季度修正", "年度变化", "2026/27", "2025/26", "2024/25"]);
});

test("2026-03与2026-06切换不改变变化列优先顺序", () => {
  for (const release of ["2026-03", "2026-06"]) {
    const rows = metricsInOrder(combination(release, "Global"), PRESENTATION_SLIDES[0].metrics);
    const headers = ["指标", "季度修正", "年度变化", ...recentAvailablePeriods(rows)];
    assert.deepEqual(headers.slice(0, 3), ["指标", "季度修正", "年度变化"]);
    assert.ok(headers.length > 3);
  }
});

test("年度列继续来自正式数据而非写死", () => {
  const march = recentAvailablePeriods(metricsInOrder(combination("2026-03", "Global"), PRESENTATION_SLIDES[0].metrics));
  const june = recentAvailablePeriods(metricsInOrder(combination("2026-06", "Global"), PRESENTATION_SLIDES[0].metrics));
  assert.notDeepEqual(march, june);
  assert.deepEqual(march, ["2025/26", "2024/25", "2023/24"]);
  assert.deepEqual(june, ["2026/27", "2025/26", "2024/25"]);
});

test("顶部同时显示配置驱动的年度供需和生产条件标签", () => {
  assert.match(slideTabsSource, /slide\.shortTitle/);
  assert.deepEqual(PRESENTATION_SLIDES.map((slide) => slide.shortTitle), ["大豆供需", "大豆生产", "豆油供需", "豆粕供需"]);
  assert.match(slideTabsSource, /onClick=\{\(\) => onPage\(index\)\}/);
  assert.match(slideTabsSource, /ordered\.length/);
});

test("顶部当前页具有深绿色激活样式", () => {
  const css = fs.readFileSync(fileURLToPath(new URL("./styles.css", import.meta.url)), "utf8");
  assert.match(slideTabsSource, /className=\{active \? "is-active" : ""\}/);
  assert.match(css, /\.presentation-slide-tabs button\.is-active\s*\{[\s\S]*?color:\s*#fff;[\s\S]*?background:\s*#164f3f;/);
});

test("底部导航使用文字按钮和键盘提示", () => {
  assert.match(navigationSource, /← 上一页/);
  assert.match(navigationSource, /下一页 →/);
  assert.match(navigationSource, /键盘 ← → 可翻页/);
  assert.match(navigationSource, /current\.pageNumber/);
  assert.match(navigationSource, /ordered\.length/);
  assert.deepEqual(PRESENTATION_SLIDES.map((slide) => `${slide.pageNumber} / ${PRESENTATION_SLIDES.length}`), ["1 / 4", "2 / 4", "3 / 4", "4 / 4"]);
});

test("顶部页面标签不随全屏工具栏隐藏", () => {
  const app = fs.readFileSync(fileURLToPath(new URL("./presentation/PresentationApp.tsx", import.meta.url)), "utf8");
  assert.match(app, /<PresentationHeader[\s\S]*?<PresentationNavigation/);
  assert.match(app, /visible=\{showControls\}/);
  assert.doesNotMatch(slideTabsSource, /controlsVisible|showControls/);
});

test("库存使用比中文名称统一且不保留旧名称", () => {
  const selectorSource = fs.readFileSync(fileURLToPath(new URL("./presentation/selectors.ts", import.meta.url)), "utf8");
  const headerSource = fs.readFileSync(fileURLToPath(new URL("./presentation/components/PresentationHeader.tsx", import.meta.url)), "utf8");
  assert.equal(presentationMetricLabel("Stocks/Use Ratio"), "库存/使用比");
  assert.equal(presentationMetricLabel(PRESENTATION_STOCK_USAGE_RATIO), "库存/使用比");
  assert.doesNotMatch(`${selectorSource}\n${headerSource}`, /库存\/消费比|库销比/);
});

test("网页计算标识与scope公式说明由共享组件显示", () => {
  const footerSource = fs.readFileSync(fileURLToPath(new URL("./presentation/components/SourceFooter.tsx", import.meta.url)), "utf8");
  assert.match(regionTableSource, /presentation-derived-tag/);
  assert.match(regionTableSource, /网页计算/);
  assert.match(footerSource, /ratioNote/);
  assert.match(PRESENTATION_STOCK_USAGE_FOOTER_NOTE, /Global＝期末库存÷国内消费/);
  assert.match(PRESENTATION_STOCK_USAGE_FOOTER_NOTE, /单个国家＝期末库存÷（国内消费＋出口）/);
  assert.match(PRESENTATION_STOCK_USAGE_FOOTER_NOTE, /G2\/G3因组内贸易无法安全剔除/);
});

test("2026-06豆油按Global、国家和聚合区scope计算", () => {
  const expected: Record<string, number | null> = {
    Global: 10.93,
    "United States": 6.44,
    Brazil: 3.99,
    Argentina: 3.50,
    China: 14.64,
    G3: null,
  };
  for (const region of expectedRegions) {
    const data = presentationProductCombination("2026-06", "Soybean Oil", region, "marketing_year", "balance");
    const ratio = presentationRatio(data);
    const latest = ratio.periods.find((period) => ratio.values[period] !== null);
    assert.equal(roundedPercent(latest ? ratio.values[latest] : null), expected[region]);
    assert.equal(latest ? presentationCellValue(ratio, latest) : "—", expected[region] === null ? "—" : expected[region]!.toFixed(2));
  }
});

test("2026-06豆粕恢复网页计算比率且G3保持空", () => {
  const expected: Record<string, number | null> = {
    Global: 3.46,
    "United States": 0.87,
    Brazil: 2.86,
    Argentina: 3.11,
    China: 2.50,
    G3: null,
  };
  for (const region of expectedRegions) {
    const ratio = presentationRatio(presentationProductCombination("2026-06", "Soybean Meal", region, "marketing_year", "balance"));
    const latest = ratio.periods.find((period) => ratio.values[period] !== null);
    assert.equal(roundedPercent(latest ? ratio.values[latest] : null), expected[region]);
  }
});

test("Global分母不含Exports而国家分母包含Exports", () => {
  const globalData = productCombination("2026-06", "Soybean Oil", "Global");
  const globalRatio = presentationRatio(applyPresentationStockUsageRatio(globalData, null, { periodFamily: "marketing_year", sourceRole: "balance" }));
  const globalPeriod = globalRatio.periods.find((period) => globalRatio.values[period] !== null)!;
  const globalEnding = globalData.metrics.find((metric) => metric.metric === "Ending Stocks")!;
  const globalConsumption = globalData.metrics.find((metric) => metric.metric === "Domestic Consumption")!;
  const globalExports = globalData.metrics.find((metric) => metric.metric === "Exports")!;
  assert.equal(globalRatio.values[globalPeriod], globalEnding.values[globalPeriod]! / globalConsumption.values[globalPeriod]! * 100);
  assert.notEqual(globalRatio.values[globalPeriod], globalEnding.values[globalPeriod]! / (globalConsumption.values[globalPeriod]! + globalExports.values[globalPeriod]!) * 100);

  const countryData = productCombination("2026-06", "Soybean Oil", "United States");
  const countryRatio = presentationRatio(applyPresentationStockUsageRatio(countryData, null, { periodFamily: "marketing_year", sourceRole: "balance" }));
  const countryPeriod = countryRatio.periods.find((period) => countryRatio.values[period] !== null)!;
  const countryEnding = countryData.metrics.find((metric) => metric.metric === "Ending Stocks")!;
  const countryConsumption = countryData.metrics.find((metric) => metric.metric === "Domestic Consumption")!;
  const countryExports = countryData.metrics.find((metric) => metric.metric === "Exports")!;
  assert.equal(countryRatio.values[countryPeriod], countryEnding.values[countryPeriod]! / (countryConsumption.values[countryPeriod]! + countryExports.values[countryPeriod]!) * 100);
});

test("网页计算严格要求同期间、时间轴、来源角色和有效组成项", () => {
  const mismatched = structuredClone(productCombination("2026-06", "Soybean Oil", "United States"));
  mismatched.metrics.find((metric) => metric.metric === "Exports")!.source_role = "production_table";
  const mismatchRatio = presentationRatio(applyPresentationStockUsageRatio(mismatched, null, { periodFamily: "marketing_year", sourceRole: "balance" }));
  assert.equal(mismatchRatio.mapping_status, "missing");
  assert.ok(Object.values(mismatchRatio.values).every((value) => value === null));

  const zero = structuredClone(productCombination("2026-06", "Soybean Oil", "United States"));
  const ending = zero.metrics.find((metric) => metric.metric === "Ending Stocks")!;
  const period = ending.periods[0];
  zero.metrics.find((metric) => metric.metric === "Domestic Consumption")!.values[period] = 0;
  zero.metrics.find((metric) => metric.metric === "Exports")!.values[period] = 0;
  const zeroRatio = presentationRatio(applyPresentationStockUsageRatio(zero, null, { periodFamily: "marketing_year", sourceRole: "balance" }));
  assert.equal(zeroRatio.values[period], null);

  const conflict = structuredClone(productCombination("2026-06", "Soybean Oil", "United States"));
  conflict.metrics.find((metric) => metric.metric === "Domestic Consumption")!.mapping_status = "conflict";
  const conflictRatio = presentationRatio(applyPresentationStockUsageRatio(conflict, null, { periodFamily: "marketing_year", sourceRole: "balance" }));
  assert.ok(Object.values(conflictRatio.values).every((value) => value === null));
});

test("网页计算保留独立键且不覆盖正式Stocks/Use Ratio", () => {
  const formal = productCombination("2026-06", "Soybean Oil", "Global");
  const formalRatio = formal.metrics.find((metric) => metric.metric === "Stocks/Use Ratio");
  const transformed = applyPresentationStockUsageRatio(formal, productCombination("2026-03", "Soybean Oil", "Global"), {
    periodFamily: "marketing_year",
    sourceRole: "balance",
  });
  assert.strictEqual(transformed.metrics.find((metric) => metric.metric === "Stocks/Use Ratio"), formalRatio);
  assert.equal(presentationRatio(transformed).metric, PRESENTATION_STOCK_USAGE_RATIO);
  assert.equal(metricStableKey(presentationRatio(transformed)), "presentation_stock_usage_ratio::marketing_year::balance");
});

test("年度变化和季度修正均由同一scope公式的未四舍五入值计算", () => {
  const march = presentationRatio(presentationProductCombination("2026-03", "Soybean Meal", "Argentina", "marketing_year", "balance"));
  const june = presentationRatio(presentationProductCombination("2026-06", "Soybean Meal", "Argentina", "marketing_year", "balance"));
  const numericPeriods = june.periods.filter((period) => june.values[period] !== null);
  assert.equal(june.annual_change?.value, june.values[numericPeriods[0]]! - june.values[numericPeriods[1]]!);
  assert.equal(june.annual_change?.unit, "percentage points");
  assert.equal(june.quarter_revision?.value, june.values[numericPeriods[0]]! - march.values[numericPeriods[0]]!);
  assert.equal(june.quarter_revision?.unit, "percentage points");
});

test("豆粕页恢复独立网页计算库存使用比行", () => {
  const slide = PRESENTATION_SLIDES.find((item) => item.slideId === "soybean-meal-balance")!;
  const slideSource = fs.readFileSync(fileURLToPath(new URL("./presentation/components/ResearchTableSlide.tsx", import.meta.url)), "utf8");
  assert.ok(slide.metrics.includes(PRESENTATION_STOCK_USAGE_RATIO));
  assert.doesNotMatch(slideSource, /hiddenWhenUnavailable|本页暂不展示/);
});

test("详细看板保留原结构且不接入演示层网页计算", () => {
  const appSource = fs.readFileSync(fileURLToPath(new URL("./App.tsx", import.meta.url)), "utf8");
  assert.match(appSource, /完整市场年度平衡表/);
  assert.doesNotMatch(appSource, /presentation_stock_usage_ratio|网页计算/);
});

test("临时数据路径只在开发服务器启用", () => {
  const viteSource = fs.readFileSync(fileURLToPath(new URL("../vite.config.ts", import.meta.url)), "utf8");
  assert.match(viteSource, /process\.env\.OIL_WORLD_PREVIEW_DATA_ROOT/);
  assert.match(viteSource, /apply:\s*"serve"/);
  assert.doesNotMatch(viteSource, /stocks_usage_revision_preview/);
});
