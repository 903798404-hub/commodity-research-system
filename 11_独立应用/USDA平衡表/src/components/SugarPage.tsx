import { useEffect, useState } from 'react'
import { AnalysisControls } from './AnalysisControls'
import { ChartPanel } from './ChartPanel'
import { ErrorState, LoadingState } from './StateViews'
import { getRuntimeDataClient } from '../utils/runtimeDataClient'
import { buildSugarCsv, buildSugarView, formatSugarYear, SUGAR_DATA_PATH, SUGAR_STATUS_LABELS, validateSugarInput, type SugarInput } from '../utils/sugarBalance'
import { formatNumber, formatPercent } from '../utils/number'
import { downloadCsv } from '../utils/csv'
import type { ChartMode } from '../utils/chart'

export function SugarPage() {
  const [input, setInput] = useState<SugarInput | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState(['食糖总产量', '消费量'])
  const [mode, setMode] = useState<ChartMode>('raw')
  useEffect(() => {
    const controller = new AbortController()
    getRuntimeDataClient().then(client => client.readJson<unknown>(SUGAR_DATA_PATH, controller.signal)).then(raw => {
      if (raw === null) throw new Error('当前数据版本尚未接入中国食糖数据，请先发布经校验的食糖数据文件。')
      if (!controller.signal.aborted) setInput(validateSugarInput(raw))
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '无法读取食糖数据')
    })
    return () => controller.abort()
  }, [])
  const view = input ? buildSugarView(input) : null
  const sourceFor = (id: string) => input!.sources.find(s => s.id === id)!
  return <main className="dashboard sugar-dashboard">
    <header className="page-header"><p className="eyebrow">USDA WMT / FAS GAIN</p><h1>中国食糖供需</h1><p className="subtitle">10月—翌年9月 · 原糖值 · 2020/21—2023/24原表历史，2024/25起采用FAS New Post。</p></header>
    {error ? <ErrorState message={error} /> : !input || !view ? <LoadingState /> : <>
      <section className="card table-card" aria-label="中国食糖供需平衡表"><div className="card-heading"><div><h2>食糖供需平衡表</h2><p>数量单位：万吨。比率和产量同比为百分比。未披露分项显示“—”。来源切换从2024/25开始。</p></div></div>
        <div className="table-scroll"><table className="sugar-table"><thead><tr><th scope="col">指标</th>{input.records.map(r => <th scope="col" className={r.status === 'forecast' ? 'forecast' : ''} key={r.year}>{formatSugarYear(r.year)}{r.status === 'forecast' ? ' *' : ''}</th>)}</tr></thead><tbody>
          <tr><th scope="row">数据状态</th>{input.records.map(r => <td className={r.status === 'forecast' ? 'forecast' : ''} key={r.year}>{SUGAR_STATUS_LABELS[r.status]}</td>)}</tr>
          {view.rows.map(row => <tr key={row.name}><th scope="row">{row.name}</th>{row.values.map((v, i) => <td className={input.records[i].status === 'forecast' ? 'forecast' : ''} key={view.years[i]}>{row.unit === '%' ? formatPercent(v) : formatNumber(v)}</td>)}</tr>)}
          <tr className="sugar-source-row"><th scope="row">逐年来源</th>{input.records.map(r => { const source = sourceFor(r.sourceId); return <td key={r.year}><a href={source.url} target="_blank" rel="noreferrer">{source.label}</a><span>{source.reportDate}</span></td> })}</tr>
        </tbody></table></div>
      </section>
      <AnalysisControls rows={view.rows} selectedIndicators={selected} chartMode={mode} onToggleIndicator={name => setSelected(current => current.includes(name) ? current.filter(n => n !== name) : [...current, name])} onChartMode={setMode} onDownload={() => downloadCsv(buildSugarCsv(input, view), 'Sugar-Centrifugal', 'China')} />
      <ChartPanel data={view} selectedIndicators={selected} chartMode={mode} visibleStart={0} forecastYears={view.forecastYears} />
      <section className="card sugar-notes" aria-label="FAS变化原因"><div className="card-heading"><div><h2>FAS变化原因与口径</h2><p>报告日期：{sourceFor(input.records.at(-1)!.sourceId).reportDate}。备注中的同比、前次估计和原表差异分别标明。</p></div></div><dl>{input.notes.map(note => <div key={note.metric}><dt>{note.metric}</dt><dd>{note.text}</dd></div>)}</dl><ul>{input.scopeNotes.map(note => <li key={note}>{note}</li>)}</ul><p className="sugar-report-link"><a href={sourceFor(input.records.at(-1)!.sourceId).url} target="_blank" rel="noreferrer">打开FAS官方报告</a> · {sourceFor(input.records.at(-1)!.sourceId).pages}</p></section>
      <section className="card table-card" aria-label="FAS差异对照"><div className="card-heading"><div><h2>FAS New Post 与原表WMT差异</h2><p>单位：万吨。差额=FAS−原表，属于来源及报告修订对照，不是月修。</p></div></div><div className="table-scroll"><table className="sugar-comparison"><thead><tr><th scope="col">指标</th><th scope="col">市场年度</th><th scope="col">原表WMT</th><th scope="col">FAS New Post</th><th scope="col">差额</th></tr></thead><tbody>{view.comparisons.map(c => <tr key={`${c.year}-${c.metric}`}><th scope="row">{c.metric}</th><td>{formatSugarYear(c.year)}</td><td>{formatNumber(c.baseline)}</td><td>{formatNumber(c.current)}</td><td className={c.difference !== 0 ? 'sugar-difference' : ''}>{formatNumber(c.difference)}</td></tr>)}</tbody></table></div></section>
    </>}
  </main>
}
