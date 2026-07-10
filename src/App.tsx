import { useEffect, useMemo, useState } from 'react'
import ReactECharts from 'echarts-for-react'
import { buildChartSeries, type ChartMode } from './utils/chart'
import { buildMetricCsv, downloadCsv } from './utils/csv'
import { formatNumber, formatPercent, normalizeSeries } from './utils/number'

type Category = 'Oilseeds' | 'Oils' | 'Meals'
type BalanceRow = { name: string; values: unknown }
type MatrixData = {
  commodityCode: string
  commodity: string
  category: Category
  countryCode: string
  country: string
  years: number[]
  rows: BalanceRow[]
}
type Commodity = { commodityCode: string; commodityDescription: string; category: Category; displayName: string }
type Country = { countryCode: string; countryName: string }
type Catalog = {
  categories: Category[]
  commodities: Commodity[]
  countries: Country[]
  defaultSelection: { category: Category; commodityDescription: string; countryName: string }
}
type Selection = { category: Category; commodityDescription: string; countryName: string }

function formatMarketYear(year: number): string {
  return `${String(year).slice(-2)}/${String(year + 1).slice(-2)}`
}

function formatValue(value: unknown, isRatio: boolean): string {
  return isRatio ? formatPercent(value) : formatNumber(value)
}

