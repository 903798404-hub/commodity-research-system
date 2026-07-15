import { PRESENTATION_STOCK_USAGE_RATIO } from "./selectors";

export type PresentationSlideType = "balance" | "production-conditions";

export interface PresentationRegionConfig {
  region: string;
  label: string;
  layoutOrder: number;
  periodFamily?: string;
  sourceRole?: string;
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

function productBalanceRegions(): PresentationRegionConfig[] {
  return RESEARCH_REGIONS.map((item, layoutOrder) => ({
    ...item,
    layoutOrder,
    periodFamily: "marketing_year",
    sourceRole: "balance",
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
    regions: productBalanceRegions(),
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
