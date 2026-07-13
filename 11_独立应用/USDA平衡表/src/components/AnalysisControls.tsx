import type { ChartMode } from '../utils/chart'
import type { BalanceRow } from '../types/dashboard'

type AnalysisControlsProps = {
  rows: BalanceRow[]
  selectedIndicators: string[]
  chartMode: ChartMode
  onToggleIndicator: (name: string) => void
  onChartMode: (mode: ChartMode) => void
  onDownload: () => void
}

export function AnalysisControls({ rows, selectedIndicators, chartMode, onToggleIndicator, onChartMode, onDownload }: AnalysisControlsProps) {
  return <section className="card chart-controls" aria-label="图表设置">
    <fieldset><legend>图表指标</legend><div className="indicator-list">{rows.map((row) => <label key={row.name}><input type="checkbox" checked={selectedIndicators.includes(row.name)} onChange={() => onToggleIndicator(row.name)} />{row.name}</label>)}</div></fieldset>
    <div className="analysis-actions"><label>图表模式<select value={chartMode} onChange={(event) => onChartMode(event.target.value as ChartMode)}><option value="raw">原始数值</option><option value="yoy">同比变化</option><option value="fiveYearAverage">五年均值对比</option></select></label><button className="year-toggle" type="button" onClick={onDownload}>下载 CSV</button></div>
  </section>
}