function App() {
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [selection, setSelection] = useState<Selection | null>(null)
  const [data, setData] = useState<MatrixData | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [showAllYears, setShowAllYears] = useState(false)
  const [selectedIndicators, setSelectedIndicators] = useState<string[]>([])
  const [chartMode, setChartMode] = useState<ChartMode>('raw')

  useEffect(() => {
    fetch('/data/index.json')
      .then((response) => {
        if (!response.ok) throw new Error(`无法读取数据索引（${response.status}）`)
        return response.json() as Promise<Catalog>
      })
      .then((index) => {
        setCatalog(index)
        setSelection(index.defaultSelection)
      })
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '无法读取数据索引'))
  }, [])

  const commodityOptions = useMemo(
    () => catalog?.commodities.filter((commodity) => commodity.category === selection?.category) ?? [],
    [catalog, selection?.category],
  )

  useEffect(() => {
    if (!catalog || !selection) return
    const commodity = catalog.commodities.find((item) => item.commodityDescription === selection.commodityDescription)
    const country = catalog.countries.find((item) => item.countryName === selection.countryName)
    if (!commodity || !country) return

    const controller = new AbortController()
    setIsLoading(true)
    setData(null)
    setError(null)
    fetch(`/data/matrix/${commodity.commodityCode}_${country.countryCode}.json`, { signal: controller.signal })
      .then((response) => {
        if (response.status === 404) return null
        if (!response.ok) throw new Error(`无法读取平衡表数据（${response.status}）`)
        return response.json() as Promise<MatrixData>
      })
      .then((matrix) => setData(matrix))
      .catch((reason: unknown) => {
        if ((reason as DOMException).name !== 'AbortError') setError(reason instanceof Error ? reason.message : '无法读取平衡表数据')
      })
      .finally(() => setIsLoading(false))
    return () => controller.abort()
  }, [catalog, selection])

  useEffect(() => {
    if (!data) return
    const available = data.rows.filter((row) => normalizeSeries(row.values, data.years.length).some((value) => value !== null))
    const preferred = ['期末库存', '期末库销比'].filter((name) => available.some((row) => row.name === name))
    setSelectedIndicators(preferred.length > 0 ? preferred : available.slice(0, 2).map((row) => row.name))
    setChartMode('raw')
  }, [data])

  const visibleStart = data && !showAllYears ? Math.max(0, data.years.length - 15) : 0
  const visibleYears = data?.years.slice(visibleStart) ?? []
  const forecastStart = data ? Math.max(0, data.years.length - 2) : 0
  const chartOption = useMemo(() => {
    if (!data) return undefined
    const selectedMetrics = data.rows.filter((row) => selectedIndicators.includes(row.name))
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
      legend: { top: 4, data: ['期末库存', '期末库销比'] },
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

  function chooseCategory(category: Category) {
    if (!catalog || !selection) return
    const firstCommodity = catalog.commodities.find((commodity) => commodity.category === category)
    if (!firstCommodity) return
    setShowAllYears(false)
    setSelection({ ...selection, category, commodityDescription: firstCommodity.commodityDescription })
  }

  function toggleIndicator(name: string) {
    setSelectedIndicators((current) => current.includes(name) ? current.filter((item) => item !== name) : [...current, name])
  }

  if (error && !catalog) return <main className="page-state">加载失败：{error}</main>
  if (!catalog || !selection) return <main className="page-state">正在加载 USDA PS&amp;D 数据…</main>

  return (
    <main className="dashboard">
      <header className="page-header">
        <p className="eyebrow">USDA PS&amp;D</p>
        <h1>USDA 油脂油粕供需</h1>
        <p className="subtitle">按商品、国家和市场年度查看供需平衡。</p>
      </header>

      <section className="card controls-card" aria-label="数据选择器">
        <nav className="category-tabs" aria-label="商品分类">
          {catalog.categories.map((category) => (
            <button className={selection.category === category ? 'active' : ''} key={category} onClick={() => chooseCategory(category)} type="button">
              {category}
            </button>
          ))}
        </nav>
        <div className="selectors">
          <label>
            Commodity
            <select value={selection.commodityDescription} onChange={(event) => { setShowAllYears(false); setSelection({ ...selection, commodityDescription: event.target.value }) }}>
              {commodityOptions.map((commodity) => <option key={commodity.commodityCode} value={commodity.commodityDescription}>{commodity.displayName}</option>)}
            </select>
          </label>
          <label>
            Country
            <select value={selection.countryName} onChange={(event) => { setShowAllYears(false); setSelection({ ...selection, countryName: event.target.value }) }}>
              {catalog.countries.map((country) => <option key={country.countryCode} value={country.countryName}>{country.countryName}</option>)}
            </select>
          </label>
        </div>
      </section>

      {isLoading && <section className="page-state compact">正在加载平衡表…</section>}
      {error && catalog && <section className="page-state compact">加载失败：{error}</section>}
      {!isLoading && !error && !data && <section className="page-state compact">暂无数据</section>}

      {data && chartOption && (
        <>
          <section className="card chart-controls" aria-label="图表设置">
            <fieldset>
              <legend>图表指标</legend>
              <div className="indicator-list">
                {data.rows.map((row) => <label key={row.name}><input type="checkbox" checked={selectedIndicators.includes(row.name)} onChange={() => toggleIndicator(row.name)} />{row.name}</label>)}
              </div>
            </fieldset>
            <div className="analysis-actions">
              <label>图表模式<select value={chartMode} onChange={(event) => setChartMode(event.target.value as ChartMode)}><option value="raw">原始数值</option><option value="yoy">同比变化</option><option value="fiveYearAverage">五年均值对比</option></select></label>
              <button className="year-toggle" type="button" onClick={() => downloadCsv(buildMetricCsv(data.rows.filter((row) => selectedIndicators.includes(row.name)), data.years, visibleStart), data.commodity, data.country)}>下载 CSV</button>
            </div>
          </section>
          <section className="card table-card" aria-label="供需平衡表">
            <div className="card-heading">
              <div>
                <h2>{data.commodity} · {data.country}</h2>
                <p>单位：万吨（库销比除外）；最近两个市场年度以红色 * 标示为预测年度。</p>
              </div>
              <button className="year-toggle" onClick={() => setShowAllYears(!showAllYears)} type="button">
                {showAllYears ? '只显示最近15年' : '显示全部年份'}
              </button>
            </div>
            <div className="table-scroll">
              <table>
                <thead><tr><th scope="col">指标</th>{visibleYears.map((year, index) => {
                  const absoluteIndex = visibleStart + index
                  return <th className={absoluteIndex >= forecastStart ? 'forecast' : ''} scope="col" key={year}>{formatMarketYear(year)}{absoluteIndex >= forecastStart ? ' *' : ''}</th>
                })}</tr></thead>
                <tbody>{data.rows.map((row) => {
                  const isRatio = row.name === '期末库销比'
                  const values = normalizeSeries(row.values, data.years.length).slice(visibleStart)
                  return <tr key={row.name}><th scope="row">{row.name}</th>{values.map((value, index) => {
                    const absoluteIndex = visibleStart + index
                    return <td className={absoluteIndex >= forecastStart ? 'forecast' : ''} key={`${row.name}-${visibleYears[index]}`}>{formatValue(value, isRatio)}</td>
                  })}</tr>
                })}</tbody>
              </table>
            </div>
          </section>

          <section className="card chart-card" aria-label="期末库存与库销比图表">
            <div className="card-heading"><div><h2>供需指标趋势</h2><p>{chartMode === 'raw' ? '显示所选指标的原始数值。' : chartMode === 'yoy' ? '同比变化仅在上一年有效且不为零时计算。' : '五年均值仅在窗口内有五个有效年份时显示。'}</p></div></div>
            <ReactECharts option={chartOption} style={{ height: 390, width: '100%' }} />
          </section>
        </>
      )}
    </main>
  )
}

export default App
