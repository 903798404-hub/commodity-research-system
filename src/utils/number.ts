export function isValidNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

export function formatNumber(value: unknown, fractionDigits = 1): string {
  return isValidNumber(value) ? value.toFixed(fractionDigits) : '—'
}

export function formatPercent(value: unknown, fractionDigits = 1): string {
  return isValidNumber(value) ? `${value.toFixed(fractionDigits)}%` : '—'
}

export function toChartValue(value: unknown): number | null {
  return isValidNumber(value) ? value : null
}

export function normalizeSeries(values: unknown, length: number): Array<number | null> {
  const source = Array.isArray(values) ? values : []
  return Array.from({ length }, (_, index) => toChartValue(source[index]))
}

export function safeDivide(numerator: unknown, denominator: unknown): number | null {
  if (!isValidNumber(numerator) || !isValidNumber(denominator) || denominator === 0) return null
  return numerator / denominator
}

export function safePercentChange(current: unknown, previous: unknown): number | null {
  const ratio = safeDivide(isValidNumber(current) && isValidNumber(previous) ? current - previous : null, previous)
  return ratio === null ? null : ratio * 100
}

export function safeDifference(current: unknown, previous: unknown): number | null {
  if (!isValidNumber(current) || !isValidNumber(previous)) return null
  return current - previous
}

export function safeAverage(values: unknown): number | null {
  if (!Array.isArray(values)) return null
  const valid = values.filter(isValidNumber)
  if (valid.length === 0) return null
  return valid.reduce((sum, value) => sum + value, 0) / valid.length
}
