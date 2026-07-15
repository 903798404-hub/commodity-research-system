import type { CombinationData, MetricData, ReleaseIndex } from "./model";

export interface Selection {
  system: string;
  product: string;
  region: string;
}

export function validSelection(index: ReleaseIndex, requested?: Partial<Selection>): Selection {
  const system = index.systems.find((item) => item.label === requested?.system) ?? index.systems[0];
  if (!system) throw new Error("发布索引没有品种体系");
  const product = system.products.find((item) => item.label === requested?.product) ?? system.products[0];
  if (!product) throw new Error(`品种体系${system.label}没有产品`);
  const region = product.regions.includes(requested?.region ?? "") ? requested!.region! : product.regions[0];
  if (!region) throw new Error(`${product.label}没有国家或地区`);
  return { system: system.label, product: product.label, region };
}

export function productsFor(index: ReleaseIndex, systemLabel: string) {
  return index.systems.find((item) => item.label === systemLabel)?.products ?? [];
}

export function regionsFor(index: ReleaseIndex, systemLabel: string, productLabel: string) {
  return productsFor(index, systemLabel).find((item) => item.label === productLabel)?.regions ?? [];
}

export function fileFor(index: ReleaseIndex, selection: Selection) {
  return index.files.find(
    (item) =>
      item.system === selection.system && item.product === selection.product && item.region === selection.region,
  );
}

export function visibleMetrics(data: CombinationData): MetricData[] {
  return data.metrics.filter((metric) => metric.mapping_status !== "not_applicable");
}

export function metricStableKey(metric: Pick<MetricData, "metric" | "period_family" | "source_role">): string {
  return [metric.metric, metric.period_family ?? "legacy", metric.source_role ?? "legacy"].join("::");
}

export function hasDualTimeAxes(data: CombinationData): boolean {
  const families = new Set(data.metrics.map((metric) => metric.period_family));
  const roles = new Set(data.metrics.map((metric) => metric.source_role));
  return families.has("calendar_year")
    && families.has("crop_year")
    && roles.has("balance")
    && roles.has("production_table");
}

const CALENDAR_BALANCE_ORDER = [
  "Beginning Stocks",
  "Production",
  "Imports",
  "Exports",
  "Crush",
  "Domestic Consumption",
  "Ending Stocks",
  "Stocks/Use Ratio",
];

const CROP_PRODUCTION_ORDER = ["Production", "Area Harvested", "Yield"];

function orderedAxisMetrics(
  data: CombinationData,
  periodFamily: string,
  sourceRole: string,
  order: readonly string[],
): MetricData[] {
  const metrics = visibleMetrics(data).filter(
    (metric) => metric.period_family === periodFamily && metric.source_role === sourceRole,
  );
  return [...metrics].sort((left, right) => {
    const leftOrder = order.indexOf(left.metric);
    const rightOrder = order.indexOf(right.metric);
    return (leftOrder < 0 ? order.length : leftOrder) - (rightOrder < 0 ? order.length : rightOrder);
  });
}

export function calendarBalanceMetrics(data: CombinationData): MetricData[] {
  return orderedAxisMetrics(data, "calendar_year", "balance", CALENDAR_BALANCE_ORDER);
}

export function cropProductionMetrics(data: CombinationData): MetricData[] {
  return orderedAxisMetrics(data, "crop_year", "production_table", CROP_PRODUCTION_ORDER);
}

export function periodsForMetrics(metrics: readonly MetricData[]): string[] {
  const periods: string[] = [];
  for (const metric of metrics) {
    for (const period of metric.periods) {
      if (!periods.includes(period)) periods.push(period);
    }
  }
  return periods;
}

export function periodHeaderLabel(period: string, index: number, calendarAxis = false): string {
  return calendarAxis && index === 0 ? `${period}F` : period;
}

export function metricDisplayLabel(metric: MetricData, dualTimeAxes = false): string {
  if (!dualTimeAxes || metric.metric !== "Production") return metric.metric;
  if (metric.period_family === "calendar_year" && metric.source_role === "balance") return "Production（自然年）";
  if (metric.period_family === "crop_year" && metric.source_role === "production_table") return "Production（作物年度）";
  return metric.metric;
}

export function axisLabel(metric: MetricData): string {
  if (metric.period_family === "calendar_year") return "自然年（Jan–Dec）";
  if (metric.period_family === "crop_year") return "Oil World作物年度";
  return "市场年度";
}

export function displayUnit(unit: string): string {
  if (unit === "1000 T") return "万吨";
  if (unit === "1000 ha") return "千公顷";
  if (unit === "T/ha") return "吨/公顷";
  if (unit === "%") return "%";
  if (unit === "percentage points") return "百分点";
  throw new Error(`未知单位：${unit}`);
}

export function displayValue(value: number | null | undefined, unit: string): number | null {
  if (value === null || value === undefined) return null;
  if (!Number.isFinite(value)) throw new Error("数据值不是数字或空值");
  return unit === "1000 T" ? value / 10 : value;
}

export function formatNumber(value: number | null, unit: string): string {
  if (value === null) return "—";
  const digits = unit === "%" || unit === "percentage points" || unit === "T/ha" ? 2 : 1;
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: digits }).format(value);
}

export function formatSignedChange(value: number | null, unit: string): string {
  if (value === null) return "—";
  const magnitude = formatNumber(Math.abs(value), unit);
  if (value > 0) return `+${magnitude}`;
  if (value < 0) return `−${magnitude}`;
  return magnitude;
}

export function quarterRevisionDisplay(metric: Pick<MetricData, "quarter_revision">): {
  text: string;
  hasValue: boolean;
} {
  const revision = metric.quarter_revision;
  if (!revision) return { text: "—", hasValue: false };
  const value = displayValue(revision.value, revision.unit);
  return {
    text: `${formatSignedChange(value, revision.unit)} ${displayUnit(revision.unit)}`,
    hasValue: true,
  };
}

export function marketYearBasisLabel(basis: readonly string[] | null | undefined): string {
  const labels = [...new Set((basis ?? []).map((item) => item.trim()).filter(Boolean))];
  return `年度口径：${labels.length ? labels.join("｜") : "—"}`;
}

export function cardMetrics(data: CombinationData): MetricData[] {
  const isUpstream = ["Soybeans", "Rapeseed / Canola", "Sunflowerseed"].includes(data.product);
  const priority = isUpstream
    ? ["Production", "Exports", "Crush", "Ending Stocks", "Stocks/Use Ratio", "Domestic Consumption"]
    : ["Product Output", "Exports", "Domestic Consumption", "Ending Stocks", "Stocks/Use Ratio"];
  const candidates = hasDualTimeAxes(data) ? calendarBalanceMetrics(data) : data.metrics;
  return priority
    .map((name) => candidates.find((metric) => metric.metric === name))
    .filter((metric): metric is MetricData =>
      Boolean(metric && ["direct", "derived"].includes(metric.mapping_status) && metric.periods.some((p) => metric.values[p] !== null)),
    )
    .slice(0, 5);
}
