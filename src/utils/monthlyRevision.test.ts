import assert from 'node:assert/strict'
import test from 'node:test'
import { buildPalmG2MonthlyRevisionLookup, buildSoybeanG3MonthlyRevisionLookup } from './monthlyRevision'
import type { MatrixData } from '../types/dashboard'

function matrix(rows: Array<{ name: string; values: Array<number | null> }>): MatrixData {
  return { commodityCode: '4243000', commodity: 'Oil, Palm', category: 'Oils', countryCode: 'test', country: 'test', years: [2025, 2026], rows }
}

test('builds G2 absolute monthly revisions from Malaysia plus Indonesia', () => {
  const currentMalaysia = matrix([{ name: '产量', values: [100, 120] }, { name: '期末库存', values: [10, 12] }, { name: '消费量', values: [30, 40] }, { name: '出口量', values: [50, 60] }])
  const currentIndonesia = matrix([{ name: '产量', values: [200, 230] }, { name: '期末库存', values: [20, 24] }, { name: '消费量', values: [70, 80] }, { name: '出口量', values: [90, 100] }])
  const previousMalaysia = matrix([{ name: '产量', values: [90, 110] }, { name: '期末库存', values: [9, 11] }, { name: '消费量', values: [28, 35] }, { name: '出口量', values: [48, 55] }])
  const previousIndonesia = matrix([{ name: '产量', values: [190, 210] }, { name: '期末库存', values: [19, 21] }, { name: '消费量', values: [65, 75] }, { name: '出口量', values: [85, 95] }])
  const currentG2 = matrix([{ name: '产量', values: [300, 350] }, { name: '库存/总使用比', values: [0, 0] }])

  const lookup = buildPalmG2MonthlyRevisionLookup(currentG2, currentMalaysia, previousMalaysia, currentIndonesia, previousIndonesia)
  assert.deepEqual(lookup.产量[2026], { previousValue: 320, revision: 30 })
})

test('keeps G2 monthly revisions empty on a one-sided missing value and recomputes stock-to-use ratio', () => {
  const currentMalaysia = matrix([{ name: '产量', values: [100, 120] }, { name: '期末库存', values: [10, 12] }, { name: '消费量', values: [30, 40] }, { name: '出口量', values: [50, 60] }])
  const currentIndonesia = matrix([{ name: '产量', values: [200, null] }, { name: '期末库存', values: [20, 24] }, { name: '消费量', values: [70, 80] }, { name: '出口量', values: [90, 100] }])
  const previousMalaysia = matrix([{ name: '产量', values: [90, 110] }, { name: '期末库存', values: [9, 11] }, { name: '消费量', values: [28, 35] }, { name: '出口量', values: [48, 55] }])
  const previousIndonesia = matrix([{ name: '产量', values: [190, 210] }, { name: '期末库存', values: [19, 21] }, { name: '消费量', values: [65, 75] }, { name: '出口量', values: [85, 95] }])
  const currentG2 = matrix([{ name: '产量', values: [300, null] }, { name: '库存/总使用比', values: [0, 0] }])

  const lookup = buildPalmG2MonthlyRevisionLookup(currentG2, currentMalaysia, previousMalaysia, currentIndonesia, previousIndonesia)
  assert.deepEqual(lookup.产量[2026], { previousValue: 320, revision: null })
  assert.ok(Math.abs((lookup['库存/总使用比'][2026].previousValue ?? 0) - (32 / 260 * 100)) < 0.000001)
  assert.ok(Math.abs((lookup['库存/总使用比'][2026].revision ?? 0) - ((36 / 280 * 100) - (32 / 260 * 100))) < 0.000001)
})

test('builds strict G3 monthly revisions and recomputes its stock-to-use ratio', () => {
  const currentUs = matrix([{ name: '产量', values: [100, 120] }, { name: '期末库存', values: [10, 12] }, { name: '消费量', values: [30, 35] }, { name: '出口量', values: [50, 55] }])
  const currentBrazil = matrix([{ name: '产量', values: [200, 230] }, { name: '期末库存', values: [20, 24] }, { name: '消费量', values: [70, 75] }, { name: '出口量', values: [90, 95] }])
  const currentArgentina = matrix([{ name: '产量', values: [300, 350] }, { name: '期末库存', values: [30, 35] }, { name: '消费量', values: [80, 90] }, { name: '出口量', values: [110, 120] }])
  const previousUs = matrix([{ name: '产量', values: [90, 110] }, { name: '期末库存', values: [9, 11] }, { name: '消费量', values: [28, 32] }, { name: '出口量', values: [48, 50] }])
  const previousBrazil = matrix([{ name: '产量', values: [190, 210] }, { name: '期末库存', values: [19, 21] }, { name: '消费量', values: [65, 70] }, { name: '出口量', values: [85, 88] }])
  const previousArgentina = matrix([{ name: '产量', values: [280, 320] }, { name: '期末库存', values: [28, 31] }, { name: '消费量', values: [75, 82] }, { name: '出口量', values: [105, 110] }])
  const currentG3 = matrix([{ name: '产量', values: [600, 700] }, { name: '库存/总使用比', values: [0, 0] }])

  const lookup = buildSoybeanG3MonthlyRevisionLookup(currentG3, [currentUs, currentBrazil, currentArgentina], [previousUs, previousBrazil, previousArgentina])
  assert.deepEqual(lookup.产量[2026], { previousValue: 640, revision: 60 })
  const expectedCurrentRatio = 71 / (200 + 270) * 100
  const expectedPreviousRatio = 63 / (184 + 248) * 100
  assert.ok(Math.abs((lookup['库存/总使用比'][2026].revision ?? 0) - (expectedCurrentRatio - expectedPreviousRatio)) < 0.000001)

  const oneSidedMissing = buildSoybeanG3MonthlyRevisionLookup(currentG3, [currentUs, currentBrazil, null], [previousUs, previousBrazil, previousArgentina])
  assert.equal(oneSidedMissing.产量[2026].revision, null)
})
