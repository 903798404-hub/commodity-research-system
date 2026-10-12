import test from 'node:test'
import assert from 'node:assert/strict'
import { buildSugarCsv, buildSugarView, validateSugarInput, type SugarInput } from './sugarBalance'
import { buildChartSeries } from './chart'

function fixture(): SugarInput {
  const records: SugarInput['records'] = []
  let beginning = 10
  for (let year = 2020; year <= 2026; year++) {
    records.push({ year, sourceId: year < 2024 ? 'wmt' : 'gain', status: year < 2024 ? 'historical' : year === 2024 ? 'revised' : year === 2025 ? 'estimate' : 'forecast', beginningStocks: beginning, production: 100, caneSugar: year < 2024 ? null : 80, beetSugar: year < 2024 ? null : 20, imports: 20, exports: 5, consumption: 100, otherUse: 0, endingStocks: beginning + 15 })
    beginning += 15
  }
  return { schemaVersion: 1, country: 'China', commodity: 'Sugar, Centrifugal', inputUnit: '1000 MT raw value', marketingYear: { startMonth: 10, endMonth: 9 }, sources: [{ id: 'wmt', basis: 'wmt', label: 'WMT test source', reportDate: '2026-05', url: 'https://apps.fas.usda.gov/psdonline/circulars/sugar.pdf' }, { id: 'gain', basis: 'gain_new_post', label: 'New Post test source', reportDate: '2026-10-05', url: 'https://apps.fas.usda.gov/example.pdf' }], records, baselineSourceId: 'wmt', baseline: records.filter(r => r.year >= 2024).map(r => ({ year: r.year, beginningStocks: r.beginningStocks, production: 90, imports: 30, exports: r.exports, consumption: r.consumption, otherUse: 0, endingStocks: r.endingStocks })), notes: [{ metric: '食糖总产量', text: 'fixture notes' }], scopeNotes: ['fixture scope'], inputEvidence: { workbookSha256: '0'.repeat(64), fasPdfSha256: '1'.repeat(64) } }
}

test('sugar conversion preserves missing components, all seven balances, source differences and forecast status', () => {
  const input = validateSugarInput(fixture()), view = buildSugarView(input)
  assert.deepEqual(view.years, [2020, 2021, 2022, 2023, 2024, 2025, 2026])
  assert.deepEqual(view.rows.find(r => r.name === '甘蔗糖产量')!.values, [null, null, null, null, 8, 8, 8])
  assert.deepEqual(view.rows.find(r => r.name === '平衡差')!.values, [0, 0, 0, 0, 0, 0, 0])
  assert.equal(view.rows.find(r => r.name === '库存/国内消费比')!.values[0], 25)
  assert.deepEqual(view.forecastYears, [2026])
  assert.equal(view.comparisons.length, 18)
  assert.equal(view.comparisons.find(c => c.year === 2026 && c.metric === '食糖总产量')!.difference, 1)
  assert.equal(view.comparisons.find(c => c.year === 2026 && c.metric === '进口量')!.difference, -1)
  assert.equal(buildChartSeries(view.rows.filter(r => r.unit === '%'), 7, 'raw').every(r => r.isPercent), true)
})

test('rejects mixed units, duplicate years, incomplete source metadata and missing FAS comparisons', () => {
  for (const mutate of [
    (d: SugarInput) => { d.inputUnit = '万吨' as SugarInput['inputUnit'] },
    (d: SugarInput) => { d.records[2].year = 2021 },
    (d: SugarInput) => { d.records[4].sourceId = 'wmt' },
    (d: SugarInput) => { d.sources[0].url = 'javascript:alert(1)' },
    (d: SugarInput) => { d.sources[1].reportDate = '' },
    (d: SugarInput) => { d.baseline.pop() },
  ]) {
    const data = fixture(); mutate(data); assert.throws(() => validateSugarInput(data), /食糖数据校验失败/)
  }
})

test('rejects broken balance, stock linkage, non-finite quantities and inconsistent or missing cane/beet production', () => {
  for (const mutate of [
    (d: SugarInput) => { d.records[1].imports++ },
    (d: SugarInput) => { d.records[4].beginningStocks++; d.records[4].endingStocks++ },
    (d: SugarInput) => { d.records[6].production = Infinity },
    (d: SugarInput) => { d.records[4].caneSugar = 81 },
    (d: SugarInput) => { d.records[4].caneSugar = null; d.records[4].beetSugar = null },
    (d: SugarInput) => { d.records[0].caneSugar = 0 },
  ]) {
    const data = fixture(); mutate(data); assert.throws(() => validateSugarInput(data), /食糖数据校验失败/)
  }
})

test('zero consumption yields a missing ratio and CSV preserves per-year source, status, units and blanks', () => {
  const data = fixture()
  data.records[6].consumption = 0
  data.records[6].endingStocks += 100
  const input = validateSugarInput(data), view = buildSugarView(input)
  assert.equal(view.rows.find(r => r.name === '库存/国内消费比')!.values[6], null)
  const csv = buildSugarCsv(input, view)
  assert.ok(csv.startsWith('\uFEFF'))
  assert.ok(csv.includes('"甘蔗糖产量","2020/21","","万吨","历史","WMT test source"'))
  assert.ok(csv.includes('"甜菜糖产量","2026/27","2","万吨","预测","New Post test source","2026-10-05"'))
  assert.ok(!csv.includes('monthly_revision'))
})
