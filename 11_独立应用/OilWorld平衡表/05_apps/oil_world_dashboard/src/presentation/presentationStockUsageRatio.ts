import type { CombinationData, MetricData } from "../model";
import {
  metricByIdentity,
  PRESENTATION_STOCK_USAGE_RATIO,
  type MetricIdentity,
} from "./selectors";

export { PRESENTATION_STOCK_USAGE_RATIO } from "./selectors";

export const PRESENTATION_STOCK_USAGE_FOOTER_NOTE =
  "Global＝期末库存÷国内消费；单个国家＝期末库存÷（国内消费＋出口）；G2/G3因组内贸易无法安全剔除，暂不计算。";

type ScopeType = "global" | "country" | "aggregate";

const COUNTRY_REGIONS = new Set(["United States", "Brazil", "Argentina", "China"]);
const USABLE_STATUSES = new Set(["direct", "derived"]);

function scopeType(region: string): ScopeType {
  if (region === "Global") return "global";
  if (COUNTRY_REGIONS.has(region)) return "country";
  return "aggregate";
}

function unique(values: readonly string[]): string[] {
  return [...new Set(values.filter(Boolean))];
}

function resolveIdentity(data: CombinationData, requested: MetricIdentity): MetricIdentity {
  const endings = data.metrics.filter((metric) =>
    metric.metric === "Ending Stocks"
    && (!requested.periodFamily || metric.period_family === requested.periodFamily)
    && (!requested.sourceRole || metric.source_role === requested.sourceRole),
  );
  const ending = endings.find((candidate) => metricByIdentity(data, {
    metric: "Domestic Consumption",
    periodFamily: candidate.period_family,
    sourceRole: candidate.source_role,
  }));
  return {
    metric: PRESENTATION_STOCK_USAGE_RATIO,
    periodFamily: requested.periodFamily ?? ending?.period_family,
    sourceRole: requested.sourceRole ?? ending?.source_role,
  };
}

