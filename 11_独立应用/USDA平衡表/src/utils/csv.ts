import { isValidNumber, normalizeSeries, safeDifference } from './number'
import { monthlyRevisionFor, type MonthlyRevisionLookup } from './monthlyRevision'

type CsvMetric = { name: string; values: unknown }

function escapeCsv(value: string): string {
  return /[",\n]/.test(value) ? `"${value.replaceAll('"', '""')}"` : value
}

function safeFilePart(value: string): string {
  return value.replace(/[^a-z0-9]+/gi, '-').replace(/^-|-$/g, '') || 'usda-data'
}

export function buildMetricCsv(metrics: CsvMetric[], years: number[], startIndex: number, monthlyRevisions: MonthlyRevisionLookup | null): string {
  const visibleYears = years.slice(startIndex)
  const latestYear = years.at(-1)
  const lines = [
    ['指标', '市场年度', '本月值', 'latest_yoy_change', 'latest_year_previous_report_value', 'latest_year_monthly_revision'].map(escapeCsv).join(','),
    ...metrics.flatMap((metric) => {
      const allValues = normalizeSeries(metric.values, years.length)
      const values = allValues.slice(startIndex)
      const latestYoYChange = safeDifference(allValues.at(-1), allValues.at(-2))
      return values.map((value, index) => {
        const year = visibleYears[index]
        const monthly = year === latestYear ? monthlyRevisionFor(monthlyRevisions, metric, year) : { previousValue: null, revision: null }
        const isLatestYear = year === latestYear
        return [metric.name, String(year), isValidNumber(value) ? String(value) : '', isLatestYear && isValidNumber(latestYoYChange) ? String(latestYoYChange) : '', isValidNumber(monthly.previousValue) ? String(monthly.previousValue) : '', isValidNumber(monthly.revision) ? String(monthly.revision) : ''].map(escapeCsv).join(',')
      })
    }),
  ]
  return `\uFEFF${lines.join('\n')}\n`
}

export function downloadCsv(content: string, commodity: string, country: string) {
  const date = new Date().toISOString().slice(0, 10)
  const fileName = `${safeFilePart(commodity)}_${safeFilePart(country)}_${date}.csv`
  const url = URL.createObjectURL(new Blob([content], { type: 'text/csv;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = fileName
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}
