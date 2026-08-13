import { useEffect, useMemo, useState } from 'react'
import { AnalysisControls } from './components/AnalysisControls'
import { ChartPanel } from './components/ChartPanel'
import { DashboardHeader } from './components/DashboardHeader'
import { MatrixTable } from './components/MatrixTable'
import { MarketYearGuide } from './components/MarketYearGuide'
import { SelectorPanel } from './components/SelectorPanel'
import { EmptyState, ErrorState, LoadingState } from './components/StateViews'
import type { Catalog, Category, Commodity, Country, MatrixData, MatrixReference, Selection } from './types/dashboard'
import { type ChartMode } from './utils/chart'
import { buildMetricCsv, downloadCsv } from './utils/csv'
import { filterVisibleMetrics } from './utils/metrics'
import { buildMonthlyRevisionLookup, buildPalmG2MonthlyRevisionLookup, type MonthlyRevisionLookup } from './utils/monthlyRevision'
import { normalizeSeries } from './utils/number'
import { sourceCompatibility, type SourceBasis } from './utils/sourceCompatibility'
import { getRuntimeDataClient, type RuntimeDataClient } from './utils/runtimeDataClient'
import provenance from '../configs/usda_source_provenance.json'

function uniqueByCode<T extends { commodityCode?: string; countryCode?: string }>(items: T[], key: 'commodityCode' | 'countryCode'): T[] {
  return [...new Map(items.map((item) => [item[key] ?? '', item])).values()]
}

async function fetchMatrixJson(client: RuntimeDataClient, path: string, signal: AbortSignal): Promise<MatrixData | null> {
  return client.readJson<MatrixData>(path, signal)
}

