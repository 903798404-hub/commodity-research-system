import { safePercentChange } from './number'

export const SUGAR_DATA_PATH = 'research/sugar/china.json'
export const formatSugarYear = (year: number) => `${year}/${String(year + 1).slice(-2)}`
export type SugarStatus = 'historical' | 'revised' | 'estimate' | 'forecast'
export const SUGAR_STATUS_LABELS: Record<SugarStatus, string> = { historical: '历史', revised: '历史修订', estimate: '最新估计', forecast: '预测' }
export type SugarSource = { id: string; basis: 'wmt' | 'gain_new_post'; label: string; reportDate: string; url: string; pages?: string }
type Quantities = { beginningStocks: number; production: number; imports: number; exports: number; consumption: number; otherUse: number; endingStocks: number }
export type SugarRecord = Quantities & { year: number; sourceId: string; status: SugarStatus; caneSugar: number | null; beetSugar: number | null }
export type SugarInput = {
  schemaVersion: 1
  country: 'China'
  commodity: 'Sugar, Centrifugal'
  inputUnit: '1000 MT raw value'
  marketingYear: { startMonth: 10; endMonth: 9 }
  sources: SugarSource[]
  records: SugarRecord[]
  baselineSourceId: string
  baseline: Array<Quantities & { year: number }>
  notes: Array<{ metric: string; text: string }>
  scopeNotes: string[]
  inputEvidence: { workbookSha256: string; fasPdfSha256: string }
}
export type SugarRow = { name: string; unit: '万吨' | '%'; values: Array<number | null> }
export type SugarComparison = { year: number; metric: string; baseline: number; current: number; difference: number }
export type SugarView = { years: number[]; rows: SugarRow[]; comparisons: SugarComparison[]; forecastYears: number[] }
const CORE = ['beginningStocks', 'production', 'imports', 'exports', 'consumption', 'otherUse', 'endingStocks'] as const
const object = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value)
const text = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0
const quantity = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0
function fail(reason: string): never { throw new Error(`食糖数据校验失败：${reason}`) }

function checkBalance(record: Record<string, unknown>) {
  if (!Number.isInteger(record.year) || CORE.some(key => !quantity(record[key]))) fail('年度或数量字段缺失、非有限值或负数')
  const r = record as unknown as Quantities
  if (Math.abs(r.beginningStocks + r.production + r.imports - r.exports - r.consumption - r.otherUse - r.endingStocks) > 1e-8) fail(`${record.year}年供需不平衡`)
}

/** Validate the research series separately from monthly PSD matrices; never fill absent components with zero. */
export function validateSugarInput(value: unknown): SugarInput {
  if (!object(value) || value.schemaVersion !== 1 || value.country !== 'China' || value.commodity !== 'Sugar, Centrifugal' || value.inputUnit !== '1000 MT raw value') fail('schema、国家、商品或原糖值单位不符')
  if (!object(value.marketingYear) || value.marketingYear.startMonth !== 10 || value.marketingYear.endMonth !== 9) fail('市场年度应为10月—翌年9月')
  if (!Array.isArray(value.sources) || value.sources.length === 0) fail('来源缺失')
  const sources = new Map<string, SugarSource>()
  for (const source of value.sources) {
    if (!object(source) || !text(source.id) || !text(source.label) || !text(source.url) || !text(source.reportDate) || !/^\d{4}-(0[1-9]|1[0-2])(?:-(0[1-9]|[12]\d|3[01]))?$/.test(source.reportDate) || !['wmt', 'gain_new_post'].includes(String(source.basis))) fail('来源信息不完整')
    try { if (new URL(source.url).protocol !== 'https:') fail('来源链接必须为HTTPS') } catch { fail('来源链接无效') }
    if (sources.has(source.id)) fail('来源ID重复')
    sources.set(source.id, source as unknown as SugarSource)
  }
  if (!Array.isArray(value.records) || value.records.length < 7) fail('需包含2020/21起的连续历史')
  let previous: SugarRecord | undefined
  for (const raw of value.records) {
    if (!object(raw)) fail('年度记录无效')
    checkBalance(raw)
    const r = raw as unknown as SugarRecord
    if (r.year !== (previous ? previous.year + 1 : 2020)) fail('年度重复、断档或未按升序排列')
    const source = sources.get(r.sourceId)
    if (!source || source.basis !== (r.year < 2024 ? 'wmt' : 'gain_new_post')) fail(`${r.year}年来源与拼接口径不符`)
    if (!Object.hasOwn(SUGAR_STATUS_LABELS, r.status)) fail('数据状态无效')
    if (r.caneSugar === null && r.beetSugar === null) {
      if (r.year >= 2024) fail('FAS年度分项产量缺失')
    } else if (!quantity(r.caneSugar) || !quantity(r.beetSugar) || Math.abs(r.caneSugar + r.beetSugar - r.production) > 1e-8) fail('甘蔗糖、甜菜糖与总产量不一致')
    if (previous && Math.abs(previous.endingStocks - r.beginningStocks) > 1e-8) fail('相邻年度库存不衔接')
    previous = r
  }
  if (!text(value.baselineSourceId) || sources.get(value.baselineSourceId)?.basis !== 'wmt' || !Array.isArray(value.baseline)) fail('WMT对照来源缺失')
  const current = new Map((value.records as SugarRecord[]).map(r => [r.year, r]))
  const comparedYears = new Set<number>()
  for (const raw of value.baseline) {
    if (!object(raw)) fail('对照记录无效')
    checkBalance(raw)
    const year = Number(raw.year)
    if (year < 2024 || !current.has(year) || comparedYears.has(year)) fail('对照年度重复或不存在')
    comparedYears.add(year)
  }
  if ([...current.keys()].filter(year => year >= 2024).some(year => !comparedYears.has(year))) fail('FAS年度对照不完整')
  if (!Array.isArray(value.notes) || value.notes.some(n => !object(n) || !text(n.metric) || !text(n.text)) || !Array.isArray(value.scopeNotes) || value.scopeNotes.length === 0 || value.scopeNotes.some(n => !text(n))) fail('备注缺失')
  const evidence = value.inputEvidence
  if (!object(evidence) || !['workbookSha256', 'fasPdfSha256'].every(key => typeof evidence[key] === 'string' && /^[a-f0-9]{64}$/.test(String(evidence[key])))) fail('输入文件身份缺失')
  return value as unknown as SugarInput
}

