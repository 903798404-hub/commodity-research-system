import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { readReportVersion } from './reportVersion'

type MatrixReference = { commodityCode: string; countryCode: string; commodity: string; country: string; category: string; file: string }
type Catalog = { matrices?: MatrixReference[] }
type MatrixRow = { name: string; values: unknown[] }
type MatrixData = { commodity: string; country: string; category: string; years: number[]; rows: MatrixRow[] }
type Revision = {
  commodity: string
  country: string
  category: string
  metric: string
  marketYear: number
  currentValue: number | null
  previousValue: number | null
  revisionValue: number | null
  comparable: boolean
  isLatestTwoMarketYears: boolean
  isAggregate: boolean
}

const SNAPSHOT_ROOT = join(process.cwd(), 'data', 'snapshots', 'usda_psd')
const OUTPUT_DIRECTORY = join(process.cwd(), 'output')
const JSON_REPORT_FILE = join(OUTPUT_DIRECTORY, 'usda_monthly_revision_report.json')
const MARKDOWN_REPORT_FILE = join(OUTPUT_DIRECTORY, 'usda_monthly_revision_report.md')

function isValidNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function readJson<T>(filePath: string): T {
  return JSON.parse(readFileSync(filePath, 'utf8')) as T
}

function matrixKey(reference: MatrixReference): string {
  return `${reference.commodityCode}|${reference.countryCode}`
}

function formatValue(value: number | null): string {
  return value === null ? '—' : value.toFixed(1)
}

function revisionTable(entries: Revision[]): string[] {
  if (entries.length === 0) return ['无。']
  return [
    '| 商品 | 国家 | 指标 | 市场年度 | 本月值 | 上月值 | 修正值 |',
    '| --- | --- | --- | ---: | ---: | ---: | ---: |',
    ...entries.map((item) => `| ${item.commodity} | ${item.country} | ${item.metric} | ${item.marketYear} | ${formatValue(item.currentValue)} | ${formatValue(item.previousValue)} | ${formatValue(item.revisionValue)} |`),
  ]
}

function buildReport(currentMonth: string, previousMonth: string, revisions: Revision[], unavailableComparisons: Revision[]) {
  const allChanges = [...revisions, ...unavailableComparisons]
  const comparable = revisions.filter((item) => item.comparable)
  const latestTwo = allChanges.filter((item) => item.isLatestTwoMarketYears)
  const aggregates = allChanges.filter((item) => item.isAggregate)
  const topAbsolute = [...comparable].filter((item) => item.revisionValue !== null).sort((a, b) => Math.abs(b.revisionValue ?? 0) - Math.abs(a.revisionValue ?? 0)).slice(0, 20)
  return {
    generatedAt: new Date().toISOString(),
    currentReportMonth: currentMonth,
    previousReportMonth: previousMonth,
    status: 'compared' as const,
    changedCount: allChanges.length,
    comparableRevisionCount: revisions.length,
    unavailableComparisonCount: unavailableComparisons.length,
    allChangedIndicators: allChanges,
    unavailableComparisons,
    latestTwoMarketYearChanges: latestTwo,
    g3AndGlobalChanges: aggregates,
    topAbsoluteChanges: topAbsolute,
  }
}

function writeBaselineReport(currentMonth: string, previousMonth: string) {
  const report = {
    generatedAt: new Date().toISOString(),
    currentReportMonth: currentMonth,
    previousReportMonth: previousMonth,
    status: 'baseline-required' as const,
    message: `缺少上月快照（${previousMonth}），当前月份将作为基准快照，暂时无法计算月度修正。`,
    changedCount: 0,
    comparableRevisionCount: 0,
    unavailableComparisonCount: 0,
    allChangedIndicators: [],
    unavailableComparisons: [],
    latestTwoMarketYearChanges: [],
    g3AndGlobalChanges: [],
    topAbsoluteChanges: [],
  }
  mkdirSync(OUTPUT_DIRECTORY, { recursive: true })
  writeFileSync(JSON_REPORT_FILE, `${JSON.stringify(report, null, 2)}\n`, 'utf8')
  writeFileSync(MARKDOWN_REPORT_FILE, `# USDA 月度修正报告\n\n- 当前报告月份：${currentMonth}\n- 上月报告月份：${previousMonth}\n- 状态：缺少上月快照\n\n${report.message}\n`, 'utf8')
  console.log(report.message)
}

