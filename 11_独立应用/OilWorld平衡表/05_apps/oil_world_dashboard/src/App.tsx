import { useEffect, useMemo, useState } from "react";
import {
  DataLoadError,
  loadBootstrap,
  loadCombination,
  loadCombinationComparison,
  loadReleaseIndex,
} from "./api";
import { applyQuarterRevisions } from "./comparison";
import type { CombinationData, MetricData, ReleaseIndex, ReleaseList } from "./model";
import {
  cardMetrics,
  displayUnit,
  displayValue,
  fileFor,
  formatNumber,
  formatSignedChange,
  marketYearBasisLabel,
  productsFor,
  quarterRevisionDisplay,
  regionsFor,
  validSelection,
  visibleMetrics,
  type Selection,
} from "./selectors";
import { TrendChart } from "./components/TrendChart";

const BASE_URL = import.meta.env.BASE_URL;

function forecastLabel(status: string | undefined) {
  if (status === "explicit_forecast") return "预测";
  if (status === "implicit_forecast") return "前瞻期";
  return "";
}

function columnForecastLabel(metrics: MetricData[], period: string) {
  const statuses = new Set(metrics.map((metric) => metric.forecast_status[period]).filter(Boolean));
  const hasHistorical = statuses.has("historical");
  if (statuses.has("explicit_forecast")) return hasHistorical ? "含预测" : "预测";
  if (statuses.has("implicit_forecast")) return hasHistorical ? "含前瞻" : "前瞻期";
  return "";
}

function latestMetricPoint(metric: MetricData) {
  const period = metric.periods.find((item) => metric.values[item] !== null);
  return period ? { period, value: metric.values[period] } : null;
}

