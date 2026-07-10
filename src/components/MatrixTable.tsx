import type { BalanceRow, MatrixData } from '../types/dashboard'
import { formatNumber, formatPercent, normalizeSeries } from '../utils/number'
import { formatMarketYear } from '../utils/marketYear'

type MatrixTableProps = { data: MatrixData; rows: BalanceRow[]; visibleStart: number }

export function MatrixTable({ data, rows, visibleStart }: MatrixTableProps) {
  const visibleYears = data.years.slice(visibleStart)
  const forecastStart = Math.max(0, data.years.length - 2)
  return <section className="card table-card" aria-label="供需平衡表"><div className="card-heading"><div><h2>{data.commodity} · {data.country}</h2><p>单位：万吨（库销比除外）；最近两个市场年度以红色 * 标示为预测年度。</p></div></div><div className="table-scroll"><table><thead><tr><th scope="col">指标</th>{visibleYears.map((year, index) => { const absoluteIndex = visibleStart + index; return <th className={absoluteIndex >= forecastStart ? 'forecast' : ''} scope="col" key={year}>{formatMarketYear(year)}{absoluteIndex >= forecastStart ? ' *' : ''}</th> })}</tr></thead><tbody>{rows.map((row) => { const isRatio = row.name === '期末库销比'; const values = normalizeSeries(row.values, data.years.length).slice(visibleStart); return <tr key={row.name}><th scope="row">{row.name}</th>{values.map((value, index) => { const absoluteIndex = visibleStart + index; const formatted = isRatio ? formatPercent(value) : formatNumber(value); return <td className={absoluteIndex >= forecastStart ? 'forecast' : ''} key={`${row.name}-${visibleYears[index]}`}>{formatted}</td> })}</tr> })}</tbody></table></div></section>
}
