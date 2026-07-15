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
  title: string;
  metrics: string[];
  regions: PresentationRegionConfig[];
}

const SOYBEAN_REGIONS = [
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
  "Stocks/Use Ratio",
];

const PRODUCTION_METRICS = ["Production", "Area Harvested", "Yield"];

export const PRESENTATION_SLIDES: readonly PresentationSlideConfig[] = [
  {
    slideId: "soybeans-balance",
    slideType: "balance",
    shortTitle: "年度供需",
    pageNumber: 1,
    layoutOrder: 0,
    system: "大豆体系",
    product: "Soybeans",
    title: "Oil World 大豆年度供需",
    metrics: BALANCE_METRICS,
    regions: SOYBEAN_REGIONS.map((item, layoutOrder) => item.region === "Brazil"
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
    shortTitle: "生产条件",
    pageNumber: 2,
    layoutOrder: 1,
    system: "大豆体系",
    product: "Soybeans",
    title: "Oil World 大豆生产条件",
    metrics: PRODUCTION_METRICS,
    regions: SOYBEAN_REGIONS.map((item, layoutOrder) => ({
      ...item,
      layoutOrder,
      periodFamily: "crop_year",
      sourceRole: "production_table",
    })),
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
