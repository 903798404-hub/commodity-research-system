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

export function cardMetrics(data: CombinationData): MetricData[] {
  const isUpstream = ["Soybeans", "Rapeseed / Canola", "Sunflowerseed"].includes(data.product);
  const priority = isUpstream
    ? ["Production", "Exports", "Crush", "Ending Stocks", "Stocks/Use Ratio", "Domestic Consumption"]
    : ["Product Output", "Exports", "Domestic Consumption", "Ending Stocks", "Stocks/Use Ratio"];
  const metrics = new Map(data.metrics.map((metric) => [metric.metric, metric]));
  return priority
    .map((name) => metrics.get(name))
    .filter((metric): metric is MetricData =>
      Boolean(metric && ["direct", "derived"].includes(metric.mapping_status) && metric.periods.some((p) => metric.values[p] !== null)),
    )
    .slice(0, 5);
}
