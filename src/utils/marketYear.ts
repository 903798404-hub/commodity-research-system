export type MarketYearLabelMode = 'start_year' | 'second_year' | 'second_year_calendar'

export type CommodityAlias = { rule_key: string; commodity_name: string }
export type MarketYearRule = { start_month: number; end_month: number; label_mode: MarketYearLabelMode; display_name: string }
export type MarketYearRulesConfig = {
  commodity_aliases: Record<string, CommodityAlias>
  market_year_rules: Record<string, Record<string, MarketYearRule>>
  reference_rows: Array<{ country: string; commodity_scope: string; rule_key: string }>
  country_display_names: Record<string, string>
}
export type ParsedMarketYear = { startYear: number; endYear: number; label: string }
export type MarketYearPeriod = {
  startYear: number
  startMonth: number
  endYear: number
  endMonth: number
  label: string
  rule: MarketYearRule
}

function normalizeYear(value: string, referenceYear?: number): number | null {
  if (!/^\d{2,4}$/.test(value)) return null
  const numeric = Number(value)
  if (value.length === 4) return numeric
  const century = referenceYear === undefined ? 2000 : Math.floor(referenceYear / 100) * 100
  const candidate = century + numeric
  return referenceYear !== undefined && candidate < referenceYear ? candidate + 100 : candidate
}

export function formatMarketYear(year: number): string {
  return `${String(year).slice(-2)}/${String(year + 1).slice(-2)}`
}

export function parseMarketYearLabel(value: string | number): ParsedMarketYear | null {
  if (typeof value === 'number' && Number.isInteger(value)) return { startYear: value, endYear: value + 1, label: `${value}/${String(value + 1).slice(-2)}` }
  if (typeof value !== 'string') return null
  const match = value.trim().match(/^(\d{2,4})\s*[/-]\s*(\d{2,4})$/)
  if (!match) return null
  const startYear = normalizeYear(match[1])
  const endYear = normalizeYear(match[2], startYear ?? undefined)
  if (startYear === null || endYear === null) return null
  return { startYear, endYear, label: value.trim() }
}

export function resolveMarketYearPeriod(country: string, commodity: string, marketYear: string | number, config: MarketYearRulesConfig): MarketYearPeriod | null {
  const parsed = parseMarketYearLabel(marketYear)
  const alias = config.commodity_aliases[commodity]
  const rule = alias ? config.market_year_rules[country]?.[alias.rule_key] : undefined
  if (!parsed || !rule) return null
  const periodStartYear = rule.label_mode === 'start_year' ? parsed.startYear : parsed.endYear
  const endYear = rule.label_mode === 'second_year_calendar' ? periodStartYear : periodStartYear + (rule.end_month < rule.start_month ? 1 : 0)
  return { startYear: periodStartYear, startMonth: rule.start_month, endYear, endMonth: rule.end_month, label: parsed.label, rule }
}

export function formatMarketYearDescription(period: MarketYearPeriod): string {
  return `${period.rule.display_name} ${period.label} 市场年度 = ${period.startYear}年${period.startMonth}月至${period.endYear}年${period.endMonth}月`
}

export function formatMarketYearRange(rule: MarketYearRule): string {
  return rule.end_month < rule.start_month ? `${rule.start_month}月—次年${rule.end_month}月` : `${rule.start_month}月—${rule.end_month}月`
}
