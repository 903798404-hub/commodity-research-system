import { copyFileSync, createReadStream, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { basename, join } from 'node:path'
import Papa from 'papaparse'
import { G3_COMMODITIES, G3_COUNTRIES, isResearchCombination, RESEARCH_SCOPE, researchScopeReportLines } from './researchScope'
import { readReportVersion } from './reportVersion'
import { readUsdaApiConfig } from './usdaApiConfig'

type Category = 'Oilseeds' | 'Oils' | 'Meals'

type RawRecord = {
  Commodity_Code?: string | number
  Commodity_Description?: string | number
  Country_Code?: string | number
  Country_Name?: string | number
  Market_Year?: string | number
  Attribute_Description?: string | number
  Unit_Description?: string | number
  Value?: string | number
}

type StoredValue = { value: number; unit: string }
type DataRow = { name: string; values: Array<number | null> }
type MatrixData = {
  commodityCode: string
  commodity: string
  category: Category
  countryCode: string
  country: string
  years: number[]
  rows: DataRow[]
}

type IndexData = {
  categories: Category[]
  commodities: Array<{ commodityCode: string; commodityDescription: string; category: Category; displayName: string }>
  countries: Array<{ countryCode: string; countryName: string; displayName?: string }>
  matrices: Array<{ category: Category; commodityCode: string; commodity: string; countryCode: string; country: string; file: string }>
  defaultSelection: { category: 'Oils'; commodityDescription: 'Oil, Soybean'; countryName: 'United States' }
}

type MatrixStore = {
  commodityCode: string
  commodity: string
  category: Category
  countryCode: string
  country: string
  attributes: Map<string, Map<number, StoredValue>>
}

type GlobalSourceType = 'USDA World' | 'synthetic sum'
type GlobalSourceCountryAudit = {
  countryCode: string
  country: string
  hasValidYearData: boolean
  validYears: number[]
}
type GlobalAggregationAudit = {
  commodityCode: string
  commodity: string
  category: Category
  sourceType: GlobalSourceType
  sourceCountryCount: number
  sourceCountries: GlobalSourceCountryAudit[]
  warnings: string[]
}

type G2AggregationAudit = {
  created: boolean
  sources: string[]
  commonValidYears: number[]
  singleSidedMissing: Array<{ attribute: string; years: number[] }>
}

const RAW_DIRECTORY = join(process.cwd(), 'raw')
const VERSIONED_RAW_DIRECTORY = join(process.cwd(), 'data', 'raw', 'usda_psd')
const API_RAW_DIRECTORY = join(process.cwd(), 'data', 'raw', 'usda_psd_api')
const SNAPSHOT_DIRECTORY = join(process.cwd(), 'data', 'snapshots', 'usda_psd')
const DATA_DIRECTORY = join(process.cwd(), 'public', 'data')
const MATRIX_DIRECTORY = join(DATA_DIRECTORY, 'matrix')
const PUBLIC_SNAPSHOT_DIRECTORY = join(DATA_DIRECTORY, 'snapshots', 'usda_psd')
const REPORT_FILE = join(process.cwd(), 'output', 'data_build_report.md')
const GLOBAL_AGGREGATION_REPORT_MARKDOWN_FILE = join(process.cwd(), 'output', 'global_aggregation_report.md')
const GLOBAL_AGGREGATION_REPORT_JSON_FILE = join(process.cwd(), 'output', 'global_aggregation_report.json')
const CATEGORIES: Category[] = ['Oilseeds', 'Oils', 'Meals']
const MIN_YEAR = 2018
const NON_ADDITIVE_ATTRIBUTES = new Set(['Yield'])
const G2_PALM_COMMODITY = 'Oil, Palm'
const G2_PALM_COUNTRIES = ['Malaysia', 'Indonesia'] as const
const G2_ADDITIVE_ATTRIBUTES = [
  'Beginning Stocks', 'Production', 'Imports', 'Exports', 'Domestic Consumption',
  'Industrial Dom. Cons.', 'Food Use Dom. Cons.', 'Feed Waste Dom. Cons.',
  'Ending Stocks', 'Total Distribution',
]
const G2_CORE_ATTRIBUTES = ['Production', 'Imports', 'Exports', 'Domestic Consumption', 'Ending Stocks']
const EUROPEAN_UNION_MEMBER_NAMES = new Set([
  'Austria', 'Belgium', 'Bulgaria', 'Croatia', 'Cyprus', 'Czech Republic', 'Denmark', 'Estonia', 'Finland',
  'France', 'Germany', 'Greece', 'Hungary', 'Ireland', 'Italy', 'Latvia', 'Lithuania', 'Luxembourg',
  'Malta', 'Netherlands', 'Poland', 'Portugal', 'Romania', 'Slovakia', 'Slovenia', 'Spain', 'Sweden',
])

const ATTRIBUTE_NAMES: Array<{ source: string; name: string }> = [
  { source: 'Beginning Stocks', name: '期初库存' },
  { source: 'Production', name: '产量' },
  { source: 'Imports', name: '进口量' },
  { source: 'Crush', name: '压榨量' },
  { source: 'Exports', name: '出口量' },
  { source: 'Domestic Consumption', name: '消费量' },
  { source: 'Industrial Dom. Cons.', name: '工业消费' },
  { source: 'Food Use Dom. Cons.', name: '食用消费' },
  { source: 'Feed Waste Dom. Cons.', name: '饲用及损耗消费' },
  { source: 'Ending Stocks', name: '期末库存' },
  { source: 'Area Harvested', name: '收获面积' },
  { source: 'Yield', name: '单产' },
]

const UPSTREAM_RULES: Record<string, { oilseed: string; production: string; crush: string }> = {
  'Oil, Soybean': { oilseed: 'Oilseed, Soybean', production: '大豆产量', crush: '大豆压榨' },
  'Meal, Soybean': { oilseed: 'Oilseed, Soybean', production: '大豆产量', crush: '大豆压榨' },
  'Oil, Rapeseed': { oilseed: 'Oilseed, Rapeseed', production: '菜籽产量', crush: '菜籽压榨' },
  'Meal, Rapeseed': { oilseed: 'Oilseed, Rapeseed', production: '菜籽产量', crush: '菜籽压榨' },
  'Oil, Sunflowerseed': { oilseed: 'Oilseed, Sunflowerseed', production: '葵籽产量', crush: '葵籽压榨' },
  'Meal, Sunflowerseed': { oilseed: 'Oilseed, Sunflowerseed', production: '葵籽产量', crush: '葵籽压榨' },
  'Oil, Cottonseed': { oilseed: 'Oilseed, Cottonseed', production: '棉籽产量', crush: '棉籽压榨' },
  'Meal, Cottonseed': { oilseed: 'Oilseed, Cottonseed', production: '棉籽产量', crush: '棉籽压榨' },
  'Oil, Peanut': { oilseed: 'Oilseed, Peanut', production: '花生产量', crush: '花生压榨' },
  'Meal, Peanut': { oilseed: 'Oilseed, Peanut', production: '花生产量', crush: '花生压榨' },
  'Oil, Palm Kernel': { oilseed: 'Oilseed, Palm Kernel', production: '棕榈仁产量', crush: '棕榈仁压榨' },
  'Meal, Palm Kernel': { oilseed: 'Oilseed, Palm Kernel', production: '棕榈仁产量', crush: '棕榈仁压榨' },
  'Oil, Copra': { oilseed: 'Oilseed, Copra', production: '椰干产量', crush: '椰干压榨' },
  'Meal, Copra': { oilseed: 'Oilseed, Copra', production: '椰干产量', crush: '椰干压榨' },
}

function categoryFor(commodity: string): Category | null {
  if (commodity.startsWith('Oilseed,')) return 'Oilseeds'
  if (commodity.startsWith('Oil,')) return 'Oils'
  if (commodity.startsWith('Meal,')) return 'Meals'
  return null
}

function parseMarketYear(value: string | number | undefined): number | null {
  const match = String(value ?? '').match(/^(\d{4})/)
  return match ? Number(match[1]) : null
}

function roundOneDecimal(value: number): number {
  return Math.round((value + Number.EPSILON) * 10) / 10
}

function isValidStoredValue(value: StoredValue | undefined): value is StoredValue {
  return value !== undefined && Number.isFinite(value.value)
}

function hasMatchingUnits(values: StoredValue[]): boolean {
  return values.length > 0 && values.every((value) => value.unit === values[0].unit)
}

function normalizeValue(record: RawRecord): StoredValue | null {
  const rawValue = Number(record.Value)
  if (!Number.isFinite(rawValue)) return null
  const unit = String(record.Unit_Description ?? '').trim()
  const value = unit === '(1000 MT)' || unit === '1000 MT' ? rawValue / 10 : rawValue
  return { value: roundOneDecimal(value), unit }
}

function matrixKey(commodityCode: string, countryCode: string): string {
  return `${commodityCode}|${countryCode}`
}

function safeFileName(commodityCode: string, countryCode: string): string {
  return `${commodityCode.replace(/[^a-z0-9_-]/gi, '_')}_${countryCode.replace(/[^a-z0-9_-]/gi, '_')}.json`
}

function countryDisplayName(countryCode: string, country: string): string | undefined {
  return countryCode === 'G2' && country === 'G2' ? 'G2（马来西亚 + 印度尼西亚）' : undefined
}

function findSingleSourceFile(reportMonth: string): string {
  const versionedDirectory = join(VERSIONED_RAW_DIRECTORY, reportMonth)
  const sourceDirectory = existsSync(versionedDirectory) ? versionedDirectory : RAW_DIRECTORY
  if (!existsSync(sourceDirectory)) throw new Error('未找到 raw 文件夹或当前报告月份的数据目录。')
  const files = readdirSync(sourceDirectory).filter((file) => file.toLowerCase().endsWith('.csv'))
  if (files.length === 0) throw new Error('raw 文件夹中没有 CSV 文件。')
  if (files.length > 1) throw new Error(`raw 文件夹中有多个 CSV 文件：${files.join(', ')}`)
  return join(sourceDirectory, files[0])
}

function saveProcessedSnapshot(reportMonth: string) {
  const snapshotRoot = join(SNAPSHOT_DIRECTORY, reportMonth)
  const snapshotMatrixDirectory = join(snapshotRoot, 'matrix')
  rmSync(snapshotRoot, { recursive: true, force: true })
  mkdirSync(snapshotMatrixDirectory, { recursive: true })
  copyFileSync(join(DATA_DIRECTORY, 'index.json'), join(snapshotRoot, 'index.json'))
  for (const file of readdirSync(MATRIX_DIRECTORY)) {
    if (file.toLowerCase().endsWith('.json')) copyFileSync(join(MATRIX_DIRECTORY, file), join(snapshotMatrixDirectory, file))
  }
}

function publishSnapshotForFrontend(reportMonth: string) {
  const sourceRoot = join(SNAPSHOT_DIRECTORY, reportMonth)
  const sourceMatrixDirectory = join(sourceRoot, 'matrix')
  if (!existsSync(join(sourceRoot, 'index.json')) || !existsSync(sourceMatrixDirectory)) return
  const targetRoot = join(PUBLIC_SNAPSHOT_DIRECTORY, reportMonth)
  const targetMatrixDirectory = join(targetRoot, 'matrix')
  mkdirSync(targetMatrixDirectory, { recursive: true })
  copyFileSync(join(sourceRoot, 'index.json'), join(targetRoot, 'index.json'))
  for (const file of readdirSync(sourceMatrixDirectory)) {
    if (file.toLowerCase().endsWith('.json')) copyFileSync(join(sourceMatrixDirectory, file), join(targetMatrixDirectory, file))
  }
}

function synchronizeSnapshotRatioRows(rootDirectory: string) {
  if (!existsSync(rootDirectory)) return
  const entries = readdirSync(rootDirectory, { withFileTypes: true })
  for (const entry of entries) {
    const entryPath = join(rootDirectory, entry.name)
    if (entry.isDirectory()) {
      synchronizeSnapshotRatioRows(entryPath)
      continue
    }
    if (!entry.name.toLowerCase().endsWith('.json') || entry.name === 'index.json') continue
    const matrix = JSON.parse(readFileSync(entryPath, 'utf8')) as MatrixData
    if (!Array.isArray(matrix.rows) || !Array.isArray(matrix.years)) continue

    const ratioRow = matrix.rows.find((row) => row.name === '库存/总使用比')
    const endingStocks = matrix.rows.find((row) => row.name === '期末库存')
    const domesticConsumption = matrix.rows.find((row) => row.name === '消费量')
    const exports = matrix.rows.find((row) => row.name === '出口量')
    const recalculated = matrix.years.map((_, index) => {
      const ending = endingStocks?.values[index]
      const domestic = domesticConsumption?.values[index]
      const exportValue = exports?.values[index]
      const primaryTotalUse = typeof domestic === 'number' && Number.isFinite(domestic) && typeof exportValue === 'number' && Number.isFinite(exportValue)
        ? domestic + exportValue
        : null
      if (typeof ending === 'number' && Number.isFinite(ending) && primaryTotalUse !== null && primaryTotalUse > 0) return roundOneDecimal((ending / primaryTotalUse) * 100)
      const existing = ratioRow?.values[index]
      return typeof existing === 'number' && Number.isFinite(existing) ? existing : null
    })
    matrix.rows = matrix.rows.filter((row) => row.name !== '期末库销比' && row.name !== '库存/国内消费比' && row.name !== '库存/总使用比')
    if (hasAnyValue(recalculated)) matrix.rows.push({ name: '库存/总使用比', values: recalculated })
    writeFileSync(entryPath, `${JSON.stringify(matrix, null, 2)}\n`, 'utf8')
  }
}

type MatrixCollection = { matrices: Map<string, MatrixStore>; unknownCommodities: Set<string>; missingKeyFields: number }

function createMatrixCollection(): MatrixCollection {
  return { matrices: new Map<string, MatrixStore>(), unknownCommodities: new Set<string>(), missingKeyFields: 0 }
}

function ingestRecord(record: RawRecord, collection: MatrixCollection) {
  const commodity = String(record.Commodity_Description ?? '').trim()
  const category = commodity ? categoryFor(commodity) : null
  if (!category) {
    if (commodity) collection.unknownCommodities.add(commodity)
    return
  }

  const commodityCode = String(record.Commodity_Code ?? '').trim()
  const countryCode = String(record.Country_Code ?? '').trim()
  const country = String(record.Country_Name ?? '').trim()
  const attribute = String(record.Attribute_Description ?? '').trim()
  const year = parseMarketYear(record.Market_Year)
  const normalized = normalizeValue(record)
  if (!commodityCode || !countryCode || !country || !attribute || year === null || !normalized) {
    collection.missingKeyFields += 1
    return
  }

  const key = matrixKey(commodityCode, countryCode)
  const store = collection.matrices.get(key) ?? {
    commodityCode,
    commodity,
    category,
    countryCode,
    country,
    attributes: new Map<string, Map<number, StoredValue>>(),
  }
  const values = store.attributes.get(attribute) ?? new Map<number, StoredValue>()
  values.set(year, normalized)
  store.attributes.set(attribute, values)
  collection.matrices.set(key, store)
}

async function readMatrices(filePath: string): Promise<MatrixCollection> {
  const collection = createMatrixCollection()

  await new Promise<void>((resolve, reject) => {
    Papa.parse<RawRecord>(createReadStream(filePath), {
      header: true,
      skipEmptyLines: true,
      step: (result) => {
        if (result.errors.length > 0) {
          reject(new Error(`CSV 解析失败：${result.errors[0].message}`))
          return
        }
        ingestRecord(result.data, collection)
      },
      complete: () => resolve(),
      error: (error) => reject(error),
    })
  })
  return collection
}

function apiField(record: Record<string, unknown>, fields: string[]): string | number | undefined {
  const normalized = new Map(Object.entries(record).map(([key, value]) => [key.replace(/[^a-z0-9]/gi, '').toLowerCase(), value]))
  for (const field of fields) {
    const value = normalized.get(field.replace(/[^a-z0-9]/gi, '').toLowerCase())
    if (typeof value === 'string' || typeof value === 'number') return value
  }
  return undefined
}

function apiItems(value: unknown): Record<string, unknown>[] {
  if (Array.isArray(value)) return value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object')
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>
    for (const key of ['data', 'Data', 'items', 'Items', 'results', 'Results']) if (Array.isArray(record[key])) return record[key].filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object')
  }
  return []
}