export function buildSugarView(input: SugarInput): SugarView {
  const records = input.records
  const row = (name: string, get: (record: SugarRecord, index: number) => number | null, unit: SugarRow['unit'] = '万吨'): SugarRow => ({ name, unit, values: records.map((r, i) => { const v = get(r, i); return v === null ? null : unit === '万吨' ? v / 10 : v }) })
  const rows = [
    row('期初库存', r => r.beginningStocks), row('食糖总产量', r => r.production),
    row('甘蔗糖产量', r => r.caneSugar), row('甜菜糖产量', r => r.beetSugar),
    row('进口量', r => r.imports), row('总供应', r => r.beginningStocks + r.production + r.imports),
    row('出口量', r => r.exports), row('消费量', r => r.consumption), row('其他消耗', r => r.otherUse),
    row('国内总使用', r => r.consumption + r.otherUse), row('期末库存', r => r.endingStocks),
    row('总分配', r => r.exports + r.consumption + r.otherUse + r.endingStocks),
    row('平衡差', r => r.beginningStocks + r.production + r.imports - r.exports - r.consumption - r.otherUse - r.endingStocks),
    row('库存/国内消费比', r => r.consumption + r.otherUse === 0 ? null : r.endingStocks / (r.consumption + r.otherUse) * 100, '%'),
    row('净出口', r => r.exports - r.imports),
    row('产量同比', (r, i) => i === 0 ? null : safePercentChange(r.production, records[i - 1].production), '%'),
  ]
  const metrics = [['期初库存', 'beginningStocks'], ['食糖总产量', 'production'], ['进口量', 'imports'], ['出口量', 'exports'], ['消费量', 'consumption'], ['期末库存', 'endingStocks']] as const
  const byYear = new Map(records.map(r => [r.year, r]))
  const comparisons = input.baseline.flatMap(base => metrics.map(([metric, key]) => ({ year: base.year, metric, baseline: base[key] / 10, current: byYear.get(base.year)![key] / 10, difference: (byYear.get(base.year)![key] - base[key]) / 10 })))
  return { years: records.map(r => r.year), rows, comparisons, forecastYears: records.filter(r => r.status === 'forecast').map(r => r.year) }
}

export function buildSugarCsv(input: SugarInput, view: SugarView): string {
  const escape = (v: string) => `"${v.replaceAll('"', '""')}"`
  const sources = new Map(input.sources.map(s => [s.id, s]))
  const notes = new Map(input.notes.map(n => [n.metric, n.text]))
  const lines = [['指标', '市场年度', '数值', '单位', '状态', '来源', '报告日期', '来源链接', '备注']]
  for (const row of view.rows) for (const [i, year] of view.years.entries()) {
    const record = input.records[i], source = sources.get(record.sourceId)!
    lines.push([row.name, formatSugarYear(year), row.values[i] === null ? '' : String(row.values[i]), row.unit, SUGAR_STATUS_LABELS[record.status], source.label, source.reportDate, source.url, notes.get(row.name) ?? ''])
  }
  return '\uFEFF' + lines.map(line => line.map(escape).join(',')).join('\n') + '\n'
}
