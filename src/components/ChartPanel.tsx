import { useMemo } from 'react'
import ReactECharts from 'echarts-for-react'
import { buildChartSeries, type ChartMode } from '../utils/chart'
import { formatNumber, formatPercent } from '../utils/number'
import { formatMarketYear } from '../utils/marketYear'
import { filterVisibleMetrics } from '../utils/metrics'
import type { MatrixData } from '../types/dashboard'

type ChartPanelProps = {
  data: MatrixData
  selectedIndicators: string[]
  chartMode: ChartMode
  visibleStart: number
}

export function ChartPanel({ data, selectedIndicators, chartMode, visibleStart }: ChartPanelProps) {
  const visibleYears = data.years.slice(visibleStart)
  const chartTitle = useMemo(() => {
    const names = filterVisibleMetrics(data.rows)
      .filter((row) => selectedIndicators.includes(row.name))
      .map((row) => row.name)

    return names.length > 0 ? `供需指标趋势：${names.join('、')}` : '供需指标趋势'
  }, [data.rows, selectedIndicators])
  const chartOption = useMemo(() => {
    const selectedMetrics = filterVisibleMetrics(data.rows).filter((row) => selectedIndicators.includes(row.name))
    const definitions = buildChartSeries(selectedMetrics, data.years.length, chartMode)
    const hasPercentageSeries = definitions.some((series) => series.isPercent)
    const hasNumberSeries = definitions.some((series) => !series.isPercent)
    const chartForecastStart = Math.max(0, data.years.length - 2 - visibleStart)

    return {
      tooltip: {
        trigger: 'axis',
        formatter: (params: Array<{ axisValueLabel?: string; marker?: string; seriesName?: string; seriesIndex?: number; dataIndex?: number; value?: unknown }>) => {
          const title = params[0]?.axisValueLabel ?? ''
          const lines = params.map((item) => {
            const definition = definitions[item.seriesIndex ?? -1]
            const isForecast = (item.dataIndex ?? -1) >= chartForecastStart
            const sampleInsufficient = definition?.sampleInsufficient[(item.dataIndex ?? -1) + visibleStart]
            const formatted = definition?.isPercent ? formatPercent(item.value) : formatNumber(item.value)
            const valueText = sampleInsufficient ? '样本不足' : formatted === '—' ? '暂无数据' : formatted
            return `${item.marker ?? ''}${item.seriesName ?? ''}：${valueText}${isForecast ? '（预测年份）' : ''}`
          })
          return [title, ...lines].join('<br/>')
        },
      },
      legend: { top: 4, data: definitions.map((definition) => definition.name) },
      grid: { top: 52, right: 58, bottom: 48, left: 58 },
      xAxis: {
        type: 'category',
        data: visibleYears.map(formatMarketYear),
        axisLabel: {
          formatter: (value: string, index: number) => index >= chartForecastStart ? `{forecast|${value}*}` : value,
          rich: { forecast: { color: '#c8372d', fontWeight: 700 } },
        },
      },
      yAxis: chartMode === 'yoy'
        ? [{ type: 'value', name: '%', nameTextStyle: { color: '#667085' }, axisLabel: { formatter: '{value}%' } }]
        : [
          { type: 'value', show: hasNumberSeries, name: '万吨', nameTextStyle: { color: '#667085' } },
          { type: 'value', show: hasPercentageSeries, name: '%', nameTextStyle: { color: '#667085' }, axisLabel: { formatter: '{value}%' } },
        ],
      series: definitions.map((definition, index) => ({
        name: definition.name,
        type: chartMode === 'raw' && index === 0 ? 'bar' : 'line',
        yAxisIndex: chartMode === 'yoy' || !definition.isPercent ? 0 : 1,
        data: definition.values.slice(visibleStart),
        smooth: true,
        connectNulls: false,
        symbolSize: 6,
        itemStyle: { color: index % 2 === 0 ? '#2f6f8f' : '#c8372d' },
        lineStyle: chartMode === 'fiveYearAverage' && definition.name.endsWith('原始') ? { type: 'dashed' } : undefined,
      })),
    }
  }, [chartMode, data, selectedIndicators, visibleStart, visibleYears])

  const description = chartMode === 'raw' ? '显示所选指标的原始数值。' : chartMode === 'yoy' ? '同比变化仅在上一年有效且不为零时计算。' : '五年均值仅在窗口内有五个有效年份时显示。'
  return <section className="card chart-card" aria-label="供需指标趋势图表"><div className="card-heading"><div><h2>{chartTitle}</h2><p>{description}</p></div></div><ReactECharts option={chartOption} style={{ height: 390, width: '100%' }} /></section>
}
