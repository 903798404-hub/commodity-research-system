import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { parse } from 'yaml'
import { formatMarketYearDescription, parseMarketYearLabel, resolveMarketYearPeriod, type MarketYearRulesConfig } from './marketYear'

const configPath = fileURLToPath(new URL('../../configs/usda_market_years.yaml', import.meta.url))
const config = parse(readFileSync(configPath, 'utf8')) as MarketYearRulesConfig

test('parses supported market-year labels', () => {
  for (const label of ['2025/26', '25/26', '2025-26', '2025/2026']) {
    assert.deepEqual(parseMarketYearLabel(label), { startYear: 2025, endYear: 2026, label })
  }
})

test('resolves United States soybean and soybean product periods', () => {
  const soybean = resolveMarketYearPeriod('United States', 'Oilseed, Soybean', '2025/26', config)
  const oil = resolveMarketYearPeriod('United States', 'Oil, Soybean', '2025/26', config)
  const meal = resolveMarketYearPeriod('United States', 'Meal, Soybean', '2025/26', config)
  assert.deepEqual([soybean?.startYear, soybean?.startMonth, soybean?.endYear, soybean?.endMonth], [2025, 9, 2026, 8])
  assert.deepEqual([oil?.startYear, oil?.startMonth, oil?.endYear, oil?.endMonth], [2025, 10, 2026, 9])
  assert.deepEqual([meal?.startYear, meal?.startMonth, meal?.endYear, meal?.endMonth], [2025, 10, 2026, 9])
})

test('applies Argentina and Brazil second-year rules', () => {
  const argentina = resolveMarketYearPeriod('Argentina', 'Oilseed, Soybean', '2025/26', config)
  const brazil = resolveMarketYearPeriod('Brazil', 'Oil, Soybean', '2025/26', config)
  assert.deepEqual([argentina?.startYear, argentina?.startMonth, argentina?.endYear, argentina?.endMonth], [2026, 4, 2027, 3])
  assert.equal(formatMarketYearDescription(argentina!), '阿根廷大豆 2025/26 市场年度 = 2026年4月至2027年3月')
  assert.deepEqual([brazil?.startYear, brazil?.startMonth, brazil?.endYear, brazil?.endMonth], [2026, 1, 2026, 12])
})

test('resolves configured rapeseed country periods and aliases', () => {
  const cases: Array<[string, string, [number, number, number, number]]> = [
    ['Canada', 'Oilseed, Rapeseed', [2025, 8, 2026, 7]],
    ['Australia', 'Oil, Rapeseed', [2025, 11, 2026, 10]],
    ['European Union', 'Meal, Rapeseed', [2025, 7, 2026, 6]],
    ['China', 'Oilseed, Rapeseed', [2025, 10, 2026, 9]],
    ['Russia', 'Oil, Rapeseed', [2025, 7, 2026, 6]],
    ['Ukraine', 'Meal, Rapeseed', [2025, 7, 2026, 6]],
  ]
  for (const [country, commodity, expected] of cases) {
    const period = resolveMarketYearPeriod(country, commodity, '2025/26', config)
    assert.deepEqual([period?.startYear, period?.startMonth, period?.endYear, period?.endMonth], expected)
  }
})

test('resolves Malaysia, Indonesia, and G2 palm oil periods', () => {
  for (const country of ['Malaysia', 'Indonesia', 'G2']) {
    const period = resolveMarketYearPeriod(country, 'Oil, Palm', '2025/26', config)
    assert.deepEqual([period?.startYear, period?.startMonth, period?.endYear, period?.endMonth], [2025, 10, 2026, 9])
  }
})

test('returns null without a configured country or commodity rule', () => {
  assert.equal(resolveMarketYearPeriod('Global', 'Oil, Soybean', '2025/26', config), null)
  assert.equal(resolveMarketYearPeriod('United States', 'Oil, Palm', '2025/26', config), null)
})