function apiMetadataLookup(filePath: string, codeFields: string[], nameFields: string[]): Map<string, string> {
  const parsed = JSON.parse(readFileSync(filePath, 'utf8')) as unknown
  const lookup = new Map<string, string>()
  for (const item of apiItems(parsed)) {
    const code = apiField(item, codeFields)
    const name = apiField(item, nameFields)
    if (code !== undefined && name !== undefined) lookup.set(String(code), String(name))
  }
  return lookup
}

function readApiMatrices(directory: string): MatrixCollection {
  const collection = createMatrixCollection()
  const metadataDirectory = join(directory, '..', 'metadata')
  const commodityNames = apiMetadataLookup(join(metadataDirectory, 'commodities.json'), ['commodityCode'], ['commodityName', 'commodityDescription'])
  const countryNames = apiMetadataLookup(join(metadataDirectory, 'countries.json'), ['countryCode'], ['countryName'])
  const attributeNames = apiMetadataLookup(join(metadataDirectory, 'commodityAttributes.json'), ['attributeId'], ['attributeName', 'attributeDescription'])
  const unitNames = apiMetadataLookup(join(metadataDirectory, 'unitsOfMeasure.json'), ['unitId'], ['unitDescription'])
  for (const file of readdirSync(directory).filter((name) => name.toLowerCase().endsWith('.json'))) {
    const parts = file.replace(/\.json$/i, '').split('_')
    const isWorld = parts[1] === 'WORLD'
    const fallbackCommodityCode = parts[0]
    const fallbackCountryCode = isWorld ? 'WORLD' : parts[1]
    const fallbackYear = parts.at(-1)
    const fallbackCountry = isWorld ? 'World' : undefined
    const parsed = JSON.parse(readFileSync(join(directory, file), 'utf8')) as unknown
    for (const item of apiItems(parsed)) {
      ingestRecord({
        Commodity_Code: apiField(item, ['commodityCode']) ?? fallbackCommodityCode,
        Commodity_Description: apiField(item, ['commodityDescription', 'commodityName']) ?? commodityNames.get(String(apiField(item, ['commodityCode']) ?? fallbackCommodityCode)),
        Country_Code: isWorld ? fallbackCountryCode : apiField(item, ['countryCode']) ?? fallbackCountryCode,
        Country_Name: isWorld ? fallbackCountry : apiField(item, ['countryName']) ?? countryNames.get(String(apiField(item, ['countryCode']) ?? fallbackCountryCode)),
        Market_Year: apiField(item, ['marketYear']) ?? fallbackYear,
        Attribute_Description: apiField(item, ['attributeDescription', 'attributeName']) ?? attributeNames.get(String(apiField(item, ['attributeId']) ?? '')),
        Unit_Description: apiField(item, ['unitDescription']) ?? unitNames.get(String(apiField(item, ['unitId']) ?? '')),
        Value: apiField(item, ['value']),
      }, collection)
    }
  }
  return collection
}

