import { PRESENTATION_STOCK_USAGE_RATIO } from "./selectors";

export type PresentationSlideType = "balance" | "production-conditions";

export interface PresentationRegionConfig {
  region: string;
  label: string;
  layoutOrder: number;
  periodFamily?: string;
  sourceRole?: string;
  stockUsageScope?: "global" | "country" | "external_region" | "aggregate";
}

export interface PresentationSlideConfig {
  slideId: string;
  slideType: PresentationSlideType;
  shortTitle: string;
  pageNumber: number;
  layoutOrder: number;
  system: string;
  product: string;
  productLabel: string;
  title: string;
  subtitle: string;
  metrics: string[];
  regions: PresentationRegionConfig[];
  navigationGroup: string;
  layoutMode: "two-by-three" | "two-by-four-notes";
  derivePresentationStockUsageRatio: boolean;
  euStockUsageRatio: "not-applicable" | "external-exports-confirmed" | "exports-scope-unconfirmed";
}

const RESEARCH_REGIONS = [
  { region: "Global", label: "全球" },
  { region: "United States", label: "美国" },
  { region: "Brazil", label: "巴西" },
  { region: "Argentina", label: "阿根廷" },
  { region: "China", label: "中国" },
  { region: "G3", label: "G3" },
] as const;

const BALANCE_METRICS = [
  "Beginning Stocks",
  "Production",
  "Imports",
  "Exports",
  "Crush",
  "Domestic Consumption",
  "Ending Stocks",
  PRESENTATION_STOCK_USAGE_RATIO,
];

const PRODUCTION_METRICS = ["Production", "Area Harvested", "Yield"];

const PRODUCT_BALANCE_METRICS = [
  "Beginning Stocks",
  "Product Output",
  "Imports",
  "Exports",
  "Domestic Consumption",
  "Ending Stocks",
  PRESENTATION_STOCK_USAGE_RATIO,
];

const RAPESEED_BALANCE_METRICS = [
  "Beginning Stocks",
  "Production",
  "Imports",
  "Exports",
  "Crush",
  "Domestic Consumption",
  "Ending Stocks",
  "Stocks/Use Ratio",
];

const RAPESEED_REGIONS = [
  { region: "Global", label: "全球" },
  { region: "Canada", label: "加拿大" },
  { region: "European Union", label: "欧盟" },
  { region: "China", label: "中国" },
  { region: "Australia", label: "澳大利亚" },
  { region: "Russia", label: "俄罗斯" },
  { region: "Ukraine", label: "乌克兰" },
] as const;

function productBalanceRegions(): PresentationRegionConfig[] {
  return RESEARCH_REGIONS.map((item, layoutOrder) => ({
    ...item,
    layoutOrder,
    periodFamily: "marketing_year",
    sourceRole: "balance",
    stockUsageScope: item.region === "Global"
      ? "global"
      : item.region === "G3" ? "aggregate" : "country",
  }));
}

function rapeseedBalanceRegions(): PresentationRegionConfig[] {
  return RAPESEED_REGIONS.map((item, layoutOrder) => ({
    ...item,
    layoutOrder,
    ...(item.region === "Global" ? {} : { periodFamily: "marketing_year", sourceRole: "balance" }),
  }));
}

function rapeseedProductionRegions(): PresentationRegionConfig[] {
  return RAPESEED_REGIONS.map((item, layoutOrder) => ({
    ...item,
    layoutOrder,
    periodFamily: "crop_year",
    sourceRole: "production_table",
  }));
}

function rapeseedProductRegions(euConfirmed: boolean): PresentationRegionConfig[] {
  return RAPESEED_REGIONS.map((item, layoutOrder) => ({
    ...item,
    layoutOrder,
    periodFamily: "marketing_year",
    sourceRole: "balance",
    stockUsageScope: item.region === "Global"
      ? "global"
      : item.region === "European Union"
        ? euConfirmed ? "external_region" : "aggregate"
        : "country",
  }));
}

