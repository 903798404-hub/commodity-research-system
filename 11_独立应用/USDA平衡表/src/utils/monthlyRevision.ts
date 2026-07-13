import type { BalanceRow, MatrixData } from '../types/dashboard'
import { isValidNumber, normalizeSeries, safeDifference } from './number'

export type MonthlyRevisionValue = { previousValue: number | null; revision: number | null }
export type MonthlyRevisionLookup = Record<string, Record<number, MonthlyRevisionValue>>

const G2_ADDITIVE_METRICS = new Set([
  '期初库存', '产量', '进口量', '出口量', '消费量', '工业消费', '食用消费', '饲用及损耗消费', '期末库存', '总分配', 'Total Distribution',
])
const G3_ADDITIVE_METRICS = new Set([
  '期初库存', '产量', '进口量', '压榨量', '出口量', '消费量', '工业消费', '食用消费', '饲用及损耗消费', '期末库存', '总分配',
  '大豆产量', '大豆压榨', 'Total Distribution',
])

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

function matrixValue(matrix: MatrixData | null, metric: string, year: number): number | null {
  if (!matrix) return null
  const row = matrix.rows.find((item) => item.name === metric)
  const index = matrix.years.indexOf(year)
  if (!row || index < 0) return null
  return normalizeSeries(row.values, matrix.years.length)[index]
}

function strictPairSum(first: MatrixData | null, second: MatrixData | null, metric: string, year: number): number | null {
  const firstValue = matrixValue(first, metric, year)
  const secondValue = matrixValue(second, metric, year)
  return isValidNumber(firstValue) && isValidNumber(secondValue) ? firstValue + secondValue : null
}

function strictGroupSum(matrices: Array<MatrixData | null>, metric: string, year: number): number | null {
  const values = matrices.map((matrix) => matrixValue(matrix, metric, year))
  return values.length > 0 && values.every(isValidNumber) ? values.reduce((sum, value) => sum + value, 0) : null
}

function g2StockToUseRatio(malaysia: MatrixData | null, indonesia: MatrixData | null, year: number): number | null {
  const endingStocks = strictPairSum(malaysia, indonesia, '期末库存', year)
  const domesticConsumption = strictPairSum(malaysia, indonesia, '消费量', year)
  const exports = strictPairSum(malaysia, indonesia, '出口量', year)
  const totalUse = isValidNumber(domesticConsumption) && isValidNumber(exports) ? domesticConsumption + exports : null
  return isValidNumber(endingStocks) && isValidNumber(totalUse) && totalUse > 0 ? (endingStocks / totalUse) * 100 : null
}

function g3StockToUseRatio(matrices: Array<MatrixData | null>, year: number): number | null {
  const endingStocks = strictGroupSum(matrices, '期末库存', year)
  const domesticConsumption = strictGroupSum(matrices, '消费量', year)
  const exports = strictGroupSum(matrices, '出口量', year)
  const totalUse = isValidNumber(domesticConsumption) && isValidNumber(exports) ? domesticConsumption + exports : null
  if (isValidNumber(endingStocks) && isValidNumber(totalUse) && totalUse > 0) return (endingStocks / totalUse) * 100
  const totalDistribution = strictGroupSum(matrices, '总分配', year)
  const fallbackTotalUse = isValidNumber(endingStocks) && isValidNumber(totalDistribution) ? totalDistribution - endingStocks : null
  return isValidNumber(endingStocks) && isValidNumber(fallbackTotalUse) && fallbackTotalUse > 0 ? (endingStocks / fallbackTotalUse) * 100 : null
}

/**
 * G2 is a derived Oil, Palm view. Historical G2 matrix snapshots are deliberately
 * not required: revisions are reconstructed from Malaysia and Indonesia snapshots.
 */
export function buildPalmG2MonthlyRevisionLookup(
  currentG2: MatrixData,
  currentMalaysia: MatrixData | null,
  previousMalaysia: MatrixData | null,
  currentIndonesia: MatrixData | null,
  previousIndonesia: MatrixData | null,
): MonthlyRevisionLookup {
  const lookup: MonthlyRevisionLookup = {}
  for (const row of currentG2.rows) {
    lookup[row.name] = Object.fromEntries(currentG2.years.map((year) => {
      if (G2_ADDITIVE_METRICS.has(row.name)) {
        const currentValue = strictPairSum(currentMalaysia, currentIndonesia, row.name, year)
        const previousValue = strictPairSum(previousMalaysia, previousIndonesia, row.name, year)
        return [year, { previousValue, revision: safeDifference(currentValue, previousValue) }]
      }
      if (row.name === '库存/总使用比') {
        const currentValue = g2StockToUseRatio(currentMalaysia, currentIndonesia, year)
        const previousValue = g2StockToUseRatio(previousMalaysia, previousIndonesia, year)
        return [year, { previousValue, revision: safeDifference(currentValue, previousValue) }]
      }
      return [year, { previousValue: null, revision: null }]
    }))
  }
  return lookup
}

/**
 * G3 is a strict United States + Brazil + Argentina soybean aggregate. Its
 * revisions come from the three country matrices, so historical G3 snapshots
 * are not required and a one-sided value never becomes an implicit zero.
 */
export function buildSoybeanG3MonthlyRevisionLookup(
  currentG3: MatrixData,
  currentSources: Array<MatrixData | null>,
  previousSources: Array<MatrixData | null>,
): MonthlyRevisionLookup {
  const lookup: MonthlyRevisionLookup = {}
  for (const row of currentG3.rows) {
    lookup[row.name] = Object.fromEntries(currentG3.years.map((year) => {
      if (G3_ADDITIVE_METRICS.has(row.name)) {
        const currentValue = strictGroupSum(currentSources, row.name, year)
        const previousValue = strictGroupSum(previousSources, row.name, year)
        return [year, { previousValue, revision: safeDifference(currentValue, previousValue) }]
      }
      if (row.name === '库存/总使用比') {
        const currentValue = g3StockToUseRatio(currentSources, year)
        const previousValue = g3StockToUseRatio(previousSources, year)
        return [year, { previousValue, revision: safeDifference(currentValue, previousValue) }]
      }
      return [year, { previousValue: null, revision: null }]
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
