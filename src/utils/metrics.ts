import type { BalanceRow } from '../types/dashboard'

export const HIDDEN_METRICS = ['饲用及损耗消费', 'Feed Waste Dom. Cons.', 'Feed and Residual', 'Feed Waste Consumption']

export function isHiddenMetric(name: string): boolean {
  const normalized = name.trim().toLowerCase()
  return HIDDEN_METRICS.some((metric) => metric.toLowerCase() === normalized)
}

export function filterVisibleMetrics<T extends Pick<BalanceRow, 'name'>>(rows: T[]): T[] {
  return rows.filter((row) => !isHiddenMetric(row.name))
}