function valueSeries(values: Map<number, StoredValue> | undefined, years: number[]): Array<number | null> {
  return years.map((year) => values?.get(year)?.value ?? null)
}

function hasAnyValue(values: Array<number | null>): boolean {
  return values.some((value) => value !== null)
}

function buildMatrix(store: MatrixStore, allMatrices: Map<string, MatrixStore>): MatrixData {
  const yearSet = new Set<number>()
  for (const values of store.attributes.values()) for (const year of values.keys()) yearSet.add(year)
  const years = [...yearSet].filter((year) => year >= MIN_YEAR).sort((a, b) => a - b)
  const rows: DataRow[] = []
  const upstream = UPSTREAM_RULES[store.commodity]

  if ((store.category === 'Oils' || store.category === 'Meals') && upstream) {
    const source = [...allMatrices.values()].find((candidate) => candidate.commodity === upstream.oilseed && candidate.countryCode === store.countryCode)
    if (source) {
      for (const upstreamMetric of [{ source: 'Production', name: upstream.production }, { source: 'Crush', name: upstream.crush }]) {
        const values = valueSeries(source.attributes.get(upstreamMetric.source), years)
        if (hasAnyValue(values)) rows.push({ name: upstreamMetric.name, values })
      }
    }
  }

  for (const metric of ATTRIBUTE_NAMES) {
    const values = valueSeries(store.attributes.get(metric.source), years)
    if (hasAnyValue(values)) rows.push({ name: metric.name, values })
  }

  const stockToTotalUse = years.map((year) => stockToTotalUseAtYear(store, year).ratio)
  if (hasAnyValue(stockToTotalUse)) rows.push({ name: '库存/总使用比', values: stockToTotalUse })

  return { commodityCode: store.commodityCode, commodity: store.commodity, category: store.category, countryCode: store.countryCode, country: store.country, years, rows }
}

