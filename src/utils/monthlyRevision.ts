import type { BalanceRow, MatrixData } from '../types/dashboard'
import { isValidNumber, normalizeSeries } from './number'

export type MonthlyRevisionValue = { previousValue: number | null; revision: number | null }
export type MonthlyRevisionLookup = Record<string, Record<number, MonthlyRevisionValue>>

export function buildMonthlyRevisionLookup(current: MatrixData, previous: MatrixData | null): MonthlyRevisionLookup {
  const lookup: MonthlyRevisionLookup = {}
  const previousRows = new Map(previous?.rows.map((row) => [row.name, row]) ?? [])
  for (const row of current.rows) {
    const currentValues = normalizeSeries(row.values, current.years.length)
    const previousRow = previousRows.get(row.name)
    const previousValues = previous ? normalizeSeries(previousRow?.values, previous.years.length) : []
    lookup[row.name] = Object.fromEntries(current.years.map((year, index) => {
      const previousIndex = previous?.years.indexOf(year) ?? -1
      const currentValue = currentValues[index]
      const previousValue = previousIndex >= 0 ? previousValues[previousIndex] : null
      const revision = isValidNumber(currentValue) && isValidNumber(previousValue) ? currentValue - previousValue : null
      return [year, { previousValue, revision }]
    }))
  }
  return lookup
}

export function monthlyRevisionFor(lookup: MonthlyRevisionLookup | null, row: Pick<BalanceRow, 'name'>, year: number): MonthlyRevisionValue {
  return lookup?.[row.name]?.[year] ?? { previousValue: null, revision: null }
}

export function formatMonthlyRevision(value: unknown, isPercentagePoint = false): string {
  if (!isValidNumber(value)) return '—'
  const formatted = `${value > 0 ? '+' : ''}${value.toFixed(1)}`
  return isPercentagePoint ? `${formatted}pct` : formatted
}
