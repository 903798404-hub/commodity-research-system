import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { RESEARCH_SCOPE } from './researchScope'
import { assessResearchCoverage, isWorldApiPsdFile, reconcileResearchMatrices, validateApiBatchManifest, type ResearchMatrixStore } from './buildDataSourcePolicy'

type Store = ResearchMatrixStore & { source: 'api' | 'csv'; value: number }
function fullStores(source: Store['source'] = 'api'): Map<string, Store> {
  const stores = new Map<string, Store>(); let index = 0
  for (const [commodity, countries] of Object.entries(RESEARCH_SCOPE)) for (const country of countries) {
    index += 1; stores.set(`C${index}|K${index}`, { commodityCode: `C${index}`, commodity, countryCode: `K${index}`, country, source, value: index })
  }
  return stores
}

test('API 完整且无 CSV 时成功，并且不读取 fallback', async () => {
  let calls = 0; const result = await reconcileResearchMatrices(fullStores(), async () => { calls += 1; return null })
  assert.deepEqual([result.apiMatrixCount, result.csvSupplementCount, result.finalMatrixCount, calls], [43, 0, 43, 0])
})
test('API 完整且有 CSV 时仍以 API 为准', async () => {
  const api = fullStores(); const result = await reconcileResearchMatrices(api, async () => fullStores('csv'))
  assert.equal(result.csvFallbackUsed, false); assert.equal([...api.values()].filter((store) => store.source === 'csv').length, 0)
})
test('API 缺一个研究矩阵且 CSV 有该矩阵时仅补缺', async () => {
  const api = fullStores(); api.delete(api.keys().next().value as string)
  const result = await reconcileResearchMatrices(api, async () => fullStores('csv'))
  assert.deepEqual([result.apiMatrixCount, result.csvSupplementCount, result.finalMatrixCount, result.missing.length], [42, 1, 43, 0])
  assert.equal([...api.values()].filter((store) => store.source === 'csv').length, 1)
})
test('CSV fallback 不覆盖已有 API matrix', async () => {
  const api = fullStores(); const entries = [...api.entries()]; api.delete(entries[0][0]); const preserved = entries[1][1]
  await reconcileResearchMatrices(api, async () => fullStores('csv'))
  const actual = [...api.values()].find((store) => store.commodity === preserved.commodity && store.country === preserved.country)!
  assert.equal(actual.source, 'api'); assert.equal(actual.value, preserved.value)
})
test('API 缺一个研究矩阵且 CSV 也没有时保持明确缺失', async () => {
  const api = fullStores(); const csv = fullStores('csv'); const key = api.keys().next().value as string; api.delete(key); csv.delete(key)
  assert.equal((await reconcileResearchMatrices(api, async () => csv)).missing.length, 1)
})
test('API 缺矩阵且完全无 CSV 时明确失败条件', async () => {
  const api = fullStores(); api.delete(api.keys().next().value as string); const result = await reconcileResearchMatrices(api, async () => null)
  assert.equal(result.csvFallbackUsed, false); assert.equal(result.missing.length, 1); assert.match(result.missing[0].matrixKey, / \| /)
})
test('CSV-only 旧模式的研究范围覆盖保持正常', () => {
  const coverage = assessResearchCoverage(fullStores('csv').values()); assert.deepEqual([coverage.expectedCount, coverage.providedCount, coverage.missing.length], [43, 43, 0])
})
test('失败 API batch 不会被当作完整输入', () => {
  const root = mkdtempSync(join(tmpdir(), 'usda-manifest-')); mkdirSync(join(root, 'psd')); writeFileSync(join(root, 'psd', 'one.json'), '[]')
  writeFileSync(join(root, 'manifest.json'), JSON.stringify({ reportMonth: '2026-08', status: 'failed', expectedMetadataRequests: 0, expectedPsdRequests: 1, requests: [{ key: 'psd:one', kind: 'psd', status: 'success', success: true, finalOutputFile: 'psd/one.json' }] }))
  assert.ok(validateApiBatchManifest(root, '2026-08').some((error) => error.includes('status=failed')))
})
test('manifest 缺少任一 request 输出时不能通过', () => {
  const root = mkdtempSync(join(tmpdir(), 'usda-manifest-')); writeFileSync(join(root, 'manifest.json'), JSON.stringify({ reportMonth: '2026-08', status: 'success', expectedMetadataRequests: 0, expectedPsdRequests: 1, requests: [{ key: 'psd:missing', kind: 'psd', status: 'success', success: true, finalOutputFile: 'psd/missing.json' }] }))
  assert.ok(validateApiBatchManifest(root, '2026-08').some((error) => error.includes('输出文件缺失')))
})
test('USDA API double-underscore World filename is recognized', () => {
  assert.equal(isWorldApiPsdFile('Oilseed_Soybean__World__2026.json'), true)
  assert.equal(isWorldApiPsdFile('Oilseed_Soybean__United_States__2026.json'), false)
})