export default function App() {
  const [releases, setReleases] = useState<ReleaseList | null>(null);
  const [index, setIndex] = useState<ReleaseIndex | null>(null);
  const [release, setRelease] = useState("");
  const [selection, setSelection] = useState<Selection | null>(null);
  const [data, setData] = useState<CombinationData | null>(null);
  const [chartMetric, setChartMetric] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retryKey, setRetryKey] = useState(0);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    loadBootstrap(BASE_URL)
      .then(({ latest, releases: releaseList, index: releaseIndex }) => {
        if (!active) return;
        setReleases(releaseList);
        setIndex(releaseIndex);
        setRelease(latest.release);
        setSelection(validSelection(releaseIndex));
      })
      .catch((reason) => {
        if (!active || (reason instanceof DOMException && reason.name === "AbortError")) return;
        console.error("Oil World bootstrap failed", reason);
        setError(reason instanceof DataLoadError ? reason.message : "无法读取Oil World发布数据。 ");
      })
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [retryKey]);

  useEffect(() => {
    if (!index || !selection || !release) return;
    const file = fileFor(index, selection);
    if (!file) {
      setData(null);
      setError("当前筛选组合没有发布数据。");
      return;
    }
    let active = true;
    setLoading(true);
    const previousRelease = releases?.releases.find((item) => item.release === release)?.previous_release ?? null;
    Promise.all([
      loadCombination(BASE_URL, release, file.path),
      previousRelease
        ? loadCombinationComparison(
            BASE_URL,
            previousRelease,
            release,
            selection.system,
            selection.product,
            selection.region,
          )
        : Promise.resolve(null),
    ])
      .then(([payload, comparison]) => {
        if (!active) return;
        const enrichedPayload = applyQuarterRevisions(payload, comparison);
        setData(enrichedPayload);
        const firstChart = cardMetrics(enrichedPayload)[0]
          ?? visibleMetrics(enrichedPayload).find((metric) => metric.periods.length);
        setChartMetric(firstChart?.metric ?? "");
        setError("");
      })
      .catch((reason) => {
        if (!active) return;
        console.error("Oil World combination failed", reason);
        setData(null);
        setError(reason instanceof DataLoadError ? reason.message : "无法读取当前组合数据。");
      })
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [index, release, releases, selection]);

  async function changeRelease(nextRelease: string) {
    setLoading(true);
    setError("");
    try {
      const nextIndex = await loadReleaseIndex(BASE_URL, nextRelease);
      setIndex(nextIndex);
      setRelease(nextRelease);
      setSelection(validSelection(nextIndex, selection ?? undefined));
    } catch (reason) {
      console.error("Oil World release switch failed", reason);
      setError(reason instanceof DataLoadError ? reason.message : "无法读取所选发布期。");
    } finally {
      setLoading(false);
    }
  }

  function changeSystem(nextSystem: string) {
    if (!index) return;
    setSelection(validSelection(index, { system: nextSystem }));
  }

  function changeProduct(nextProduct: string) {
    if (!index || !selection) return;
    setSelection(validSelection(index, { system: selection.system, product: nextProduct }));
  }

  function changeRegion(nextRegion: string) {
    if (!index || !selection) return;
    setSelection(validSelection(index, { ...selection, region: nextRegion }));
  }

  const rows = useMemo(() => (data ? visibleMetrics(data) : []), [data]);
  const cards = useMemo(() => (data ? cardMetrics(data) : []), [data]);
  const selectedChartMetric = rows.find((metric) => metric.metric === chartMetric && metric.periods.length);

  return (
    <main className="app-shell">
      <header className="hero">
        <div>
          <div className="eyebrow">AGRICULTURAL RESEARCH · OIL WORLD</div>
          <h1>Oil World 供需平衡表</h1>
          <p>以原始报表与专项映射审计为依据，保留各国真实市场年度口径。</p>
        </div>
        <div className="release-pill">{index ? `当前发布 · ${index.release_label}` : "读取发布信息"}</div>
      </header>

      {error && (
        <section className="error-card" role="alert">
          <strong>数据暂时无法打开</strong>
          <span>{error}</span>
          <button onClick={() => setRetryKey((value) => value + 1)}>重新加载</button>
        </section>
      )}

      <section className="filters panel" aria-label="筛选条件">
        <label>
          <span>发布期</span>
          <select value={release} onChange={(event) => void changeRelease(event.target.value)} disabled={!releases}>
            {(releases?.releases ?? []).map((item) => (
              <option key={item.release} value={item.release}>{item.label} · {item.release}</option>
            ))}
          </select>
        </label>
        <label>
          <span>品种体系</span>
          <select value={selection?.system ?? ""} onChange={(event) => changeSystem(event.target.value)} disabled={!index}>
            {(index?.systems ?? []).map((item) => <option key={item.id}>{item.label}</option>)}
          </select>
        </label>
        <label>
          <span>产品</span>
          <select value={selection?.product ?? ""} onChange={(event) => changeProduct(event.target.value)} disabled={!index || !selection}>
            {(index && selection ? productsFor(index, selection.system) : []).map((item) => <option key={item.id}>{item.label}</option>)}
          </select>
        </label>
        <label>
          <span>国家或地区</span>
          <select value={selection?.region ?? ""} onChange={(event) => changeRegion(event.target.value)} disabled={!index || !selection}>
            {(index && selection ? regionsFor(index, selection.system, selection.product) : []).map((item) => <option key={item}>{item}</option>)}
          </select>
        </label>
      </section>

      {loading && !data && <section className="loading-card">正在读取经审计确认的年度数据…</section>}

      {data && (
        <>
          <section className="context-line">
            <div>
              <strong>{data.product}</strong><span> · {data.region}</span>
            </div>
          </section>

          <section className="cards" aria-label="最新状态">
            {cards.map((metric) => {
              const point = latestMetricPoint(metric);
              const shown = point ? displayValue(point.value, metric.unit) : null;
              const change = metric.annual_change
                ? displayValue(metric.annual_change.value, metric.annual_change.unit === "1000 T" ? "1000 T" : metric.unit)
                : null;
              const revision = quarterRevisionDisplay(metric);
              return (
                <article className="metric-card" key={metric.metric}>
                  <div className="metric-card__title">
                    <span>{metric.metric}</span>
                    {metric.is_derived && <em>派生</em>}
                  </div>
                  <div className="metric-card__period">{point?.period ?? "—"} {point && forecastLabel(metric.forecast_status[point.period])}</div>
                  <div className="metric-card__value">{formatNumber(shown, metric.unit)} <small>{displayUnit(metric.unit)}</small></div>
                  <dl>
                    <div><dt>年度变化</dt><dd>{change === null ? "—" : formatNumber(change, metric.annual_change?.unit ?? metric.unit)} {change === null ? "" : displayUnit(metric.annual_change?.unit ?? metric.unit)}</dd></div>
                    <div><dt>季度修正</dt><dd className={revision.hasValue ? "metric-card__revision--value" : "metric-card__revision--muted"}>{revision.text}</dd></div>
                  </dl>
                </article>
              );
            })}
          </section>

          <section className="panel matrix-panel">
            <div className="section-heading matrix-heading">
              <div><span>MARKET YEAR MATRIX</span><h2>完整市场年度平衡表</h2></div>
              <div className="matrix-heading__meta">
                <span className="market-year-basis">{marketYearBasisLabel(data.market_year_basis)}</span>
                <p>数量底层单位为1000 T，页面换算为万吨；空白与0严格区分。</p>
              </div>
            </div>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>指标</th>
                    <th>单位</th>
                    <th className="change-header">季度修正</th>
                    <th className="change-header">年度变化</th>
                    {data.periods.map((period) => {
                      const label = columnForecastLabel(rows, period);
                      return (
                        <th className={label ? "forecast-col" : ""} key={period}>
                          {period}<small>{label}</small>
                        </th>
                      );
                    })}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((metric) => {
                    const revision = quarterRevisionDisplay(metric);
                    return <tr key={metric.metric} className={metric.mapping_status === "conflict" ? "conflict-row" : ""}>
                      <th>
                        <span>{metric.metric}</span>
                        {metric.is_derived && <em className="tag">派生</em>}
                        {metric.mapping_status === "conflict" && <span className="warning-dot" title={metric.quality_note}>!</span>}
                      </th>
                      <td>{displayUnit(metric.unit)}</td>
                      <td className={`change-cell ${revision.hasValue ? "change-cell--value" : "change-cell--muted"}`}>
                        {revision.text}
                      </td>
                      <td className={`change-cell ${metric.annual_change ? "change-cell--value" : "change-cell--muted"}`}>
                        {metric.annual_change
                          ? `${formatSignedChange(displayValue(metric.annual_change.value, metric.annual_change.unit === "1000 T" ? "1000 T" : metric.unit), metric.annual_change.unit)} ${displayUnit(metric.annual_change.unit)}`
                          : "—"}
                      </td>
                      {data.periods.map((period) => {
                        const value = ["direct", "derived"].includes(metric.mapping_status)
                          ? displayValue(metric.values[period], metric.unit)
                          : null;
                        const forecast = metric.forecast_status[period] && metric.forecast_status[period] !== "historical";
                        return <td className={forecast ? "forecast-cell" : ""} key={period}>{formatNumber(value, metric.unit)}</td>;
                      })}
                    </tr>;
                  })}
                </tbody>
              </table>
            </div>
            {rows.some((metric) => metric.mapping_status === "conflict") && (
              <div className="quality-banner">原始Oil World报表存在统计周期、指标定义或来源冲突，当前版本暂不展示相关数值。</div>
            )}
          </section>

          <section className="panel chart-panel">
            <div className="section-heading">
              <div><span>TREND</span><h2>指标趋势</h2></div>
              <label className="chart-select">指标
                <select value={chartMetric} onChange={(event) => setChartMetric(event.target.value)}>
                  {rows.filter((metric) => metric.periods.length && ["direct", "derived"].includes(metric.mapping_status)).map((metric) => (
                    <option key={metric.metric}>{metric.metric}</option>
                  ))}
                </select>
              </label>
            </div>
            {selectedChartMetric ? <TrendChart metric={selectedChartMetric} /> : <div className="empty-chart">当前组合没有可绘制指标。</div>}
          </section>

          <details className="panel sources-panel">
            <summary>数据来源与口径 <span>展开查看原始报表、单元格和质量状态</span></summary>
            <div className="source-grid">
              {rows.map((metric) => (
                <article key={metric.metric}>
                  <h3>{metric.metric} {metric.is_derived && <em className="tag">派生</em>}</h3>
                  <dl>
                    <div><dt>映射状态</dt><dd>{metric.mapping_status}</dd></div>
                    <div><dt>市场年度口径</dt><dd>{metric.market_year_basis}</dd></div>
                    <div><dt>原始指标</dt><dd>{metric.original_metric || "—"}</dd></div>
                    <div><dt>原始单位</dt><dd>{metric.original_unit || "—"}</dd></div>
                    <div><dt>报表编号</dt><dd>{metric.source_report_id.join("；") || "—"}</dd></div>
                    <div><dt>报表标题</dt><dd>{metric.source_report_title.join("；") || "—"}</dd></div>
                    <div><dt>工作表</dt><dd>{metric.source_sheet.join("；") || "—"}</dd></div>
                    <div><dt>来源单元格</dt><dd className="mono">{metric.source_cell_or_range || "—"}</dd></div>
                    {metric.is_derived && <div><dt>派生方法</dt><dd>{metric.derivation_method}；{metric.derivation_components}</dd></div>}
                    {metric.quality_note && <div><dt>质量说明</dt><dd>{metric.quality_note}</dd></div>}
                  </dl>
                </article>
              ))}
            </div>
          </details>
        </>
      )}
    </main>
  );
}