function cloneAsVirtualStore(source: MatrixStore, countryCode: string, country: string): MatrixStore {
  return { ...source, countryCode, country }
}

function aggregateStores(sources: MatrixStore[], commodityCode: string, commodity: string, category: Category, countryCode: string, country: string): MatrixStore | null {
  if (sources.length === 0) return null
  const attributes = new Map<string, Map<number, StoredValue>>()
  const attributeNames = new Set(sources.flatMap((source) => [...source.attributes.keys()]))
  for (const attribute of attributeNames) {
    if (NON_ADDITIVE_ATTRIBUTES.has(attribute)) continue
    const years = new Set<number>()
    for (const source of sources) for (const year of source.attributes.get(attribute)?.keys() ?? []) years.add(year)
    const totals = new Map<number, StoredValue>()
    for (const year of years) {
      const values = sources.map((source) => source.attributes.get(attribute)?.get(year)).filter((value): value is StoredValue => Boolean(value) && Number.isFinite(value.value))
      if (values.length > 0) totals.set(year, { value: roundOneDecimal(values.reduce((sum, value) => sum + value.value, 0)), unit: values[0].unit })
    }
    if (totals.size > 0) attributes.set(attribute, totals)
  }
  return { commodityCode, commodity, category, countryCode, country, attributes }
}

