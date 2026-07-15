import type { MetricData } from "../model";
import { displayUnit, displayValue, formatNumber } from "../selectors";

export function TrendChart({ metric, axis }: { metric: MetricData; axis: string }) {
  const periods = [...metric.periods].reverse();
  const values = periods.map((period) => displayValue(metric.values[period], metric.unit));
  const numeric = values.filter((value): value is number => value !== null);
  if (!numeric.length) return <div className="empty-chart">当前指标没有可绘制数值。</div>;

  const width = 1040;
  const height = 320;
  const margin = { left: 72, right: 32, top: 32, bottom: 58 };
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;
  const rawMin = Math.min(...numeric);
  const rawMax = Math.max(...numeric);
  const padding = rawMax === rawMin ? Math.max(Math.abs(rawMax) * 0.08, 1) : (rawMax - rawMin) * 0.12;
  const min = rawMin - padding;
  const max = rawMax + padding;
  const x = (index: number) => margin.left + (periods.length === 1 ? innerWidth / 2 : (index / (periods.length - 1)) * innerWidth);
  const y = (value: number) => margin.top + ((max - value) / (max - min)) * innerHeight;
  const segments: string[] = [];
  let current: string[] = [];
  values.forEach((value, index) => {
    if (value === null) {
      if (current.length) segments.push(current.join(" "));
      current = [];
    } else {
      current.push(`${x(index)},${y(value)}`);
    }
  });
  if (current.length) segments.push(current.join(" "));
  const ticks = Array.from({ length: 5 }, (_, index) => min + ((max - min) * index) / 4).reverse();

  return (
    <div className="trend-wrap">
      <svg className="trend-svg" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${metric.metric}${axis}趋势图`}>
        <text x={margin.left} y={17} className="axis-title">{displayUnit(metric.unit)}</text>
        {ticks.map((tick) => (
          <g key={tick}>
            <line x1={margin.left} x2={width - margin.right} y1={y(tick)} y2={y(tick)} className="grid-line" />
            <text x={margin.left - 12} y={y(tick) + 4} textAnchor="end" className="axis-label">{formatNumber(tick, metric.unit)}</text>
          </g>
        ))}
        {segments.map((points, index) => <polyline key={index} points={points} className="trend-line" />)}
        {values.map((value, index) => {
          const period = periods[index];
          const forecast = metric.forecast_status[period] !== "historical";
          return (
            <g key={period}>
              <text x={x(index)} y={height - 24} textAnchor="middle" className={forecast ? "axis-label forecast-label" : "axis-label"}>
                {period}{forecast ? " 预测" : ""}
              </text>
              {value !== null && (
                <circle cx={x(index)} cy={y(value)} r={forecast ? 7 : 5} className={forecast ? "trend-point forecast-point" : "trend-point"}>
                  <title>{period}：{formatNumber(value, metric.unit)} {displayUnit(metric.unit)}</title>
                </circle>
              )}
            </g>
          );
        })}
      </svg>
      <div className="trend-axis-note">横轴：{axis}</div>
      <div className="trend-legend"><span><i />历史</span><span><i className="forecast-key" />预测/前瞻</span></div>
    </div>
  );
}