function componentValue(metric: MetricData | undefined, period: string): number | null {
  if (!metric || !USABLE_STATUSES.has(metric.mapping_status)) return null;
  const value = metric.values[period];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function emptyMetric(identity: MetricIdentity): MetricData {
  return {
    metric: PRESENTATION_STOCK_USAGE_RATIO,
    mapping_status: "missing",
    market_year_basis: "演示层网页计算",
    period_family: identity.periodFamily,
    period_basis: "",
    source_role: identity.sourceRole,
    periods: [],
    original_periods: {},
    forecast_status: {},
    original_metric: PRESENTATION_STOCK_USAGE_RATIO,
    values: {},
    annual_change: null,
    quarter_revision: null,
    quarter_revision_note: "组成项不足或该区域不适合网页计算。",
    unit: "%",
    original_unit: "%",
    source_report_id: [],
    source_report_title: [],
    source_sheet: [],
    source_cells: {},
    source_cell_or_range: "",
    is_derived: true,
    derivation_method: "",
    derivation_components: "",
    quality_note: "仅用于演示页面，不覆盖正式Stocks/Use Ratio。",
    has_footnote: false,
    has_star: false,
  };
}

function calculateMetric(data: CombinationData, requested: MetricIdentity): MetricData {
  const identity = resolveIdentity(data, requested);
  const result = emptyMetric(identity);
  const ending = metricByIdentity(data, {
    metric: "Ending Stocks",
    periodFamily: identity.periodFamily,
    sourceRole: identity.sourceRole,
  });
  const consumption = metricByIdentity(data, {
    metric: "Domestic Consumption",
    periodFamily: identity.periodFamily,
    sourceRole: identity.sourceRole,
  });
  const scope = scopeType(data.region);
  const exportsMetric = scope === "country" ? metricByIdentity(data, {
    metric: "Exports",
    periodFamily: identity.periodFamily,
    sourceRole: identity.sourceRole,
  }) : undefined;

  if (!ending || !consumption || (scope === "country" && !exportsMetric)) return result;
  const components = scope === "country" ? [ending, consumption, exportsMetric!] : [ending, consumption];
  const sameUnit = components.every((metric) => metric.unit === ending.unit);
  const commonPeriods = ending.periods.filter((period) => components.every((metric) => metric.periods.includes(period)));
  const values = Object.fromEntries(commonPeriods.map((period) => {
    if (scope === "aggregate" || !sameUnit) return [period, null];
    const endingValue = componentValue(ending, period);
    const consumptionValue = componentValue(consumption, period);
    const exportValue = scope === "country" ? componentValue(exportsMetric, period) : 0;
    if (endingValue === null || consumptionValue === null || exportValue === null) return [period, null];
    const denominator = consumptionValue + exportValue;
    return [period, denominator === 0 ? null : endingValue / denominator * 100];
  }));
  const numericPeriods = commonPeriods.filter((period) => values[period] !== null);
  const annualChange = numericPeriods.length >= 2
    ? {
        current_period: numericPeriods[0],
        previous_period: numericPeriods[1],
        value: values[numericPeriods[0]]! - values[numericPeriods[1]]!,
        unit: "percentage points",
      }
    : null;
  const formula = scope === "global"
    ? "Ending Stocks / Domestic Consumption × 100"
    : scope === "country"
      ? "Ending Stocks / (Domestic Consumption + Exports) × 100"
      : "G2/G3及其他聚合区域不进行网页计算";
  const sourceCells = Object.fromEntries(commonPeriods.map((period) => [
    period,
    unique(components.flatMap((metric) => metric.source_cells[period] ?? [])),
  ]));

  return {
    ...result,
    mapping_status: numericPeriods.length > 0 ? "derived" : "missing",
    market_year_basis: ending.market_year_basis,
    period_basis: ending.period_basis,
    source_period_label: ending.source_period_label,
    periods: commonPeriods,
    original_periods: Object.fromEntries(commonPeriods.map((period) => [period, ending.original_periods[period] ?? period])),
    forecast_status: Object.fromEntries(commonPeriods.map((period) => [period, ending.forecast_status[period] ?? "historical"])),
    values,
    annual_change: annualChange,
    source_report_id: unique(components.flatMap((metric) => metric.source_report_id)),
    source_report_title: unique(components.flatMap((metric) => metric.source_report_title)),
    source_sheet: unique(components.flatMap((metric) => metric.source_sheet)),
    source_cells: sourceCells,
    source_cell_or_range: unique(Object.values(sourceCells).flat()).join(" + "),
    derivation_method: formula,
    derivation_components: scope === "country"
      ? "Ending Stocks; Domestic Consumption; Exports"
      : scope === "global" ? "Ending Stocks; Domestic Consumption" : "",
    quality_note: scope === "aggregate"
      ? "聚合区域无法安全排除组内贸易，网页计算保持为空。"
      : "演示层使用底层未四舍五入数值计算，不覆盖正式Stocks/Use Ratio。",
    has_footnote: components.some((metric) => metric.has_footnote),
    has_star: components.some((metric) => metric.has_star),
  };
}

export function applyPresentationStockUsageRatio(
  current: CombinationData,
  previous: CombinationData | null,
  identity: Omit<MetricIdentity, "metric"> = {},
): CombinationData {
  const requested = { metric: PRESENTATION_STOCK_USAGE_RATIO, ...identity };
  const ratio = calculateMetric(current, requested);
  const latestPeriod = ratio.periods.find((period) => ratio.values[period] !== null);
  const previousRatio = previous
    ? calculateMetric(previous, {
        metric: PRESENTATION_STOCK_USAGE_RATIO,
        periodFamily: ratio.period_family,
        sourceRole: ratio.source_role,
      })
    : null;
  const previousValue = latestPeriod ? previousRatio?.values[latestPeriod] : null;
  const currentValue = latestPeriod ? ratio.values[latestPeriod] : null;
  const quarterRevision = latestPeriod
    && typeof currentValue === "number"
    && typeof previousValue === "number"
    ? { period: latestPeriod, value: currentValue - previousValue, unit: "percentage points" }
    : null;

  return {
    ...current,
    metrics: [
      ...current.metrics.filter((metric) => metric.metric !== PRESENTATION_STOCK_USAGE_RATIO),
      {
        ...ratio,
        quarter_revision: quarterRevision,
        quarter_revision_note: quarterRevision ? "" : previous ? "上一发布期同期间不可比。" : "暂无上一期",
      },
    ],
  };
}