function aggregatePalmG2(sources: MatrixStore[]): { store: MatrixStore | null; audit: G2AggregationAudit } {
  const sourceByCountry = new Map(sources.map((source) => [source.country, source]))
  const malaysia = sourceByCountry.get('Malaysia')
  const indonesia = sourceByCountry.get('Indonesia')
  const audit: G2AggregationAudit = { created: false, sources: [...G2_PALM_COUNTRIES], commonValidYears: [], singleSidedMissing: [] }
  if (!malaysia || !indonesia || malaysia.commodity !== G2_PALM_COMMODITY || indonesia.commodity !== G2_PALM_COMMODITY) return { store: null, audit }

  const sourcePair = [malaysia, indonesia]
  const attributes = new Map<string, Map<number, StoredValue>>()
  for (const attribute of G2_ADDITIVE_ATTRIBUTES) {
    const years = new Set<number>()
    for (const source of sourcePair) for (const year of source.attributes.get(attribute)?.keys() ?? []) years.add(year)
    const totals = new Map<number, StoredValue>()
    const missingYears: number[] = []
    for (const year of years) {
      const values = sourcePair.map((source) => source.attributes.get(attribute)?.get(year))
      if (values.every(isValidStoredValue) && hasMatchingUnits(values)) {
        totals.set(year, { value: roundOneDecimal(values.reduce((sum, value) => sum + value.value, 0)), unit: values[0].unit })
      } else if (values.some(isValidStoredValue)) {
        missingYears.push(year)
      }
    }
    if (totals.size > 0) attributes.set(attribute, totals)
    if (missingYears.length > 0) audit.singleSidedMissing.push({ attribute, years: missingYears.sort((a, b) => a - b) })
  }

  const allYears = new Set<number>()
  for (const source of sourcePair) for (const attribute of G2_CORE_ATTRIBUTES) for (const year of source.attributes.get(attribute)?.keys() ?? []) allYears.add(year)
  audit.commonValidYears = [...allYears].filter((year) => G2_CORE_ATTRIBUTES.every((attribute) => {
    const values = sourcePair.map((source) => source.attributes.get(attribute)?.get(year))
    return values.every(isValidStoredValue) && hasMatchingUnits(values)
  })).filter((year) => year >= MIN_YEAR).sort((a, b) => a - b)
  audit.created = attributes.size > 0

  return {
    store: audit.created
      ? { commodityCode: malaysia.commodityCode, commodity: G2_PALM_COMMODITY, category: malaysia.category, countryCode: 'G2', country: 'G2', attributes }
      : null,
    audit,
  }
}

function stockToTotalUseAtYear(store: MatrixStore, year: number): { totalUse: number | null; ratio: number | null } {
  const ending = store.attributes.get('Ending Stocks')?.get(year)?.value
  const domestic = store.attributes.get('Domestic Consumption')?.get(year)?.value
  const exportValue = store.attributes.get('Exports')?.get(year)?.value
  const primaryTotalUse = domestic !== undefined && exportValue !== undefined ? domestic + exportValue : null
  const totalDistribution = store.attributes.get('Total Distribution')?.get(year)?.value
  const fallbackTotalUse = ending !== undefined && totalDistribution !== undefined ? totalDistribution - ending : null
  const totalUse = primaryTotalUse !== null && Number.isFinite(primaryTotalUse) && primaryTotalUse > 0
    ? primaryTotalUse
    : fallbackTotalUse !== null && Number.isFinite(fallbackTotalUse) && fallbackTotalUse > 0 ? fallbackTotalUse : null
  return { totalUse, ratio: ending !== undefined && totalUse !== null ? roundOneDecimal((ending / totalUse) * 100) : null }
}

function isWorldStore(store: MatrixStore): boolean {
  return /(^|\s)(world|global)(\s|$)/i.test(store.country)
}

function validYearsForStore(store: MatrixStore): number[] {
  const years = new Set<number>()
  for (const values of store.attributes.values()) {
    for (const [year, storedValue] of values) {
      if (year >= MIN_YEAR && Number.isFinite(storedValue.value)) years.add(year)
    }
  }
  return [...years].sort((a, b) => a - b)
}

