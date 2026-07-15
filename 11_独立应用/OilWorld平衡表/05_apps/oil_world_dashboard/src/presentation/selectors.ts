import type { CombinationData, MetricData } from "../model";
import { displayValue, formatNumber } from "../selectors";

export interface MetricIdentity {
  metric: string;
  periodFamily?: string;
  sourceRole?: string;
}

export function metricByIdentity(data: CombinationData, identity: MetricIdentity): MetricData | undefined {
  return data.metrics.find((metric) =>
    metric.metric === identity.metric
    && (!identity.periodFamily || metric.period_family === identity.periodFamily)
    && (!identity.sourceRole || metric.source_role === identity.sourceRole),
  );
}

export function metricsInOrder(
  data: CombinationData,
  names: readonly string[],
  periodFamily?: string,
  sourceRole?: string,
): MetricData[] {
  return names
    .map((metric) => metricByIdentity(data, { metric, periodFamily, sourceRole }))
    .filter((metric): metric is MetricData => Boolean(metric));
}

export function recentAvailablePeriods(metrics: readonly MetricData[], limit = 3): string[] {
  const periods: string[] = [];
  for (const metric of metrics) {
    for (const period of metric.periods) {
      if (metric.values[period] !== null && !periods.includes(period)) periods.push(period);
    }
  }
  return periods.slice(0, limit);
}

export function presentationCellValue(metric: MetricData | undefined, period: string): string {
  if (!metric || !["direct", "derived"].includes(metric.mapping_status)) return "—";
  return formatNumber(displayValue(metric.values[period], metric.unit), metric.unit);
}

const METRIC_LABELS: Record<string, string> = {
  "Beginning Stocks": "期初库存",
  Production: "产量",
  Imports: "进口量",
  Exports: "出口量",
  Crush: "压榨量",
  "Domestic Consumption": "国内消费",
  "Ending Stocks": "期末库存",
  "Stocks/Use Ratio": "库存/消费比",
  "Area Harvested": "收获面积",
  Yield: "单产",
};

export function presentationMetricLabel(metric: string, productionConditions = false): string {
  if (productionConditions && metric === "Production") return "作物年度产量";
  return METRIC_LABELS[metric] ?? metric;
}

export function directionClass(value: number | null | undefined): string {
  if (value === null || value === undefined || value === 0) return "is-neutral";
  return value > 0 ? "is-positive" : "is-negative";
}
