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
  groupId: string;
  groupTitle: string;
  groupOrder: number;
  groupPageNumber: number;
  system: string;
  product: string;
  productLabel: string;
  title: string;
  subtitle: string;
  metrics: string[];
  regions: PresentationRegionConfig[];
  layoutMode: "two-by-three" | "two-by-three-notes" | "two-by-four-notes";
  derivePresentationStockUsageRatio: boolean;
  euStockUsageRatio: "not-applicable" | "external-exports-confirmed" | "exports-scope-unconfirmed";
}

const SOYBEAN_SYSTEM_GROUP = {
  groupId: "soybean-system",
  groupTitle: "大豆体系",
  groupOrder: 1,
} as const;

const RAPESEED_SYSTEM_GROUP = {
  groupId: "rapeseed-system",
  groupTitle: "菜籽体系",
  groupOrder: 2,
} as const;

const SUNFLOWER_SYSTEM_GROUP = {
  groupId: "sunflower-system",
  groupTitle: "葵花体系",
  groupOrder: 3,
} as const;

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

const SUNFLOWER_REGIONS = [
  { region: "Global", label: "全球" },
  { region: "Russia", label: "俄罗斯" },
  { region: "Ukraine", label: "乌克兰" },
  { region: "European Union", label: "欧盟" },
  { region: "Argentina", label: "阿根廷" },
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

function sunflowerseedBalanceRegions(): PresentationRegionConfig[] {
  return SUNFLOWER_REGIONS.map((item, layoutOrder) => ({
    ...item,
    layoutOrder,
    ...(item.region === "Global"
      ? {}
      : item.region === "Argentina"
        ? { periodFamily: "calendar_year", sourceRole: "balance" }
        : { periodFamily: "marketing_year", sourceRole: "balance" }),
  }));
}

function sunflowerseedProductionRegions(): PresentationRegionConfig[] {
  return SUNFLOWER_REGIONS.map((item, layoutOrder) => ({
    ...item,
    layoutOrder,
    periodFamily: "crop_year",
    sourceRole: "production_table",
  }));
}

function sunflowerProductRegions(euConfirmed: boolean): PresentationRegionConfig[] {
  return SUNFLOWER_REGIONS.map((item, layoutOrder) => ({
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
    ...SOYBEAN_SYSTEM_GROUP,
    groupPageNumber: 1,
    system: "大豆体系",
    product: "Soybeans",
    productLabel: "大豆",
    title: "Oil World 大豆年度供需",
    subtitle: "高密度季度研究演示 · Soybeans · 六地区",
    metrics: BALANCE_METRICS,
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
    ...SOYBEAN_SYSTEM_GROUP,
    groupPageNumber: 2,
    system: "大豆体系",
    product: "Soybeans",
    productLabel: "大豆",
    title: "Oil World 大豆生产条件",
    subtitle: "高密度季度研究演示 · Soybeans · 六地区",
    metrics: PRODUCTION_METRICS,
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
    ...SOYBEAN_SYSTEM_GROUP,
    groupPageNumber: 3,
    system: "大豆体系",
    product: "Soybean Oil",
    productLabel: "豆油",
    title: "Oil World 豆油年度供需",
    subtitle: "高密度季度研究演示 · Soybean Oil · 六地区",
    metrics: PRODUCT_BALANCE_METRICS,
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
    ...SOYBEAN_SYSTEM_GROUP,
    groupPageNumber: 4,
    system: "大豆体系",
    product: "Soybean Meal",
    productLabel: "豆粕",
    title: "Oil World 豆粕年度供需",
    subtitle: "高密度季度研究演示 · Soybean Meal · 六地区",
    metrics: PRODUCT_BALANCE_METRICS,
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
    ...RAPESEED_SYSTEM_GROUP,
    groupPageNumber: 1,
    system: "菜籽体系",
    product: "Rapeseed / Canola",
    productLabel: "菜籽",
    title: "Oil World 菜籽年度供需",
    subtitle: "高密度季度研究演示 · Rapeseed / Canola · 七地区",
    metrics: RAPESEED_BALANCE_METRICS,
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
    ...RAPESEED_SYSTEM_GROUP,
    groupPageNumber: 2,
    system: "菜籽体系",
    product: "Rapeseed / Canola",
    productLabel: "菜籽",
    title: "Oil World 菜籽生产条件",
    subtitle: "高密度季度研究演示 · Rapeseed / Canola · 七地区",
    metrics: PRODUCTION_METRICS,
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
    ...RAPESEED_SYSTEM_GROUP,
    groupPageNumber: 3,
    system: "菜籽体系",
    product: "Rapeseed Oil",
    productLabel: "菜油",
    title: "Oil World 菜油年度供需",
    subtitle: "高密度季度研究演示 · Rapeseed Oil · 七地区",
    metrics: PRODUCT_BALANCE_METRICS,
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
    ...RAPESEED_SYSTEM_GROUP,
    groupPageNumber: 4,
    system: "菜籽体系",
    product: "Rapeseed Meal",
    productLabel: "菜粕",
    title: "Oil World 菜粕年度供需",
    subtitle: "高密度季度研究演示 · Rapeseed Meal · 七地区",
    metrics: PRODUCT_BALANCE_METRICS,
    layoutMode: "two-by-four-notes",
    derivePresentationStockUsageRatio: true,
    euStockUsageRatio: "exports-scope-unconfirmed",
    regions: rapeseedProductRegions(false),
  },
  {
    slideId: "sunflowerseed-balance",
    slideType: "balance",
    shortTitle: "葵花籽供需",
    pageNumber: 9,
    layoutOrder: 8,
    ...SUNFLOWER_SYSTEM_GROUP,
    groupPageNumber: 1,
    system: "葵花体系",
    product: "Sunflowerseed",
    productLabel: "葵花籽",
    title: "Oil World 葵花籽年度供需",
    subtitle: "高密度季度研究演示 · Sunflowerseed · 五地区",
    metrics: RAPESEED_BALANCE_METRICS,
    layoutMode: "two-by-three-notes",
    derivePresentationStockUsageRatio: false,
    euStockUsageRatio: "not-applicable",
    regions: sunflowerseedBalanceRegions(),
  },
  {
    slideId: "sunflowerseed-production-conditions",
    slideType: "production-conditions",
    shortTitle: "葵花籽生产",
    pageNumber: 10,
    layoutOrder: 9,
    ...SUNFLOWER_SYSTEM_GROUP,
    groupPageNumber: 2,
    system: "葵花体系",
    product: "Sunflowerseed",
    productLabel: "葵花籽",
    title: "Oil World 葵花籽生产条件",
    subtitle: "高密度季度研究演示 · Sunflowerseed · 五地区",
    metrics: PRODUCTION_METRICS,
    layoutMode: "two-by-three-notes",
    derivePresentationStockUsageRatio: false,
    euStockUsageRatio: "not-applicable",
    regions: sunflowerseedProductionRegions(),
  },
  {
    slideId: "sunflower-oil-balance",
    slideType: "balance",
    shortTitle: "葵油供需",
    pageNumber: 11,
    layoutOrder: 10,
    ...SUNFLOWER_SYSTEM_GROUP,
    groupPageNumber: 3,
    system: "葵花体系",
    product: "Sunflower Oil",
    productLabel: "葵油",
    title: "Oil World 葵油年度供需",
    subtitle: "高密度季度研究演示 · Sunflower Oil · 五地区",
    metrics: PRODUCT_BALANCE_METRICS,
    layoutMode: "two-by-three-notes",
    derivePresentationStockUsageRatio: true,
    euStockUsageRatio: "external-exports-confirmed",
    regions: sunflowerProductRegions(true),
  },
  {
    slideId: "sunflower-meal-balance",
    slideType: "balance",
    shortTitle: "葵粕供需",
    pageNumber: 12,
    layoutOrder: 11,
    ...SUNFLOWER_SYSTEM_GROUP,
    groupPageNumber: 4,
    system: "葵花体系",
    product: "Sunflower Meal",
    productLabel: "葵粕",
    title: "Oil World 葵粕年度供需",
    subtitle: "高密度季度研究演示 · Sunflower Meal · 五地区",
    metrics: PRODUCT_BALANCE_METRICS,
    layoutMode: "two-by-three-notes",
    derivePresentationStockUsageRatio: true,
    euStockUsageRatio: "exports-scope-unconfirmed",
    regions: sunflowerProductRegions(false),
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
