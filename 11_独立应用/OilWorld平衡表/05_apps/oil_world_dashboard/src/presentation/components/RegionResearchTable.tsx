import type { CombinationData, MetricData } from "../../model";
import {
  displayUnit,
  displayValue,
  formatSignedChange,
  metricStableKey,
  periodHeaderLabel,
  quarterRevisionDisplay,
} from "../../selectors";
import type { PresentationRegionConfig, PresentationSlideConfig } from "../config";
import { presentationPeriodBasis } from "../presentationPeriodBasis";
import {
  directionClass,
  metricByIdentity,
  PRESENTATION_STOCK_USAGE_RATIO,
  presentationCellValue,
  presentationMetricLabel,
  recentAvailablePeriods,
} from "../selectors";

function formatPresentationRatioChange(value: number): string {
  const rounded = Math.round((value + Number.EPSILON) * 100) / 100;
  const magnitude = new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Math.abs(rounded));
  if (rounded > 0) return `+${magnitude}`;
  if (rounded < 0) return `−${magnitude}`;
  return magnitude;
}

function metricUnit(metricName: string): string {
  if (metricName === "Stocks/Use Ratio" || metricName === PRESENTATION_STOCK_USAGE_RATIO) return "%";
  if (metricName === "Area Harvested") return "1000 ha";
  if (metricName === "Yield") return "T/ha";
  return "1000 T";
}

function annualChange(metric: MetricData | undefined) {
  if (!metric || !["direct", "derived"].includes(metric.mapping_status) || !metric.annual_change) {
    return { text: "—", className: "is-missing" };
  }
  const change = metric.annual_change;
  const value = displayValue(change.value, change.unit === "1000 T" ? "1000 T" : metric.unit);
  return {
    text: `${metric.metric === PRESENTATION_STOCK_USAGE_RATIO && value !== null ? formatPresentationRatioChange(value) : formatSignedChange(value, change.unit)} ${displayUnit(change.unit)}`,
    className: directionClass(value),
  };
}

function quarterChange(metric: MetricData | undefined) {
  if (!metric || !["direct", "derived"].includes(metric.mapping_status)) {
    return { text: "—", className: "is-missing" };
  }
  const revision = quarterRevisionDisplay(metric);
  const value = metric.quarter_revision?.value;
  return {
    text: metric.metric === PRESENTATION_STOCK_USAGE_RATIO && typeof value === "number"
      ? `${formatPresentationRatioChange(value)} ${displayUnit("percentage points")}`
      : revision.text,
    className: revision.hasValue ? directionClass(metric.quarter_revision?.value) : "is-missing",
  };
}

export function RegionResearchTable({
  data,
  region,
  slide,
  metricNames = slide.metrics,
}: {
  data: CombinationData;
  region: PresentationRegionConfig;
  slide: PresentationSlideConfig;
  metricNames?: readonly string[];
}) {
  const rows = metricNames.map((metric) => metricByIdentity(data, {
    metric,
    periodFamily: region.periodFamily,
    sourceRole: region.sourceRole,
  }));
  const presentRows = rows.filter((metric): metric is MetricData => Boolean(metric));
  const periods = recentAvailablePeriods(presentRows, 3);
  const periodBasis = presentationPeriodBasis(slide, region, presentRows);
  const calendarAxis = region.periodFamily === "calendar_year";
  const productionConditions = slide.slideType === "production-conditions";

  return (
    <article className="presentation-region-table" data-region={region.region}>
      <div className="presentation-region-title">
        <strong>{slide.productLabel}－{region.label}</strong>
        <span>{periodBasis.resolved_label}</span>
      </div>
      <table aria-label={`${slide.productLabel}－${region.label}`}>
        <thead>
          <tr>
            <th>指标</th>
            <th className="is-revision">季度修正</th>
            <th>年度变化</th>
            {periods.map((period, index) => <th className={index === 0 ? "is-latest" : ""} key={period}>{periodHeaderLabel(period, index, calendarAxis)}</th>)}
          </tr>
        </thead>
        <tbody>
          {metricNames.map((metricName, rowIndex) => {
            const metric = rows[rowIndex];
            const annual = annualChange(metric);
            const revision = quarterChange(metric);
            const unit = metric?.unit ?? metricUnit(metricName);
            const latestValuePeriod = metric?.periods.find((period) => metric.values[period] !== null);
            return (
              <tr
                key={metric ? metricStableKey(metric) : `${metricName}::${region.periodFamily ?? "published"}::${region.sourceRole ?? "published"}`}
                data-metric-key={metric ? metricStableKey(metric) : undefined}
              >
                <th>
                  <span>{presentationMetricLabel(metricName, productionConditions)}</span>
                  {metricName === PRESENTATION_STOCK_USAGE_RATIO && <em className="presentation-derived-tag">网页计算</em>}
                  <small>{displayUnit(unit)}</small>
                </th>
                <td className={`presentation-change is-revision ${revision.className}`}>{revision.text}</td>
                <td className={`presentation-change ${annual.className}`}>{annual.text}</td>
                {periods.map((period) => {
                  const forecast = metric?.forecast_status[period] && metric.forecast_status[period] !== "historical";
                  const emphasize = period === latestValuePeriod || forecast;
                  return <td className={emphasize ? "is-latest" : ""} key={period}>{presentationCellValue(metric, period)}</td>;
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </article>
  );
}
