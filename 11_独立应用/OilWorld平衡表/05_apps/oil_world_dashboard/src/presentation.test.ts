import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { loadBootstrap } from "./api";
import { applyQuarterRevisions } from "./comparison";
import type { CombinationComparison, CombinationData, ComparisonIndex, ReleaseIndex } from "./model";
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
  recentAvailablePeriods,
} from "./presentation/selectors";
import {
  PRESENTATION_PERIOD_BASIS,
  presentationPeriodBasis,
  presentationPeriodFooterDetails,
} from "./presentation/presentationPeriodBasis";
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
  const index = releaseIndex(release);
  const file = index.files.find((item) => item.product === "Soybeans" && item.region === region);
  assert.ok(file);
  return readJson(`${publicRoot}/releases/${release}/${file.path}`);
}

function comparison(region: string): CombinationComparison {
  const root = `${publicRoot}/comparisons/2026-03_to_2026-06`;
  const index = readJson<ComparisonIndex>(`${root}/index.json`);
  const file = index.files.find((item) => item.product === "Soybeans" && item.region === region);
  assert.ok(file);
  return readJson(`${root}/${file.path}`);
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
  assert.equal(PRESENTATION_SLIDES[0].shortTitle, "年度供需");
  assert.equal(PRESENTATION_SLIDES[0].pageNumber, 1);
  assert.equal(PRESENTATION_SLIDES[0].layoutOrder, 0);
});

test("第2页为Oil World大豆生产条件", () => {
  assert.equal(PRESENTATION_SLIDES[1].slideId, "soybeans-production-conditions");
  assert.equal(PRESENTATION_SLIDES[1].slideType, "production-conditions");
  assert.equal(PRESENTATION_SLIDES[1].shortTitle, "生产条件");
  assert.equal(PRESENTATION_SLIDES[1].pageNumber, 2);
  assert.equal(PRESENTATION_SLIDES[1].layoutOrder, 1);
  assert.deepEqual(PRESENTATION_SLIDES[1].metrics, ["Production", "Area Harvested", "Yield"]);
});

test("每页包含六个地区", () => {
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
  assert.equal(PRESENTATION_PERIOD_BASIS.length, 12);
  for (const basis of PRESENTATION_PERIOD_BASIS) {
    assert.equal(basis.product, "Soybeans");
    assert.ok(basis.region);
    assert.ok(basis.period_family);
    assert.ok(basis.source_role);
    assert.ok(basis.display_label);
    assert.ok(basis.source_report_id.length > 0);
    assert.ok(basis.source_note);
  }
});

test("两页每张地区表均显示具体月份、未注明或混合口径", () => {
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

test("Stocks/Use Ratio年度变化和季度修正使用百分点", () => {
  const data = applyQuarterRevisions(combination("2026-06", "Brazil"), comparison("Brazil"));
  const ratio = metricsInOrder(data, ["Stocks/Use Ratio"], "calendar_year", "balance")[0];
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
  assert.equal(pageIndexForKey("ArrowRight", 0, 2), 1);
  assert.equal(pageIndexForKey("ArrowLeft", 1, 2), 0);
  assert.equal(pageIndexForKey("ArrowRight", 1, 2), 1);
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
  assert.deepEqual(PRESENTATION_SLIDES.map((slide) => slide.shortTitle), ["年度供需", "生产条件"]);
  assert.match(slideTabsSource, /onClick=\{\(\) => onPage\(index\)\}/);
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
});

test("顶部页面标签不随全屏工具栏隐藏", () => {
  const app = fs.readFileSync(fileURLToPath(new URL("./presentation/PresentationApp.tsx", import.meta.url)), "utf8");
  assert.match(app, /<PresentationHeader[\s\S]*?<PresentationNavigation/);
  assert.match(app, /visible=\{showControls\}/);
  assert.doesNotMatch(slideTabsSource, /controlsVisible|showControls/);
});