function compareSnapshots() {
  const { currentReportMonth, previousReportMonth } = readReportVersion()
  const currentRoot = join(SNAPSHOT_ROOT, currentReportMonth)
  const previousRoot = join(SNAPSHOT_ROOT, previousReportMonth)
  const currentIndexFile = join(currentRoot, 'index.json')
  const previousIndexFile = join(previousRoot, 'index.json')
  if (!existsSync(currentIndexFile)) throw new Error(`缺少当前快照：${currentRoot}。请先执行 npm run build:data。`)
  if (!existsSync(previousIndexFile)) {
    writeBaselineReport(currentReportMonth, previousReportMonth)
    return
  }

  const currentIndex = readJson<Catalog>(currentIndexFile)
  const previousIndex = readJson<Catalog>(previousIndexFile)
  const previousReferences = new Map((previousIndex.matrices ?? []).map((item) => [matrixKey(item), item]))
  const revisions: Revision[] = []
  const unavailableComparisons: Revision[] = []

  for (const currentReference of currentIndex.matrices ?? []) {
    const previousReference = previousReferences.get(matrixKey(currentReference))
    if (!previousReference) continue
    const currentMatrixFile = join(currentRoot, currentReference.file)
    const previousMatrixFile = join(previousRoot, previousReference.file)
    if (!existsSync(currentMatrixFile) || !existsSync(previousMatrixFile)) continue
    const currentMatrix = readJson<MatrixData>(currentMatrixFile)
    const previousMatrix = readJson<MatrixData>(previousMatrixFile)
    const previousRows = new Map(previousMatrix.rows.map((row) => [row.name, row]))
    const latestYears = new Set(currentMatrix.years.slice(-2))

    for (const currentRow of currentMatrix.rows) {
      const previousRow = previousRows.get(currentRow.name)
      if (!previousRow) continue
      for (const [index, marketYear] of currentMatrix.years.entries()) {
        const previousIndexForYear = previousMatrix.years.indexOf(marketYear)
        if (previousIndexForYear < 0) continue
        const currentValue = isValidNumber(currentRow.values[index]) ? currentRow.values[index] : null
        const previousValue = isValidNumber(previousRow.values[previousIndexForYear]) ? previousRow.values[previousIndexForYear] : null
        if (currentValue === previousValue) continue
        const comparable = currentValue !== null && previousValue !== null
        const revision: Revision = {
          commodity: currentMatrix.commodity,
          country: currentMatrix.country,
          category: currentMatrix.category,
          metric: currentRow.name,
          marketYear,
          currentValue,
          previousValue,
          revisionValue: comparable ? currentValue - previousValue : null,
          comparable,
          isLatestTwoMarketYears: latestYears.has(marketYear),
          isAggregate: currentMatrix.country === 'G3' || currentMatrix.country === 'Global',
        }
        if (comparable) revisions.push(revision)
        else unavailableComparisons.push(revision)
      }
    }
  }

  const report = buildReport(currentReportMonth, previousReportMonth, revisions, unavailableComparisons)
  mkdirSync(OUTPUT_DIRECTORY, { recursive: true })
  writeFileSync(JSON_REPORT_FILE, `${JSON.stringify(report, null, 2)}\n`, 'utf8')
  const lines = [
    '# USDA 月度修正报告',
    '',
    `- 当前报告月份：${currentReportMonth}`,
    `- 上月报告月份：${previousReportMonth}`,
    `- 全部有变化的指标：${report.changedCount}`,
    `- 数值可比较的修正项：${report.comparableRevisionCount}`,
    `- 因本月或上月为空而无法计算数值修正的项：${report.unavailableComparisonCount}`,
    '', '## 所有有变化的指标', '', ...revisionTable(report.allChangedIndicators),
    '', '## 最新两个市场年度的变化', '', ...revisionTable(report.latestTwoMarketYearChanges),
    '', '## G3 和 Global 的变化', '', ...revisionTable(report.g3AndGlobalChanges),
    '', '## 绝对值变化前 20 条', '', ...revisionTable(report.topAbsoluteChanges),
  ]
  writeFileSync(MARKDOWN_REPORT_FILE, `${lines.join('\n')}\n`, 'utf8')
  console.log(`月度修正比较完成：${report.changedCount} 个有变化指标，其中 ${report.comparableRevisionCount} 个可比较修正项，${report.unavailableComparisonCount} 个空值差异项。`)
}

try {
  compareSnapshots()
} catch (error: unknown) {
  console.error(error instanceof Error ? error.message : error)
  process.exitCode = 1
}
