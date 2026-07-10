import { useEffect, useMemo, useState } from 'react'
import ReactECharts from 'echarts-for-react'
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

  const visibleStart = data && !showAllYears ? Math.max(0, data.years.length - 15) : 0
  const visibleYears = data?.years.slice(visibleStart) ?? []
  const forecastStart = data ? Math.max(0, data.years.length - 2) : 0
  const chartOption = useMemo(() => {
    if (!data) return undefined
    const endingStocks = normalizeSeries(data.rows.find((row) => row.name === '期末库存')?.values, data.years.length).slice(visibleStart)
    const stockToUse = normalizeSeries(data.rows.find((row) => row.name === '期末库销比')?.values, data.years.length).slice(visibleStart)
    const chartForecastStart = Math.max(0, data.years.length - 2 - visibleStart)
    return {
      tooltip: {
        trigger: 'axis',
        formatter: (params: Array<{ axisValueLabel?: string; marker?: string; seriesName?: string; value?: unknown }>) => {
          const title = params[0]?.axisValueLabel ?? ''
          const lines = params.map((item) => {
            const formatted = item.seriesName === '期末库销比' ? formatPercent(item.value) : formatNumber(item.value)
            return `${item.marker ?? ''}${item.seriesName ?? ''}：${formatted === '—' ? '暂无数据' : formatted}`
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
      yAxis: [
        { type: 'value', name: '万吨', nameTextStyle: { color: '#667085' } },
        { type: 'value', name: '%', nameTextStyle: { color: '#667085' }, axisLabel: { formatter: '{value}%' } },
      ],
      series: [
        { name: '期末库存', type: 'bar', data: endingStocks, itemStyle: { color: '#2f6f8f' } },
        { name: '期末库销比', type: 'line', yAxisIndex: 1, data: stockToUse, smooth: true, symbolSize: 6, itemStyle: { color: '#c8372d' } },
      ],
    }
  }, [data, visibleStart, visibleYears])

  function chooseCategory(category: Category) {
    if (!catalog || !selection) return
    const firstCommodity = catalog.commodities.find((commodity) => commodity.category === category)
    if (!firstCommodity) return
    setShowAllYears(false)
    setSelection({ ...selection, category, commodityDescription: firstCommodity.commodityDescription })
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
            <div className="card-heading"><div><h2>期末库存与库销比</h2><p>柱状图为期末库存，折线为期末库销比。</p></div></div>
            <ReactECharts option={chartOption} style={{ height: 390, width: '100%' }} />
          </section>
        </>
      )}
    </main>
  )
}

export default App
