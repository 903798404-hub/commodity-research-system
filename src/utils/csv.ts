import { isValidNumber, normalizeSeries } from './number'

type CsvMetric = { name: string; values: unknown }

function escapeCsv(value: string): string {
  return /[",\n]/.test(value) ? `"${value.replaceAll('"', '""')}"` : value
}

function safeFilePart(value: string): string {
  return value.replace(/[^a-z0-9]+/gi, '-').replace(/^-|-$/g, '') || 'usda-data'
}

export function buildMetricCsv(metrics: CsvMetric[], years: number[], startIndex: number): string {
  const visibleYears = years.slice(startIndex)
  const lines = [
    ['指标', ...visibleYears.map(String)].map(escapeCsv).join(','),
    ...metrics.map((metric) => {
      const values = normalizeSeries(metric.values, years.length).slice(startIndex)
      return [metric.name, ...values.map((value) => isValidNumber(value) ? String(value) : '')].map(escapeCsv).join(',')
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