function europeanUnionOverlapWarning(sourceCountries: GlobalSourceCountryAudit[]): string[] {
  const hasEuropeanUnion = sourceCountries.some(({ country }) => /^(European Union|EU-?27|European Union-?27)$/i.test(country))
  if (!hasEuropeanUnion) return []

  const members = sourceCountries
    .filter(({ country, hasValidYearData }) => hasValidYearData && EUROPEAN_UNION_MEMBER_NAMES.has(country))
    .map(({ country }) => country)
    .sort((a, b) => a.localeCompare(b))
  return members.length > 0
    ? [`潜在区域重复风险：来源同时包含 European Union 与其成员国（${members.join('、')}）。synthetic Global 会按原始记录口径汇总，研究使用时请注意该重叠。`]
    : []
}

function createGlobalAggregationAudit(sources: MatrixStore[], sourceType: GlobalSourceType): GlobalAggregationAudit | null {
  const source = sources[0]
  if (!source) return null
  const sourceCountries = sources
    .map((store) => {
      const validYears = validYearsForStore(store)
      return { countryCode: store.countryCode, country: store.country, hasValidYearData: validYears.length > 0, validYears }
    })
    .sort((a, b) => a.country.localeCompare(b.country) || a.countryCode.localeCompare(b.countryCode))
  return {
    commodityCode: source.commodityCode,
    commodity: source.commodity,
    category: source.category,
    sourceType,
    sourceCountryCount: sourceCountries.length,
    sourceCountries,
    warnings: sourceType === 'synthetic sum' ? europeanUnionOverlapWarning(sourceCountries) : [],
  }
}

function writeGlobalAggregationReport(audits: GlobalAggregationAudit[]) {
  mkdirSync(join(process.cwd(), 'output'), { recursive: true })
  const report = {
    generatedAt: new Date().toISOString(),
    minimumMarketYear: MIN_YEAR,
    globalDefinition: 'USDA World 优先；若不存在则 synthetic sum（该商品全部原始国家记录汇总）。',
    commodities: audits,
  }
  writeFileSync(GLOBAL_AGGREGATION_REPORT_JSON_FILE, `${JSON.stringify(report, null, 2)}\n`, 'utf8')

  const lines = [
    '# Global 聚合来源审计报告',
    '',
    `- 生成时间：${report.generatedAt}`,
    `- 有效年份：Market_Year >= ${MIN_YEAR}`,
    '- Global 口径：优先使用 USDA World；没有 World 时使用同一商品全部原始国家记录的 synthetic sum。',
    '- 本报告只审计 Global 的来源；不修改原始 USDA 数据，也不影响前端加载逻辑。',
  ]
  for (const audit of audits) {
    lines.push('', `## ${audit.commodity}`, '', `- 来源类型：${audit.sourceType}`, `- 实际参与来源国家数：${audit.sourceCountryCount}`)
    if (audit.warnings.length === 0) lines.push('- 区域重复风险：未发现明显区域重复风险。')
    else lines.push(...audit.warnings.map((warning) => `- warning：${warning}`))
    lines.push('', '| Country Code | 来源国家 | 是否有 2018 年及以后有效数据 | 有效年份 |', '| --- | --- | --- | --- |')
    for (const country of audit.sourceCountries) {
      lines.push(`| ${country.countryCode} | ${country.country} | ${country.hasValidYearData ? '是' : '否'} | ${country.validYears.join(', ') || '—'} |`)
    }
  }
  writeFileSync(GLOBAL_AGGREGATION_REPORT_MARKDOWN_FILE, `${lines.join('\n')}\n`, 'utf8')
}

function writeReport(sourceFile: string, reportMonth: string, index: IndexData, matrixCount: number, defaultCreated: boolean, unknownCommodities: Set<string>, missingKeyFields: number, globalSources: Map<string, GlobalSourceType>, g2Store: MatrixStore | null, g2Audit: G2AggregationAudit, g2Sources: MatrixStore[]) {
  mkdirSync(join(process.cwd(), 'output'), { recursive: true })
  const lines = [
    '# 数据构建报告',
    '',
    `- 原始 CSV：\`${basename(sourceFile)}\``,
    `- 当前报告月份：${reportMonth}`,
    `- 已保存处理后快照：\`data/snapshots/usda_psd/${reportMonth}/\``,
    '- 数据模式：研究白名单模式，仅生成核心油脂油粕商品与指定国家组合。',
    `- 年份过滤：仅保留 Market_Year >= ${MIN_YEAR} 的数据`,
    `- 生成商品数：${index.commodities.length}`,
    `- 生成国家数：${index.countries.length}`,
    `- 生成 matrix JSON 数：${matrixCount}`,
    `- Oil, Soybean + United States：${defaultCreated ? '成功生成' : '未生成'}`,
    `- 无法识别 category 的 Commodity_Description：${unknownCommodities.size}${unknownCommodities.size ? `（${[...unknownCommodities].slice(0, 20).join('；')}）` : ''}`,
    `- 缺少关键字段的数据行：${missingKeyFields}`,
    '',
    '## 研究白名单',
    '',
    ...researchScopeReportLines(),
    '',
    '## 聚合口径',
    '',
    '- G3 = United States + Brazil + Argentina，仅用于 Oilseed, Soybean / Meal, Soybean / Oil, Soybean；库存/总使用比按 G3 期末库存 ÷（G3 国内消费 + G3 出口）重新计算，缺失时回退为 G3 期末库存 ÷（G3 总分配 - G3 期末库存）。',
    '- Global 优先使用 USDA World 原始口径；本次未发现可识别 World/Global 原始国家记录，以下 Global 均为 synthetic sum（原始数据全部国家记录汇总）。',
    ...[...globalSources.entries()].map(([commodity, source]) => `- ${commodity}: ${source}`),
    '',
    '## 棕榈油 G2 聚合口径',
    '',
    `- Oil, Palm + G2：${g2Audit.created ? '成功生成' : '未生成'}`,
    '- G2 = Malaysia + Indonesia；仅用于 Oil, Palm。',
    `- 双方共同有效 Market_Year 数量（Production、Imports、Exports、Domestic Consumption、Ending Stocks 均有效且单位一致）：${g2Audit.commonValidYears.length}${g2Audit.commonValidYears.length ? `（${g2Audit.commonValidYears.join('、')}）` : ''}`,
    `- 单边缺失或单位不一致：${g2Audit.singleSidedMissing.length === 0 ? '未发现' : '存在，详见下表'}`,
    '',
    ...(g2Audit.singleSidedMissing.length === 0 ? [] : [
      '| 指标 | 单边缺失或单位不一致的年份 |',
      '| --- | --- |',
      ...g2Audit.singleSidedMissing.map((item) => `| ${item.attribute} | ${item.years.join('、')} |`),
      '',
    ]),
    ...g2ReportLines(g2Store, g2Audit.commonValidYears, g2Sources),
    '',
    '说明：matrix 文件按 Commodity_Code + Country_Code 拆分；数量单位为 (1000 MT) 或 1000 MT 时已转换为万吨。',
  ]
  writeFileSync(REPORT_FILE, `${lines.join('\n')}\n`, 'utf8')
}

