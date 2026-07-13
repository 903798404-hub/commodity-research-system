import type { MatrixData } from '../types/dashboard'
import { formatMarketYear } from '../utils/marketYear'
import { formatPercent, isValidNumber, normalizeSeries } from '../utils/number'

type CompactBalanceTableProps = {
  title: string
  metrics: string[]
  years: number[]
  matrix: MatrixData | null
  changes: Record<string, { latestYoY: number | null; monthlyRevision: number | null }>
  showLatestYoY: boolean
  showMonthlyRevision: boolean
}

function formatAmount(value: unknown): string {
  if (!isValidNumber(value)) return '#N/A'
  if (Math.abs(value) < 1 && value !== 0) return value.toFixed(1)
  return String(Math.round(value))
}

function formatValue(metric: string, value: unknown): string {
  return metric === '库存/总使用比' ? (isValidNumber(value) ? formatPercent(value) : '#N/A') : formatAmount(value)
}

function formatChange(metric: string, value: number | null): string {
  if (!isValidNumber(value)) return '#N/A'
  if (metric === '库存/总使用比') return `${value > 0 ? '+' : ''}${value.toFixed(1)}pct`
  if (value === 0) return '0'
  const absolute = Math.abs(value) < 1 ? value.toFixed(1) : String(Math.round(value))
  return `${value > 0 ? '+' : ''}${absolute}`
}

function changeClass(value: number | null): string {
  if (!isValidNumber(value)) return 'presentation-na'
  if (value > 0) return 'presentation-positive'
  if (value < 0) return 'presentation-negative'
  return 'presentation-zero'
}

export function CompactBalanceTable({ title, metrics, years, matrix, changes, showLatestYoY, showMonthlyRevision }: CompactBalanceTableProps) {
  const forecastStart = Math.max(0, years.length - 2)
  const rowsByName = new Map(matrix?.rows.map((row) => [row.name, row]) ?? [])

  return <section className="compact-balance-table" aria-label={title}>
    <h2>{title}</h2>
    <table>
      <thead><tr><th>指标</th>{years.map((year, index) => <th className={index >= forecastStart ? 'presentation-forecast' : ''} key={year}>{formatMarketYear(year)}{index >= forecastStart ? ' *' : ''}</th>)}{showLatestYoY && <th className="presentation-summary">最新同比</th>}{showMonthlyRevision && <th className="presentation-summary">月修</th>}</tr></thead>
      <tbody>{metrics.map((metric) => {
        const row = rowsByName.get(metric)
        const values = matrix && row ? normalizeSeries(row.values, matrix.years.length) : []
        const changesForMetric = changes[metric]
        return <tr key={metric}><th>{metric}</th>{years.map((year, index) => {
          const sourceIndex = matrix?.years.indexOf(year) ?? -1
          const value = sourceIndex >= 0 ? values[sourceIndex] : null
          return <td className={index >= forecastStart ? 'presentation-forecast' : ''} key={`${metric}-${year}`}>{formatValue(metric, value)}</td>
        })}{showLatestYoY && <td className={`presentation-summary ${changeClass(changesForMetric?.latestYoY ?? null)}`}>{formatChange(metric, changesForMetric?.latestYoY ?? null)}</td>}{showMonthlyRevision && <td className={`presentation-summary ${changeClass(changesForMetric?.monthlyRevision ?? null)}`}>{formatChange(metric, changesForMetric?.monthlyRevision ?? null)}</td>}</tr>
      })}</tbody>
    </table>
  </section>
}
