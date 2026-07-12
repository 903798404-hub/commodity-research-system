import { isValidNumber, normalizeSeries, safeAverage, safePercentChange } from './number'
import { isRatioMetric } from './metrics'

export type ChartMode = 'raw' | 'yoy' | 'fiveYearAverage'
export type ChartMetric = { name: string; values: unknown }
export type ChartSeriesDefinition = {
  name: string
  metricName: string
  values: Array<number | null>
  isPercent: boolean
  sampleInsufficient: boolean[]
}

function emptyFlags(length: number) {
  return Array.from({ length }, () => false)
}

export function buildChartSeries(metrics: ChartMetric[], yearCount: number, mode: ChartMode): ChartSeriesDefinition[] {
  return metrics.flatMap((metric) => {
    const raw = normalizeSeries(metric.values, yearCount)
    const isPercent = isRatioMetric(metric.name)
    if (mode === 'raw') return [{ name: metric.name, metricName: metric.name, values: raw, isPercent, sampleInsufficient: emptyFlags(yearCount) }]

    if (mode === 'yoy') {
      return [{
        name: `${metric.name} 同比`,
        metricName: metric.name,
        values: raw.map((value, index) => index === 0 ? null : safePercentChange(value, raw[index - 1])),
        isPercent: true,
        sampleInsufficient: emptyFlags(yearCount),
      }]
    }

    const sampleInsufficient = raw.map((_, index) => raw.slice(Math.max(0, index - 4), index + 1).filter(isValidNumber).length < 5)
    return [
      { name: `${metric.name} 原始`, metricName: metric.name, values: raw, isPercent, sampleInsufficient: emptyFlags(yearCount) },
      {
        name: `${metric.name} 5年均值`,
        metricName: metric.name,
        values: raw.map((_, index) => sampleInsufficient[index] ? null : safeAverage(raw.slice(index - 4, index + 1))),
        isPercent,
        sampleInsufficient,
      },
    ]
  })
}
