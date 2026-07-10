import { createReadStream, existsSync, mkdirSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { basename, join } from 'node:path'
import Papa from 'papaparse'

type Category = 'Oilseeds' | 'Oils' | 'Meals'

type RawRecord = {
  Commodity_Code?: string
  Commodity_Description?: string
  Country_Code?: string
  Country_Name?: string
  Market_Year?: string
  Attribute_Description?: string
  Unit_Description?: string
  Value?: string
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
  countries: Array<{ countryCode: string; countryName: string }>
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

const RAW_DIRECTORY = join(process.cwd(), 'raw')
const DATA_DIRECTORY = join(process.cwd(), 'public', 'data')
const MATRIX_DIRECTORY = join(DATA_DIRECTORY, 'matrix')
const REPORT_FILE = join(process.cwd(), 'output', 'data_build_report.md')
const CATEGORIES: Category[] = ['Oilseeds', 'Oils', 'Meals']
const MIN_YEAR = 2018

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

function parseMarketYear(value: string | undefined): number | null {
  const match = value?.match(/^(\d{4})/)
  return match ? Number(match[1]) : null
}

function roundOneDecimal(value: number): number {
  return Math.round((value + Number.EPSILON) * 10) / 10
}

function normalizeValue(record: RawRecord): StoredValue | null {
  const rawValue = Number(record.Value)
  if (!Number.isFinite(rawValue)) return null
  const unit = record.Unit_Description?.trim() ?? ''
  const value = unit === '(1000 MT)' || unit === '1000 MT' ? rawValue / 10 : rawValue
  return { value: roundOneDecimal(value), unit }
}

function matrixKey(commodityCode: string, countryCode: string): string {
  return `${commodityCode}|${countryCode}`
}

function safeFileName(commodityCode: string, countryCode: string): string {
  return `${commodityCode.replace(/[^a-z0-9_-]/gi, '_')}_${countryCode.replace(/[^a-z0-9_-]/gi, '_')}.json`
}

function findSingleSourceFile(): string {
  if (!existsSync(RAW_DIRECTORY)) throw new Error('未找到 raw 文件夹。')
  const files = readdirSync(RAW_DIRECTORY).filter((file) => file.toLowerCase().endsWith('.csv'))
  if (files.length === 0) throw new Error('raw 文件夹中没有 CSV 文件。')
  if (files.length > 1) throw new Error(`raw 文件夹中有多个 CSV 文件：${files.join(', ')}`)
  return join(RAW_DIRECTORY, files[0])
}

async function readMatrices(filePath: string) {
  const matrices = new Map<string, MatrixStore>()
  const unknownCommodities = new Set<string>()
  let missingKeyFields = 0

  await new Promise<void>((resolve, reject) => {
    Papa.parse<RawRecord>(createReadStream(filePath), {
      header: true,
      skipEmptyLines: true,
      step: (result) => {
        if (result.errors.length > 0) {
          reject(new Error(`CSV 解析失败：${result.errors[0].message}`))
          return
        }
        const record = result.data
        const commodity = record.Commodity_Description?.trim()
        const category = commodity ? categoryFor(commodity) : null
        if (!category) {
          if (commodity) unknownCommodities.add(commodity)
          return
        }

        const commodityCode = record.Commodity_Code?.trim()
        const countryCode = record.Country_Code?.trim()
        const country = record.Country_Name?.trim()
        const attribute = record.Attribute_Description?.trim()
        const year = parseMarketYear(record.Market_Year)
        const normalized = normalizeValue(record)
        if (!commodityCode || !countryCode || !country || !attribute || year === null || !normalized) {
          missingKeyFields += 1
          return
        }

        const key = matrixKey(commodityCode, countryCode)
        const store = matrices.get(key) ?? {
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
        matrices.set(key, store)
      },
      complete: () => resolve(),
      error: (error) => reject(error),
    })
  })
  return { matrices, unknownCommodities, missingKeyFields }
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

  const endingStocks = store.attributes.get('Ending Stocks')
  const domesticConsumption = store.attributes.get('Domestic Consumption')
  const totalDistribution = store.attributes.get('Total Distribution')
  const ratio = years.map((year) => {
    const ending = endingStocks?.get(year)?.value
    const denominator = domesticConsumption?.get(year)?.value ?? totalDistribution?.get(year)?.value
    return ending !== undefined && denominator !== undefined && denominator !== 0 ? roundOneDecimal((ending / denominator) * 100) : null
  })
  if (hasAnyValue(ratio)) rows.push({ name: '期末库销比', values: ratio })

  return { commodityCode: store.commodityCode, commodity: store.commodity, category: store.category, countryCode: store.countryCode, country: store.country, years, rows }
}

function writeReport(sourceFile: string, index: IndexData, matrixCount: number, defaultCreated: boolean, unknownCommodities: Set<string>, missingKeyFields: number) {
  mkdirSync(join(process.cwd(), 'output'), { recursive: true })
  const lines = [
    '# 数据构建报告',
    '',
    `- 原始 CSV：\`${basename(sourceFile)}\``,
    `- 年份过滤：仅保留 Market_Year >= ${MIN_YEAR} 的数据`,
    `- 生成商品数：${index.commodities.length}`,
    `- 生成国家数：${index.countries.length}`,
    `- 生成 matrix JSON 数：${matrixCount}`,
    `- Oil, Soybean + United States：${defaultCreated ? '成功生成' : '未生成'}`,
    `- 无法识别 category 的 Commodity_Description：${unknownCommodities.size}${unknownCommodities.size ? `（${[...unknownCommodities].slice(0, 20).join('；')}）` : ''}`,
    `- 缺少关键字段的数据行：${missingKeyFields}`,
    '',
    '说明：matrix 文件按 Commodity_Code + Country_Code 拆分；数量单位为 (1000 MT) 或 1000 MT 时已转换为万吨。',
  ]
  writeFileSync(REPORT_FILE, `${lines.join('\n')}\n`, 'utf8')
}

async function buildData() {
  const sourceFile = findSingleSourceFile()
  const { matrices, unknownCommodities, missingKeyFields } = await readMatrices(sourceFile)
  rmSync(MATRIX_DIRECTORY, { recursive: true, force: true })
  mkdirSync(MATRIX_DIRECTORY, { recursive: true })

  const commodityIndex = new Map<string, IndexData['commodities'][number]>()
  const countryIndex = new Map<string, IndexData['countries'][number]>()
  let defaultMatrix: MatrixData | null = null

  let generatedMatrixCount = 0
  for (const store of matrices.values()) {
    const matrix = buildMatrix(store, matrices)
    if (matrix.years.length === 0) continue
    writeFileSync(join(MATRIX_DIRECTORY, safeFileName(store.commodityCode, store.countryCode)), `${JSON.stringify(matrix, null, 2)}\n`, 'utf8')
    generatedMatrixCount += 1
    commodityIndex.set(store.commodityCode, {
      commodityCode: store.commodityCode,
      commodityDescription: store.commodity,
      category: store.category,
      displayName: store.commodity.replace(/^(Oilseed|Oil|Meal),\s*/, ''),
    })
    countryIndex.set(store.countryCode, { countryCode: store.countryCode, countryName: store.country })
    if (matrix.commodity === 'Oil, Soybean' && matrix.country === 'United States') defaultMatrix = matrix
  }

  const index: IndexData = {
    categories: CATEGORIES,
    commodities: [...commodityIndex.values()].sort((a, b) => a.category.localeCompare(b.category) || a.displayName.localeCompare(b.displayName)),
    countries: [...countryIndex.values()].sort((a, b) => a.countryName.localeCompare(b.countryName)),
    defaultSelection: { category: 'Oils', commodityDescription: 'Oil, Soybean', countryName: 'United States' },
  }

  mkdirSync(DATA_DIRECTORY, { recursive: true })
  writeFileSync(join(DATA_DIRECTORY, 'index.json'), `${JSON.stringify(index, null, 2)}\n`, 'utf8')
  if (defaultMatrix) {
    const legacyOrder = ['大豆产量', '大豆压榨', '期初库存', '产量', '进口量', '出口量', '消费量', '工业消费', '食用消费', '期末库存', '期末库销比']
    const legacyMatrix = { ...defaultMatrix, rows: legacyOrder.flatMap((name) => defaultMatrix.rows.filter((row) => row.name === name)) }
    writeFileSync(join(DATA_DIRECTORY, 'soybean_oil_US.json'), `${JSON.stringify(legacyMatrix, null, 2)}\n`, 'utf8')
  }
  writeReport(sourceFile, index, generatedMatrixCount, defaultMatrix !== null, unknownCommodities, missingKeyFields)
  console.log(`已生成 ${generatedMatrixCount} 个 matrix JSON、${index.commodities.length} 个商品和 ${index.countries.length} 个国家。`)
}

buildData().catch((error: unknown) => {
  console.error(error instanceof Error ? error.message : error)
  process.exitCode = 1
})