function g2ReportLines(store: MatrixStore | null, commonValidYears: number[], sources: MatrixStore[]): string[] {
  if (!store || commonValidYears.length === 0) return ['- 无可用于关键指标核对的共同有效年份。']
  const latestYear = commonValidYears.at(-1)!
  const latest = (attribute: string) => store.attributes.get(attribute)?.get(latestYear)?.value ?? null
  const totalUse = stockToTotalUseAtYear(store, latestYear)
  const samples = commonValidYears.slice(0, 3)
  return [
    `- 最新共同有效年度：${latestYear}`,
    `- 最新共同有效年度关键数据（万吨）：Production ${latest('Production')}；Imports ${latest('Imports')}；Exports ${latest('Exports')}；Domestic Consumption ${latest('Domestic Consumption')}；Ending Stocks ${latest('Ending Stocks')}；Total Use ${totalUse.totalUse ?? '—'}；库存/总使用比 ${totalUse.ratio === null ? '—' : `${totalUse.ratio}%`}。`,
    '- 库存/总使用比使用 G2 聚合后的期末库存与总使用量重新计算，未对 Malaysia 和 Indonesia 的比率进行相加、平均或加权平均。',
    '',
    '### 三个共同市场年度抽查',
    '',
    '| Market Year | G2 Production = Malaysia + Indonesia | G2 Exports = Malaysia + Indonesia | G2 Domestic Consumption = Malaysia + Indonesia | G2 Ending Stocks = Malaysia + Indonesia |',
    '| --- | --- | --- | --- | --- |',
    ...samples.map((year) => `| ${year} | ${g2Equation(store, sources, 'Production', year)} | ${g2Equation(store, sources, 'Exports', year)} | ${g2Equation(store, sources, 'Domestic Consumption', year)} | ${g2Equation(store, sources, 'Ending Stocks', year)} |`),
  ]
}

function g2Equation(store: MatrixStore, sources: MatrixStore[], attribute: string, year: number): string {
  const result = store.attributes.get(attribute)?.get(year)?.value
  const malaysia = sources.find((source) => source.country === 'Malaysia')?.attributes.get(attribute)?.get(year)?.value
  const indonesia = sources.find((source) => source.country === 'Indonesia')?.attributes.get(attribute)?.get(year)?.value
  return result === undefined || malaysia === undefined || indonesia === undefined ? '—' : `${result} = ${malaysia} + ${indonesia}`
}