export const PRESENTATION_SLIDES: readonly PresentationSlideConfig[] = [
  {
    slideId: "soybeans-balance",
    slideType: "balance",
    shortTitle: "大豆供需",
    pageNumber: 1,
    layoutOrder: 0,
    system: "大豆体系",
    product: "Soybeans",
    productLabel: "大豆",
    title: "Oil World 大豆年度供需",
    subtitle: "高密度季度研究演示 · Soybeans · 六地区",
    metrics: BALANCE_METRICS,
    navigationGroup: "大豆体系",
    layoutMode: "two-by-three",
    derivePresentationStockUsageRatio: true,
    euStockUsageRatio: "not-applicable",
    regions: RESEARCH_REGIONS.map((item, layoutOrder) => item.region === "Brazil"
      ? {
          ...item,
          layoutOrder,
          periodFamily: "calendar_year",
          sourceRole: "balance",
        }
      : {
          ...item,
          layoutOrder,
        }),
  },
  {
    slideId: "soybeans-production-conditions",
    slideType: "production-conditions",
    shortTitle: "大豆生产",
    pageNumber: 2,
    layoutOrder: 1,
    system: "大豆体系",
    product: "Soybeans",
    productLabel: "大豆",
    title: "Oil World 大豆生产条件",
    subtitle: "高密度季度研究演示 · Soybeans · 六地区",
    metrics: PRODUCTION_METRICS,
    navigationGroup: "大豆体系",
    layoutMode: "two-by-three",
    derivePresentationStockUsageRatio: false,
    euStockUsageRatio: "not-applicable",
    regions: RESEARCH_REGIONS.map((item, layoutOrder) => ({
      ...item,
      layoutOrder,
      periodFamily: "crop_year",
      sourceRole: "production_table",
    })),
  },
  {
    slideId: "soybean-oil-balance",
    slideType: "balance",
    shortTitle: "豆油供需",
    pageNumber: 3,
    layoutOrder: 2,
    system: "大豆体系",
    product: "Soybean Oil",
    productLabel: "豆油",
    title: "Oil World 豆油年度供需",
    subtitle: "高密度季度研究演示 · Soybean Oil · 六地区",
    metrics: PRODUCT_BALANCE_METRICS,
    navigationGroup: "大豆体系",
    layoutMode: "two-by-three",
    derivePresentationStockUsageRatio: true,
    euStockUsageRatio: "not-applicable",
    regions: productBalanceRegions(),
  },
  {
    slideId: "soybean-meal-balance",
    slideType: "balance",
    shortTitle: "豆粕供需",
    pageNumber: 4,
    layoutOrder: 3,
    system: "大豆体系",
    product: "Soybean Meal",
    productLabel: "豆粕",
    title: "Oil World 豆粕年度供需",
    subtitle: "高密度季度研究演示 · Soybean Meal · 六地区",
    metrics: PRODUCT_BALANCE_METRICS,
    navigationGroup: "大豆体系",
    layoutMode: "two-by-three",
    derivePresentationStockUsageRatio: true,
    euStockUsageRatio: "not-applicable",
    regions: productBalanceRegions(),
  },
  {
    slideId: "rapeseed-canola-balance",
    slideType: "balance",
    shortTitle: "菜籽供需",
    pageNumber: 5,
    layoutOrder: 4,
    system: "菜籽体系",
    product: "Rapeseed / Canola",
    productLabel: "菜籽",
    title: "Oil World 菜籽年度供需",
    subtitle: "高密度季度研究演示 · Rapeseed / Canola · 七地区",
    metrics: RAPESEED_BALANCE_METRICS,
    navigationGroup: "菜籽体系",
    layoutMode: "two-by-four-notes",
    derivePresentationStockUsageRatio: false,
    euStockUsageRatio: "not-applicable",
    regions: rapeseedBalanceRegions(),
  },
  {
    slideId: "rapeseed-canola-production-conditions",
    slideType: "production-conditions",
    shortTitle: "菜籽生产",
    pageNumber: 6,
    layoutOrder: 5,
    system: "菜籽体系",
    product: "Rapeseed / Canola",
    productLabel: "菜籽",
    title: "Oil World 菜籽生产条件",
    subtitle: "高密度季度研究演示 · Rapeseed / Canola · 七地区",
    metrics: PRODUCTION_METRICS,
    navigationGroup: "菜籽体系",
    layoutMode: "two-by-four-notes",
    derivePresentationStockUsageRatio: false,
    euStockUsageRatio: "not-applicable",
    regions: rapeseedProductionRegions(),
  },
  {
    slideId: "rapeseed-oil-balance",
    slideType: "balance",
    shortTitle: "菜油供需",
    pageNumber: 7,
    layoutOrder: 6,
    system: "菜籽体系",
    product: "Rapeseed Oil",
    productLabel: "菜油",
    title: "Oil World 菜油年度供需",
    subtitle: "高密度季度研究演示 · Rapeseed Oil · 七地区",
    metrics: PRODUCT_BALANCE_METRICS,
    navigationGroup: "菜籽体系",
    layoutMode: "two-by-four-notes",
    derivePresentationStockUsageRatio: true,
    euStockUsageRatio: "external-exports-confirmed",
    regions: rapeseedProductRegions(true),
  },
  {
    slideId: "rapeseed-meal-balance",
    slideType: "balance",
    shortTitle: "菜粕供需",
    pageNumber: 8,
    layoutOrder: 7,
    system: "菜籽体系",
    product: "Rapeseed Meal",
    productLabel: "菜粕",
    title: "Oil World 菜粕年度供需",
    subtitle: "高密度季度研究演示 · Rapeseed Meal · 七地区",
    metrics: PRODUCT_BALANCE_METRICS,
    navigationGroup: "菜籽体系",
    layoutMode: "two-by-four-notes",
    derivePresentationStockUsageRatio: true,
    euStockUsageRatio: "exports-scope-unconfirmed",
    regions: rapeseedProductRegions(false),
  },
] as const;

export function isPresentationRoute(pathname: string): boolean {
  const normalized = pathname.replace(/\/+$/, "") || "/";
  return normalized === "/presentation" || normalized.endsWith("/presentation");
}

export function releaseFromSearch(
  search: string,
  availableReleases: readonly string[],
  latestRelease: string,
): string {
  const requested = new URLSearchParams(search).get("release");
  return requested && availableReleases.includes(requested) ? requested : latestRelease;
}

export function searchForRelease(search: string, release: string): string {
  const params = new URLSearchParams(search);
  params.set("release", release);
  return `?${params.toString()}`;
}

export function pageFromSearch(search: string, slides: readonly PresentationSlideConfig[]): number {
  const requested = new URLSearchParams(search).get("slide");
  const index = slides.findIndex((slide) => slide.slideId === requested);
  return index >= 0 ? index : 0;
}

export function searchForSlide(search: string, slideId: string): string {
  const params = new URLSearchParams(search);
  params.set("slide", slideId);
  return `?${params.toString()}`;
}

export function pageIndexForKey(key: string, current: number, pageCount: number): number {
  if (key === "ArrowRight" || key === "PageDown") return Math.min(current + 1, pageCount - 1);
  if (key === "ArrowLeft" || key === "PageUp") return Math.max(current - 1, 0);
  return current;
}
