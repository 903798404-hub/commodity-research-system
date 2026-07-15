import type { MetricData } from "../model";
import {
  displayUnit,
  displayValue,
  formatNumber,
  formatSignedChange,
  metricDisplayLabel,
  metricStableKey,
  periodHeaderLabel,
  quarterRevisionDisplay,
} from "../selectors";

function columnForecastLabel(metrics: MetricData[], period: string) {
  const statuses = new Set(metrics.map((metric) => metric.forecast_status[period]).filter(Boolean));
  const hasHistorical = statuses.has("historical");
  if (statuses.has("explicit_forecast")) return hasHistorical ? "含预测" : "预测";
  if (statuses.has("implicit_forecast")) return hasHistorical ? "含前瞻" : "前瞻期";
  return "";
}

interface MetricMatrixProps {
  eyebrow: string;
  title: string;
  basisLabel: string;
  note: string;
  rows: MetricData[];
  periods: string[];
  dualTimeAxes?: boolean;
  calendarAxis?: boolean;
}

export function MetricMatrix({
  eyebrow,
  title,
  basisLabel,
  note,
  rows,
  periods,
  dualTimeAxes = false,
  calendarAxis = false,
}: MetricMatrixProps) {
  return (
    <section className={`panel matrix-panel${dualTimeAxes ? " dual-axis-panel" : ""}`}>
      <div className="section-heading matrix-heading">
        <div><span>{eyebrow}</span><h2>{title}</h2></div>
        <div className="matrix-heading__meta">
          <span className="market-year-basis">{basisLabel}</span>
          <p>{note}</p>
        </div>
      </div>
      <div className="table-scroll">
        <table aria-label={title}>
          <thead>
            <tr>
              <th>指标</th>
              <th>单位</th>
              <th className="change-header">季度修正</th>
              <th className="change-header">年度变化</th>
              {periods.map((period, index) => {
                const forceCalendarForecast = calendarAxis && index === 0;
                const label = forceCalendarForecast ? "预测" : columnForecastLabel(rows, period);
                return (
                  <th className={label ? "forecast-col" : ""} key={period}>
                    {periodHeaderLabel(period, index, calendarAxis)}<small>{label}</small>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {rows.map((metric) => {
              const revision = quarterRevisionDisplay(metric);
              return (
                <tr key={metricStableKey(metric)} className={metric.mapping_status === "conflict" ? "conflict-row" : ""}>
                  <th>
                    <span>{metricDisplayLabel(metric, dualTimeAxes)}</span>
                    {metric.is_derived && <em className="tag">派生</em>}
                    {metric.mapping_status === "conflict" && <span className="warning-dot" title={metric.quality_note}>!</span>}
                  </th>
                  <td>{displayUnit(metric.unit)}</td>
                  <td className={`change-cell ${revision.hasValue ? "change-cell--value" : "change-cell--muted"}`}>
                    {revision.text}
                  </td>
                  <td className={`change-cell ${metric.annual_change ? "change-cell--value" : "change-cell--muted"}`}>
                    {metric.annual_change
                      ? `${formatSignedChange(
                          displayValue(
                            metric.annual_change.value,
                            metric.annual_change.unit === "1000 T" ? "1000 T" : metric.unit,
                          ),
                          metric.annual_change.unit,
                        )} ${displayUnit(metric.annual_change.unit)}`
                      : "—"}
                  </td>
                  {periods.map((period) => {
                    const value = ["direct", "derived"].includes(metric.mapping_status)
                      ? displayValue(metric.values[period], metric.unit)
                      : null;
                    const forecast = metric.forecast_status[period] && metric.forecast_status[period] !== "historical";
                    return <td className={forecast ? "forecast-cell" : ""} key={period}>{formatNumber(value, metric.unit)}</td>;
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {rows.some((metric) => metric.mapping_status === "conflict") && (
        <div className="quality-banner">原始Oil World报表存在统计周期、指标定义或来源冲突，当前版本暂不展示相关数值。</div>
      )}
    </section>
  );
}