async function buildData() {
  const reportVersion = readReportVersion()
  const apiConfig = readUsdaApiConfig()
  const apiDataDirectory = join(API_RAW_DIRECTORY, apiConfig.reportMonth, 'psd')
  const useApiSource = apiConfig.useApi && existsSync(apiDataDirectory) && readdirSync(apiDataDirectory).some((file) => file.toLowerCase().endsWith('.json'))
  const sourceFile = useApiSource ? join(apiDataDirectory, 'API_PSD_JSON') : findSingleSourceFile(reportVersion.currentReportMonth)
  const { matrices, unknownCommodities, missingKeyFields } = useApiSource ? readApiMatrices(apiDataDirectory) : await readMatrices(sourceFile)
  const activeReportMonth = useApiSource ? apiConfig.reportMonth : reportVersion.currentReportMonth
  rmSync(MATRIX_DIRECTORY, { recursive: true, force: true })
  mkdirSync(MATRIX_DIRECTORY, { recursive: true })

  const commodityIndex = new Map<string, IndexData['commodities'][number]>()
  const countryIndex = new Map<string, IndexData['countries'][number]>()
  const matrixIndex: IndexData['matrices'] = []
  const renderStores = new Map<string, MatrixStore>()
  const globalSources = new Map<string, GlobalSourceType>()
  const globalAudits: GlobalAggregationAudit[] = []
  const palmG2Sources = [...matrices.values()].filter((store) => store.commodity === G2_PALM_COMMODITY && G2_PALM_COUNTRIES.includes(store.country as typeof G2_PALM_COUNTRIES[number]))
  const { store: palmG2, audit: palmG2Audit } = aggregatePalmG2(palmG2Sources)
  let defaultMatrix: MatrixData | null = null

  for (const store of matrices.values()) {
    if (isResearchCombination(store.commodity, store.country)) renderStores.set(matrixKey(store.commodityCode, store.countryCode), store)
  }

  for (const commodity of G3_COMMODITIES) {
    const sources = [...matrices.values()].filter((store) => store.commodity === commodity && G3_COUNTRIES.includes(store.country))
    const source = sources[0]
    const g3 = source && aggregateStores(sources, source.commodityCode, commodity, source.category, 'G3', 'G3')
    if (g3) renderStores.set(matrixKey(g3.commodityCode, g3.countryCode), g3)
  }

  if (palmG2) renderStores.set(matrixKey(palmG2.commodityCode, palmG2.countryCode), palmG2)

  for (const commodity of Object.keys(RESEARCH_SCOPE)) {
    const sources = [...matrices.values()].filter((store) => store.commodity === commodity)
    const source = sources[0]
    if (!source) continue
    const world = sources.find(isWorldStore)
    const sourceType: GlobalSourceType = world ? 'USDA World' : 'synthetic sum'
    const aggregationSources = world ? [world] : sources.filter((store) => !isWorldStore(store))
    const global = world
      ? cloneAsVirtualStore(world, 'GL', 'Global')
      : aggregateStores(aggregationSources, source.commodityCode, commodity, source.category, 'GL', 'Global')
    if (global) {
      renderStores.set(matrixKey(global.commodityCode, global.countryCode), global)
      globalSources.set(commodity, sourceType)
      const audit = createGlobalAggregationAudit(aggregationSources, sourceType)
      if (audit) globalAudits.push(audit)
    }
  }

  let generatedMatrixCount = 0
  for (const store of renderStores.values()) {
    const matrix = buildMatrix(store, renderStores)
    if (matrix.years.length === 0) continue
    const file = `matrix/${safeFileName(store.commodityCode, store.countryCode)}`
    writeFileSync(join(DATA_DIRECTORY, file), `${JSON.stringify(matrix, null, 2)}\n`, 'utf8')
    generatedMatrixCount += 1
    matrixIndex.push({ category: matrix.category, commodityCode: matrix.commodityCode, commodity: matrix.commodity, countryCode: matrix.countryCode, country: matrix.country, file })
    commodityIndex.set(store.commodityCode, {
      commodityCode: store.commodityCode,
      commodityDescription: store.commodity,
      category: store.category,
      displayName: store.commodity.replace(/^(Oilseed|Oil|Meal),\s*/, ''),
    })
    countryIndex.set(store.countryCode, { countryCode: store.countryCode, countryName: store.country, displayName: countryDisplayName(store.countryCode, store.country) })
    if (matrix.commodity === 'Oil, Soybean' && matrix.country === 'United States') defaultMatrix = matrix
  }

  const index: IndexData = {
    categories: CATEGORIES,
    commodities: [...commodityIndex.values()].sort((a, b) => a.category.localeCompare(b.category) || a.displayName.localeCompare(b.displayName)),
    countries: [...countryIndex.values()].sort((a, b) => a.countryName.localeCompare(b.countryName)),
    matrices: matrixIndex.sort((a, b) => a.category.localeCompare(b.category) || a.commodity.localeCompare(b.commodity) || a.country.localeCompare(b.country)),
    defaultSelection: { category: 'Oils', commodityDescription: 'Oil, Soybean', countryName: 'United States' },
  }

  mkdirSync(DATA_DIRECTORY, { recursive: true })
  writeFileSync(join(DATA_DIRECTORY, 'index.json'), `${JSON.stringify(index, null, 2)}\n`, 'utf8')
  if (defaultMatrix) {
    const legacyOrder = ['大豆产量', '大豆压榨', '期初库存', '产量', '进口量', '出口量', '消费量', '工业消费', '食用消费', '期末库存', '库存/总使用比']
    const legacyMatrix = { ...defaultMatrix, rows: legacyOrder.flatMap((name) => defaultMatrix.rows.filter((row) => row.name === name)) }
    writeFileSync(join(DATA_DIRECTORY, 'soybean_oil_US.json'), `${JSON.stringify(legacyMatrix, null, 2)}\n`, 'utf8')
  }
  saveProcessedSnapshot(activeReportMonth)
  publishSnapshotForFrontend(activeReportMonth)
  publishSnapshotForFrontend(reportVersion.previousReportMonth)
  synchronizeSnapshotRatioRows(SNAPSHOT_DIRECTORY)
  synchronizeSnapshotRatioRows(PUBLIC_SNAPSHOT_DIRECTORY)
  writeFileSync(join(DATA_DIRECTORY, 'report_version.json'), `${JSON.stringify({ currentReportMonth: activeReportMonth, previousReportMonth: reportVersion.previousReportMonth }, null, 2)}\n`, 'utf8')
  writeReport(sourceFile, activeReportMonth, index, generatedMatrixCount, defaultMatrix !== null, unknownCommodities, missingKeyFields, globalSources, palmG2, palmG2Audit, palmG2Sources)
  writeGlobalAggregationReport(globalAudits)
  console.log(`已生成 ${generatedMatrixCount} 个 matrix JSON、${index.commodities.length} 个商品和 ${index.countries.length} 个国家。`)
}

buildData().catch((error: unknown) => {
  console.error(error instanceof Error ? error.message : error)
  process.exitCode = 1
})
