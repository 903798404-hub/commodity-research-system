import { useEffect, useState } from 'react'
import { presentationLayouts, type PresentationLayoutConfig, type PresentationReportKey } from '../config/presentationLayout'
import type { Catalog, MatrixData, MatrixReference } from '../types/dashboard'
import { CompactBalanceTable } from './CompactBalanceTable'
import { getRuntimeDataClient, type RuntimeDataClient } from '../utils/runtimeDataClient'

type LoadedMatrices = Record<string, MatrixData | null>
type PresentationChanges = {
  snapshotCurrent: string | null
  snapshotPrevious: string | null
  matrices: Array<{ commodity: string; country: string; rows: Array<{ name: string; latestYoY: number | null; monthlyRevision: number | null }> }>
}

function matrixKey(commodity: string, country: string): string {
  return `${commodity}::${country}`
}

function splitIntoSlides<T>(items: T[], size: number): T[][] {
  return Array.from({ length: Math.ceil(items.length / size) }, (_, index) => items.slice(index * size, (index + 1) * size))
}

async function readJson<T>(client: RuntimeDataClient, path: string): Promise<T | null> {
  return client.readJson<T>(path)
}

function changeRows(changes: PresentationChanges | null, commodity: string, country: string) {
  return Object.fromEntries(changes?.matrices.find((item) => item.commodity === commodity && item.country === country)?.rows.map((row) => [row.name, { latestYoY: row.latestYoY, monthlyRevision: row.monthlyRevision }]) ?? [])
}

function changeNotice(years: number[], changes: PresentationChanges | null): string {
  const latest = years.at(-1)
  const previous = years.at(-2)
  if (latest === undefined || previous === undefined || !changes?.snapshotCurrent || !changes.snapshotPrevious) return '暂无可用快照比较。'
  return `最新同比：${String(latest).slice(-2)}/${String(latest + 1).slice(-2)}较${String(previous).slice(-2)}/${String(previous + 1).slice(-2)}；月修：${changes.snapshotCurrent}较${changes.snapshotPrevious}，仅比较${String(latest).slice(-2)}/${String(latest + 1).slice(-2)}年度。`
}

export function PresentationPage() {
  const [activeReport, setActiveReport] = useState<PresentationReportKey>('rapeseed')
  const [matrices, setMatrices] = useState<LoadedMatrices>({})
  const [changes, setChanges] = useState<PresentationChanges | null>(null)
  const [years, setYears] = useState<number[]>([])
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const layout: PresentationLayoutConfig = presentationLayouts.reports[activeReport]

  useEffect(() => {
    let active = true
    setStatus('loading')
    getRuntimeDataClient().then(async (client) => Promise.all([readJson<Catalog>(client, 'index.json'), readJson<PresentationChanges>(client, 'presentation_changes.json')]).then(async ([catalog, changeData]) => {
      if (!catalog) throw new Error('无法读取数据索引')
      const requested = layout.region_pairs.flatMap(({ region }) => [
        { commodity: layout.seed_commodity, country: region },
        { commodity: layout.oil_commodity, country: region },
      ])
      const references = new Map<string, MatrixReference | undefined>(requested.map(({ commodity, country }) => [matrixKey(commodity, country), catalog.matrices.find((item) => item.commodity === commodity && item.country === country)]))
      const loadedEntries = await Promise.all([...references.entries()].map(async ([key, reference]) => [key, reference ? await readJson<MatrixData>(client, reference.file) : null] as const))
      if (!active) return
      const loaded = Object.fromEntries(loadedEntries)
      const allYears = Object.values(loaded).flatMap((matrix) => matrix?.years ?? []).filter((year, index, values) => values.indexOf(year) === index).sort((a, b) => a - b)
      setMatrices(loaded)
      setYears(allYears.filter((year) => year >= layout.start_market_year))
      setChanges(changeData)
      setStatus('ready')
    })).catch(() => { if (active) setStatus('error') })
    return () => { active = false }
  }, [activeReport, layout])

  const tabs = <nav className="presentation-report-tabs" aria-label="汇总报告切换">
    {(Object.entries(presentationLayouts.reports) as Array<[PresentationReportKey, PresentationLayoutConfig]>).map(([key, report]) => <button className={key === activeReport ? 'active' : ''} key={key} onClick={() => setActiveReport(key)} type="button">{report.label}</button>)}
  </nav>

  if (status === 'loading') return <main className="presentation-shell">{tabs}<div className="presentation-canvas presentation-state">正在加载{layout.label}汇总数据…</div></main>
  if (status === 'error') return <main className="presentation-shell">{tabs}<div className="presentation-canvas presentation-state">无法加载{layout.label}汇总数据。</div></main>

  const slides = splitIntoSlides(layout.region_pairs, layout.rows_per_slide)
  const notice = changeNotice(years, changes)

  return <main className="presentation-shell">
    {tabs}
    {slides.map((regions, slideIndex) => <article className={`presentation-canvas presentation-sheet ${layout.rows_per_slide >= 4 ? 'presentation-sheet--dense' : ''}`} aria-label={`${layout.title}第${slideIndex + 1}页`} key={slideIndex}>
      <header className="presentation-header"><div><p>USDA PS&amp;D</p><h1>{layout.title}</h1></div><span>单位：万吨；库存/总使用比：%</span></header>
      <p className="presentation-market-year-note">市场年度口径说明：{layout.market_year_note}</p>
      <p className="presentation-change-note">{notice}</p>
      <div className="presentation-paired-rows">{regions.map(({ region, display_name }) => <section className="presentation-pair-row" aria-label={`${display_name}${layout.label}供需`} key={region}>
        <CompactBalanceTable title={`${layout.seed_display_name}-${display_name}`} metrics={layout.seed_metrics} years={years} matrix={matrices[matrixKey(layout.seed_commodity, region)] ?? null} changes={changeRows(changes, layout.seed_commodity, region)} showLatestYoY={layout.show_latest_yoy} showMonthlyRevision={layout.show_monthly_revision} />
        <CompactBalanceTable title={`${layout.oil_display_name}-${display_name}`} metrics={layout.oil_metrics} years={years} matrix={matrices[matrixKey(layout.oil_commodity, region)] ?? null} changes={changeRows(changes, layout.oil_commodity, region)} showLatestYoY={layout.show_latest_yoy} showMonthlyRevision={layout.show_monthly_revision} />
      </section>)}</div>
    </article>)}
  </main>
}
