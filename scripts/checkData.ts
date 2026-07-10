import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

type Severity = 'fatal' | 'warning' | 'info'
type Issue = { severity: Severity; file: string; message: string }
type IndexCommodity = { commodityCode?: unknown; commodityDescription?: unknown; category?: unknown }
type IndexCountry = { countryCode?: unknown; countryName?: unknown }
type IndexMatrix = { category?: unknown; commodityCode?: unknown; commodity?: unknown; countryCode?: unknown; country?: unknown; file?: unknown }
type Matrix = {
  commodityCode?: unknown
  commodity?: unknown
  category?: unknown
  countryCode?: unknown
  country?: unknown
  years?: unknown
  rows?: unknown
}

const MIN_YEAR = 2018
const DATA_DIRECTORY = join(process.cwd(), 'public', 'data')
const MATRIX_DIRECTORY = join(DATA_DIRECTORY, 'matrix')
const REPORT_FILE = join(process.cwd(), 'output', 'data_quality_report.md')
const VALID_CATEGORIES = new Set(['Oilseeds', 'Oils', 'Meals'])
const issues: Issue[] = []

function addIssue(severity: Severity, file: string, message: string) {
  issues.push({ severity, file, message })
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function parseJson(filePath: string): unknown | null {
  try {
    return JSON.parse(readFileSync(filePath, 'utf8'))
  } catch (error) {
    addIssue('fatal', filePath, `无法解析 JSON：${error instanceof Error ? error.message : String(error)}`)
    return null
  }
}

function validateYears(file: string, years: unknown): number[] {
  if (!Array.isArray(years)) {
    addIssue('fatal', file, '缺少 years 数组。')
    return []
  }
  if (years.length === 0) {
    addIssue('warning', file, `years 为空，未找到 ${MIN_YEAR} 年及以后的数据。`)
    return []
  }
  const numericYears = years.filter((year): year is number => typeof year === 'number' && Number.isInteger(year))
  if (numericYears.length !== years.length) addIssue('warning', file, 'years 含非整数年份值。')
  if (!numericYears.some((year) => year >= MIN_YEAR)) addIssue('warning', file, `未包含 ${MIN_YEAR} 年及以后的年份。`)
  if (numericYears.some((year) => year < MIN_YEAR)) addIssue('warning', file, `包含早于 ${MIN_YEAR} 年的年份。`)
  const sorted = [...numericYears].sort((a, b) => a - b)
  if (sorted.some((year, index) => index > 0 && year === sorted[index - 1])) addIssue('warning', file, 'years 含重复年份。')
  if (numericYears.some((year, index) => index > 0 && year < numericYears[index - 1])) addIssue('warning', file, 'years 未按升序排列。')
  for (let index = 1; index < sorted.length; index += 1) {
    if (sorted[index] - sorted[index - 1] > 1) {
      addIssue('warning', file, `年份存在断档：${sorted[index - 1]} 至 ${sorted[index]}。`)
    }
  }
  return numericYears
}

function validateRows(file: string, rows: unknown, yearCount: number) {
  if (!Array.isArray(rows)) {
    addIssue('fatal', file, '缺少 rows 数组。')
    return
  }
  if (rows.length < 5) addIssue('info', file, `指标行较少（${rows.length} 行）；这可能是该商品或国家的正常数据覆盖范围。`)
  for (const [index, row] of rows.entries()) {
    if (!isRecord(row) || typeof row.name !== 'string' || !Array.isArray(row.values)) {
      addIssue('fatal', file, `rows[${index}] 缺少 name 或 values 基础结构。`)
      continue
    }
    if (row.name.trim() === '') addIssue('warning', file, `rows[${index}] 指标名称为空。`)
    if (row.values.length !== yearCount) addIssue('warning', file, `指标“${row.name}”的 values 长度（${row.values.length}）与 years 长度（${yearCount}）不一致。`)
    for (const [valueIndex, value] of row.values.entries()) {
      if (value === null || value === undefined || value === '') {
        addIssue('warning', file, `指标“${row.name}”在第 ${valueIndex + 1} 个年份位置为空。`)
      } else if (typeof value !== 'number' || !Number.isFinite(value)) {
        addIssue('warning', file, `指标“${row.name}”在第 ${valueIndex + 1} 个年份位置不是可读取数字。`)
      }
    }
  }
}

function validateMatrix(filePath: string): Matrix | null {
  const parsed = parseJson(filePath)
  if (!isRecord(parsed)) {
    if (parsed !== null) addIssue('fatal', filePath, 'JSON 根节点不是对象。')
    return null
  }
  const matrix = parsed as Matrix
  for (const field of ['commodityCode', 'commodity', 'category', 'countryCode', 'country'] as const) {
    if (typeof matrix[field] !== 'string' || matrix[field].trim() === '') addIssue('fatal', filePath, `缺少基础字段 ${field}。`)
  }
  if (typeof matrix.category === 'string' && !VALID_CATEGORIES.has(matrix.category)) addIssue('fatal', filePath, `category 无效：${matrix.category}。`)
  const years = validateYears(filePath, matrix.years)
  validateRows(filePath, matrix.rows, years.length)
  return matrix
}

function writeReport(matrixCount: number, indexParsed: boolean) {
  const groups = (severity: Severity) => issues.filter((issue) => issue.severity === severity)
  const lines = [
    '# 数据技术完整性检查报告',
    '',
    `- 检查范围：${matrixCount} 个 matrix JSON 与 index.json`,
    `- 年份要求：至少包含 ${MIN_YEAR} 年及以后年份`,
    `- index.json：${indexParsed ? '已解析' : '无法解析'}`,
    `- fatal：${groups('fatal').length}`,
    `- warning：${groups('warning').length}`,
    `- info：${groups('info').length}`,
    '',
    '## 规则',
    '',
    '- fatal：文件无法解析、基础结构缺失、或 index 引用无法对应真实 matrix。',
    '- warning：年份断档、字段为空、非数字值，或年份格式异常。',
    '- info：指标行较少；不对 USDA 数值或商品天然缺失指标作判断。',
  ]
  for (const severity of ['fatal', 'warning', 'info'] as const) {
    const items = groups(severity)
    if (items.length === 0) continue
    lines.push('', `## ${severity}`, '')
    for (const issue of items.slice(0, 200)) lines.push(`- \`${issue.file.replace(process.cwd(), '.') }\`：${issue.message}`)
    if (items.length > 200) lines.push(`- 其余 ${items.length - 200} 条同级结果未展开。`)
  }
  mkdirSync(join(process.cwd(), 'output'), { recursive: true })
  writeFileSync(REPORT_FILE, `${lines.join('\n')}\n`, 'utf8')
}

function main() {
  if (!existsSync(MATRIX_DIRECTORY)) {
    addIssue('fatal', MATRIX_DIRECTORY, 'matrix 目录不存在。')
    writeReport(0, false)
    process.exitCode = 1
    return
  }

  const matrixFiles = readdirSync(MATRIX_DIRECTORY).filter((file) => file.endsWith('.json'))
  const matrices = new Map<string, Matrix>()
  const matricesByFile = new Map<string, Matrix>()
  for (const file of matrixFiles) {
    const matrix = validateMatrix(join(MATRIX_DIRECTORY, file))
    if (matrix && typeof matrix.commodityCode === 'string' && typeof matrix.countryCode === 'string') {
      matrices.set(`${matrix.commodityCode}|${matrix.countryCode}`, matrix)
      matricesByFile.set(`matrix/${file}`, matrix)
    }
  }

  const indexPath = join(DATA_DIRECTORY, 'index.json')
  const parsedIndex = parseJson(indexPath)
  let indexParsed = false
  if (!isRecord(parsedIndex) || !Array.isArray(parsedIndex.commodities) || !Array.isArray(parsedIndex.countries) || !Array.isArray(parsedIndex.categories) || !Array.isArray(parsedIndex.matrices)) {
    if (parsedIndex !== null) addIssue('fatal', indexPath, '缺少 categories、commodities、countries 或 matrices 基础结构。')
  } else {
    indexParsed = true
    const commodities = parsedIndex.commodities as IndexCommodity[]
    const countries = parsedIndex.countries as IndexCountry[]
    const indexMatrices = parsedIndex.matrices as IndexMatrix[]
    const commodityKeys = new Set<string>()
    const countryKeys = new Set<string>()
    for (const commodity of commodities) {
      if (typeof commodity.commodityCode !== 'string' || typeof commodity.commodityDescription !== 'string' || typeof commodity.category !== 'string') {
        addIssue('fatal', indexPath, 'commodities 中存在缺少 commodityCode、commodityDescription 或 category 的记录。')
        continue
      }
      if (!VALID_CATEGORIES.has(commodity.category)) addIssue('fatal', indexPath, `commodities 中存在无效 category：${commodity.category}。`)
      commodityKeys.add(commodity.commodityCode)
    }
    for (const country of countries) {
      if (typeof country.countryCode !== 'string' || typeof country.countryName !== 'string') {
        addIssue('fatal', indexPath, 'countries 中存在缺少 countryCode 或 countryName 的记录。')
        continue
      }
      countryKeys.add(country.countryCode)
    }
    for (const [key, matrix] of matrices) {
      if (!commodityKeys.has(String(matrix.commodityCode)) || !countryKeys.has(String(matrix.countryCode))) {
        addIssue('fatal', indexPath, `matrix 引用 ${key} 未同时出现在 index 的 commodity 与 country 列表中。`)
      }
    }
    const referencedFiles = new Set<string>()
    for (const reference of indexMatrices) {
      if (typeof reference.category !== 'string' || typeof reference.commodityCode !== 'string' || typeof reference.commodity !== 'string' || typeof reference.countryCode !== 'string' || typeof reference.country !== 'string' || typeof reference.file !== 'string') {
        addIssue('fatal', indexPath, 'matrices 中存在缺少 category、commodity、country 或 file 的记录。')
        continue
      }
      const matrix = matricesByFile.get(reference.file)
      if (!matrix) {
        addIssue('fatal', indexPath, `matrix 文件路径不存在：${reference.file}。`)
        continue
      }
      referencedFiles.add(reference.file)
      if (matrix.category !== reference.category || matrix.commodityCode !== reference.commodityCode || matrix.commodity !== reference.commodity || matrix.countryCode !== reference.countryCode || matrix.country !== reference.country) {
        addIssue('fatal', indexPath, `matrix 文件 ${reference.file} 的元数据与 index 引用不一致。`)
      }
    }
    for (const file of matricesByFile.keys()) {
      if (!referencedFiles.has(file)) addIssue('fatal', indexPath, `真实 matrix 文件未在 index.matrices 中引用：${file}。`)
    }
    const selection = parsedIndex.defaultSelection
    if (!isRecord(selection) || typeof selection.commodityDescription !== 'string' || typeof selection.countryName !== 'string') {
      addIssue('fatal', indexPath, 'defaultSelection 基础结构缺失。')
    } else {
      const commodity = commodities.find((item) => item.commodityDescription === selection.commodityDescription)
      const country = countries.find((item) => item.countryName === selection.countryName)
      const defaultMatrix = indexMatrices.find((item) => item.commodity === selection.commodityDescription && item.country === selection.countryName)
      if (!commodity || !country || !defaultMatrix) {
        addIssue('fatal', indexPath, 'defaultSelection 未对应到真实 matrix 文件。')
      }
    }
  }

  writeReport(matrixFiles.length, indexParsed)
  const fatalCount = issues.filter((issue) => issue.severity === 'fatal').length
  console.log(`检查完成：${matrixFiles.length} 个 matrix，fatal ${fatalCount}，warning ${issues.filter((issue) => issue.severity === 'warning').length}，info ${issues.filter((issue) => issue.severity === 'info').length}。`)
  if (fatalCount > 0) process.exitCode = 1
}

main()