function App() {
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [selection, setSelection] = useState<Selection | null>(null)
  const [data, setData] = useState<MatrixData | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedIndicators, setSelectedIndicators] = useState<string[]>([])
  const [chartMode, setChartMode] = useState<ChartMode>('raw')
  const [reportVersion, setReportVersion] = useState<{ currentReportMonth: string; previousReportMonth: string } | null>(null)
  const [monthlyRevisions, setMonthlyRevisions] = useState<MonthlyRevisionLookup | null>(null)
  const [runtimeClient, setRuntimeClient] = useState<RuntimeDataClient | null>(null)

  useEffect(() => {
    getRuntimeDataClient().then(async (client) => {
      const [index, version] = await Promise.all([
        client.readJson<Catalog>('index.json'),
        client.readJson<{ currentReportMonth: string; previousReportMonth: string }>('report_version.json'),
      ])
      if (!index) throw new Error('无法读取数据索引')
      setRuntimeClient(client)
      setReportVersion(version)
      setCatalog(index)
      const initial = index.matrices.find((item) => item.category === index.defaultSelection.category && item.commodity === index.defaultSelection.commodityDescription && item.country === index.defaultSelection.countryName) ?? index.matrices[0]
      setSelection(initial ? { category: initial.category, commodityDescription: initial.commodity, countryName: initial.country } : null)
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '无法读取数据索引'))
  }, [])

  const categoryMatrices = useMemo(() => catalog && selection ? catalog.matrices.filter((item) => item.category === selection.category) : [], [catalog, selection])
  const commodityOptions = useMemo(() => {
    if (!catalog) return []
    const byCode = uniqueByCode(categoryMatrices, 'commodityCode')
    return byCode.map((item) => catalog.commodities.find((commodity) => commodity.commodityCode === item.commodityCode)).filter((item): item is Commodity => Boolean(item))
  }, [catalog, categoryMatrices])
  const countryOptions = useMemo(() => {
    if (!catalog || !selection) return []
    const byCode = uniqueByCode(categoryMatrices.filter((item) => item.commodity === selection.commodityDescription), 'countryCode')
    return byCode.map((item) => catalog.countries.find((country) => country.countryCode === item.countryCode)).filter((item): item is Country => Boolean(item))
  }, [catalog, categoryMatrices, selection])

  useEffect(() => {
    if (!catalog || !selection || !runtimeClient) return
    const matrix = catalog.matrices.find((item) => item.category === selection.category && item.commodity === selection.commodityDescription && item.country === selection.countryName)
    if (!matrix) { setData(null); setError(null); setIsLoading(false); return }
    const controller = new AbortController()
    setIsLoading(true); setData(null); setError(null)
    runtimeClient.readJson<MatrixData>(matrix.file, controller.signal).then((loaded) => setData(loaded)).catch((reason: unknown) => {
      if ((reason as DOMException).name !== 'AbortError') { setData(null); setError(null) }
    }).finally(() => setIsLoading(false))
    return () => controller.abort()
  }, [catalog, runtimeClient, selection])

  useEffect(() => {
    if (!data || !catalog || !selection || !reportVersion || !runtimeClient) { setMonthlyRevisions(null); return }
    const matrix = catalog.matrices.find((item) => item.category === selection.category && item.commodity === selection.commodityDescription && item.country === selection.countryName)
    if (!matrix) { setMonthlyRevisions(null); return }
    const controller = new AbortController()
    const isPalmG2 = matrix.commodity === 'Oil, Palm' && matrix.countryCode === 'G2'
    if (isPalmG2) {
      const malaysia = catalog.matrices.find((item) => item.commodity === 'Oil, Palm' && item.countryCode === 'MY')
      const indonesia = catalog.matrices.find((item) => item.commodity === 'Oil, Palm' && item.countryCode === 'ID')
      if (!malaysia || !indonesia) { setMonthlyRevisions(null); return () => controller.abort() }
      Promise.all([
        fetchMatrixJson(runtimeClient, malaysia.file, controller.signal),
        fetchMatrixJson(runtimeClient, `snapshots/usda_psd/${reportVersion.previousReportMonth}/${malaysia.file}`, controller.signal),
        fetchMatrixJson(runtimeClient, indonesia.file, controller.signal),
        fetchMatrixJson(runtimeClient, `snapshots/usda_psd/${reportVersion.previousReportMonth}/${indonesia.file}`, controller.signal),
      ]).then(([currentMalaysia, previousMalaysia, currentIndonesia, previousIndonesia]) => {
        setMonthlyRevisions(buildPalmG2MonthlyRevisionLookup(data, currentMalaysia, previousMalaysia, currentIndonesia, previousIndonesia))
      }).catch(() => setMonthlyRevisions(null))
    } else {
      fetchMatrixJson(runtimeClient, `snapshots/usda_psd/${reportVersion.previousReportMonth}/${matrix.file}`, controller.signal)
        .then((previous) => {
          const previousLegacyBasis = (provenance.legacyGlobalSourceBasis as Record<string, SourceBasis>)[reportVersion.previousReportMonth] ?? null
          const compatible = previous ? sourceCompatibility(data, previous, null, previousLegacyBasis).comparable : false
          setMonthlyRevisions(compatible ? buildMonthlyRevisionLookup(data, previous) : null)
        })
        .catch(() => setMonthlyRevisions(null))
    }
    return () => controller.abort()
  }, [catalog, data, reportVersion, runtimeClient, selection])

  useEffect(() => {
    if (!data) return
    const available = filterVisibleMetrics(data.rows).filter((row) => normalizeSeries(row.values, data.years.length).some((value) => value !== null))
    const preferred = ['期末库存', '库存/总使用比'].filter((name) => available.some((row) => row.name === name))
    setSelectedIndicators(preferred.length > 0 ? preferred : available.slice(0, 2).map((row) => row.name))
    setChartMode('raw')
  }, [data])

  const visibleStart = 0
  const visibleRows = data ? filterVisibleMetrics(data.rows) : []
  const selectMatrix = (matrix: MatrixReference | undefined, category?: Category) => setSelection(matrix ? { category: matrix.category, commodityDescription: matrix.commodity, countryName: matrix.country } : { category: category ?? selection?.category ?? 'Oils', commodityDescription: '', countryName: '' })
  const chooseCategory = (category: Category) => selectMatrix(catalog?.matrices.find((item) => item.category === category), category)
  const chooseCommodity = (commodityDescription: string) => {
    const matching = categoryMatrices.filter((item) => item.commodity === commodityDescription)
    const currentCountry = matching.find((item) => item.country === selection?.countryName)
    selectMatrix(currentCountry ?? matching[0])
  }
  const chooseCountry = (countryName: string) => selectMatrix(categoryMatrices.find((item) => item.commodity === selection?.commodityDescription && item.country === countryName))
  const toggleIndicator = (name: string) => setSelectedIndicators((current) => current.includes(name) ? current.filter((item) => item !== name) : [...current, name])

  if (error && !catalog) return <main className="page-state">加载失败：{error}</main>
  if (!catalog) return <main className="page-state">正在加载 USDA PS&amp;D 数据…</main>

  return <main className="dashboard"><DashboardHeader />{selection && <><SelectorPanel categories={catalog.categories} selection={selection} commodityOptions={commodityOptions} countries={countryOptions} onCategory={chooseCategory} onCommodity={chooseCommodity} onCountry={chooseCountry} /><MarketYearGuide country={selection.countryName} commodity={selection.commodityDescription} marketYear={data?.years.at(-1)} /></>}{isLoading && <LoadingState />}{error && <ErrorState message={error} />}{!isLoading && !error && !data && <EmptyState />}{data && <><AnalysisControls rows={visibleRows} selectedIndicators={selectedIndicators} chartMode={chartMode} onToggleIndicator={toggleIndicator} onChartMode={setChartMode} onDownload={() => downloadCsv(buildMetricCsv(visibleRows.filter((row) => selectedIndicators.includes(row.name)), data.years, visibleStart, monthlyRevisions), data.commodity, data.country)} /><MatrixTable data={data} rows={visibleRows} visibleStart={visibleStart} monthlyRevisions={monthlyRevisions} /><ChartPanel data={data} selectedIndicators={selectedIndicators} chartMode={chartMode} visibleStart={visibleStart} /></>}</main>
}

export default App
