import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { RESEARCH_SCOPE } from './researchScope'

export type ResearchMatrixStore = { commodityCode: string; commodity: string; countryCode: string; country: string }
export type MissingResearchMatrix = { commodity: string; country: string; matrixKey: string }
export type ResearchCoverage = { expectedCount: number; providedCount: number; missing: MissingResearchMatrix[] }
export type ReconciliationResult = {
  expectedCount: number
  apiMatrixCount: number
  csvSupplementCount: number
  finalMatrixCount: number
  missing: MissingResearchMatrix[]
  csvFallbackUsed: boolean
}

const identityKey = (commodity: string, country: string) => JSON.stringify([commodity, country])
const storageKey = (store: ResearchMatrixStore) => `${store.commodityCode}|${store.countryCode}`

export function expectedResearchMatrices(): MissingResearchMatrix[] {
  return Object.entries(RESEARCH_SCOPE).flatMap(([commodity, countries]) => countries.map((country) => ({
    commodity, country, matrixKey: `${commodity} | ${country}`,
  })))
}

export function assessResearchCoverage(stores: Iterable<ResearchMatrixStore>): ResearchCoverage {
  const identities = new Set([...stores].map((store) => identityKey(store.commodity, store.country)))
  const expected = expectedResearchMatrices()
  const missing = expected.filter((item) => !identities.has(identityKey(item.commodity, item.country)))
  return { expectedCount: expected.length, providedCount: expected.length - missing.length, missing }
}

export async function reconcileResearchMatrices<T extends ResearchMatrixStore>(primary: Map<string, T>, loadFallback: () => Promise<Map<string, T> | null>): Promise<ReconciliationResult> {
  const apiCoverage = assessResearchCoverage(primary.values())
  if (apiCoverage.missing.length === 0) return {
    expectedCount: apiCoverage.expectedCount, apiMatrixCount: apiCoverage.providedCount, csvSupplementCount: 0,
    finalMatrixCount: apiCoverage.providedCount, missing: [], csvFallbackUsed: false,
  }
  const fallback = await loadFallback()
  if (!fallback) return {
    expectedCount: apiCoverage.expectedCount, apiMatrixCount: apiCoverage.providedCount, csvSupplementCount: 0,
    finalMatrixCount: apiCoverage.providedCount, missing: apiCoverage.missing, csvFallbackUsed: false,
  }
  const primaryIdentities = new Set([...primary.values()].map((store) => identityKey(store.commodity, store.country)))
  const missingIdentities = new Set(apiCoverage.missing.map((item) => identityKey(item.commodity, item.country)))
  let csvSupplementCount = 0
  for (const store of fallback.values()) {
    const identity = identityKey(store.commodity, store.country)
    if (!missingIdentities.has(identity) || primaryIdentities.has(identity)) continue
    primary.set(storageKey(store), store)
    primaryIdentities.add(identity)
    csvSupplementCount += 1
  }
  const finalCoverage = assessResearchCoverage(primary.values())
  return {
    expectedCount: finalCoverage.expectedCount, apiMatrixCount: apiCoverage.providedCount, csvSupplementCount,
    finalMatrixCount: finalCoverage.providedCount, missing: finalCoverage.missing, csvFallbackUsed: true,
  }
}

export function formatMissingResearchMatrices(missing: MissingResearchMatrix[]): string {
  return [`缺少 ${missing.length} 个必须研究矩阵：`, ...missing.map((item) => `- commodity=${item.commodity}; country/region=${item.country}; matrixKey=${item.matrixKey}`)].join('\n')
}

type ManifestRequest = { key?: unknown; kind?: unknown; status?: unknown; success?: unknown; finalOutputFile?: unknown }
type ApiManifest = { reportMonth?: unknown; status?: unknown; expectedMetadataRequests?: unknown; expectedPsdRequests?: unknown; requests?: unknown }

export function validateApiBatchManifest(apiMonthDirectory: string, expectedReportMonth: string): string[] {
  const manifestPath = join(apiMonthDirectory, 'manifest.json')
  if (!existsSync(manifestPath)) return []
  let manifest: ApiManifest
  try { manifest = JSON.parse(readFileSync(manifestPath, 'utf8')) as ApiManifest } catch (error) {
    return [`API manifest 无法解析：${error instanceof Error ? error.message : String(error)}`]
  }
  const errors: string[] = []
  if (manifest.status !== 'success') errors.push(`API manifest status=${String(manifest.status)}，预期 success`)
  if (manifest.reportMonth !== expectedReportMonth) errors.push(`API manifest reportMonth=${String(manifest.reportMonth)}，预期 ${expectedReportMonth}`)
  const requests = Array.isArray(manifest.requests) ? manifest.requests as ManifestRequest[] : []
  const expectedMetadata = Number(manifest.expectedMetadataRequests)
  const expectedPsd = Number(manifest.expectedPsdRequests)
  if (!Number.isInteger(expectedMetadata) || !Number.isInteger(expectedPsd)) errors.push('API manifest 缺少有效 expected request 数')
  if (requests.length !== expectedMetadata + expectedPsd) errors.push(`API manifest request 数=${requests.length}，预期 ${expectedMetadata + expectedPsd}`)
  const keys = requests.map((request) => String(request.key ?? ''))
  if (new Set(keys).size !== keys.length || keys.some((key) => !key)) errors.push('API manifest request key 缺失或不唯一')
  const metadataCount = requests.filter((request) => request.kind === 'metadata').length
  const psdCount = requests.filter((request) => request.kind === 'psd').length
  if (metadataCount !== expectedMetadata) errors.push(`API manifest metadata request 数=${metadataCount}，预期 ${expectedMetadata}`)
  if (psdCount !== expectedPsd) errors.push(`API manifest PSD request 数=${psdCount}，预期 ${expectedPsd}`)
  for (const request of requests) {
    if (request.status !== 'success' || request.success !== true) errors.push(`API request 未成功：${String(request.key)}`)
    else if (typeof request.finalOutputFile !== 'string' || !existsSync(join(apiMonthDirectory, request.finalOutputFile))) errors.push(`API request 输出文件缺失：${String(request.key)}`)
  }
  return errors
}

export function isWorldApiPsdFile(fileName: string): boolean {
  return /__world__\d{4}\.json$/i.test(fileName)
}
