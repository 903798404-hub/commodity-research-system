import assert from 'node:assert/strict'
import test from 'node:test'
import { sourceCompatibility } from './sourceCompatibility'

test('ordinary countries remain comparable across source migrations', () => {
  assert.equal(sourceCompatibility({ country: 'United States' }, { country: 'United States' }).comparable, true)
})
test('2026-07 legacy synthetic Global vs USDA World is unavailable', () => {
  const result = sourceCompatibility({ country: 'Global', sourceBasis: 'usda_psd_world' }, { country: 'Global' }, null, 'legacy_csv_synthetic_global')
  assert.deepEqual(result, { comparable: false, reason: 'source_basis_changed', currentSourceBasis: 'usda_psd_world', previousSourceBasis: 'legacy_csv_synthetic_global' })
})
test('future USDA World vs USDA World automatically resumes comparison', () => {
  assert.equal(sourceCompatibility({ country: 'Global', sourceBasis: 'usda_psd_world' }, { country: 'Global', sourceBasis: 'usda_psd_world' }).comparable, true)
})
