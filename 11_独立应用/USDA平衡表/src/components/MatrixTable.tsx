import type { BalanceRow, MatrixData } from '../types/dashboard'
import { formatNumber, formatPercent, normalizeSeries, safeDifference } from '../utils/number'
import { formatMarketYear } from '../utils/marketYear'
import { isRatioMetric } from '../utils/metrics'
import { formatMonthlyRevision, monthlyRevisionFor, type MonthlyRevisionLookup } from '../utils/monthlyRevision'

type MatrixTableProps = { data: MatrixData; rows: BalanceRow[]; visibleStart: number; monthlyRevisions: MonthlyRevisionLookup | null }

export function MatrixTable({ data, rows, visibleStart, monthlyRevisions }: MatrixTableProps) {
  const visibleYears = data.years.slice(visibleStart)
  const forecastStart = Math.max(0, data.years.length - 2)
  const latestYear = data.years.at(-1)
  return <section className="card table-card" aria-label="供需平衡表"><div className="card-heading"><div><h2>{data.commodity} · {data.country}</h2><p>单位：万吨（库存比、最新同比和月修除外）；最近两个市场年度以红色 * 标示为预测年度。</p></div></div><div className="table-scroll"><table><thead><tr><th scope="col">指标</th>{visibleYears.map((year, index) => { const absoluteIndex = visibleStart + index; return <th className={absoluteIndex >= forecastStart ? 'forecast' : ''} scope="col" key={year}>{formatMarketYear(year)}{absoluteIndex >= forecastStart ? ' *' : ''}</th> })}<th scope="col">最新同比</th>{latestYear !== undefined && <th scope="col">{formatMarketYear(latestYear)} 月修</th>}</tr></thead><tbody>{rows.map((row) => { const isRatio = isRatioMetric(row.name); const allValues = normalizeSeries(row.values, data.years.length); const values = allValues.slice(visibleStart); const latestYoYChange = safeDifference(allValues.at(-1), allValues.at(-2)); return <tr key={row.name}><th scope="row">{row.name}</th>{values.map((value, index) => { const absoluteIndex = visibleStart + index; const formatted = isRatio ? formatPercent(value) : formatNumber(value); return <td className={absoluteIndex >= forecastStart ? 'forecast' : ''} key={`${row.name}-${visibleYears[index]}`}>{formatted}</td> })}<td>{formatMonthlyRevision(latestYoYChange, isRatio)}</td>{latestYear !== undefined && <td>{formatMonthlyRevision(monthlyRevisionFor(monthlyRevisions, row, latestYear).revision, isRatio)}</td>}</tr> })}</tbody></table></div></section>
}
