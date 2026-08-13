import { mkdirSync, writeFileSync } from 'node:fs'
import { request } from 'node:https'
import { join } from 'node:path'
import { SocksProxyAgent } from 'socks-proxy-agent'
import { RESEARCH_SCOPE } from './researchScope'
import { readUsdaApiConfig } from './usdaApiConfig'
import {
  RequestPacer, createPlannedManifest, createRateLimitEvidence, createRunId, promoteRunAtomically, readFetchSettings,
  redactSecret, requestWithRetry, runWithConcurrency, validateManifest, writeJsonAtomic,
  type FetchManifest, type ManifestRequest, type RateLimitEvidence, type TransportResponse,
} from './usdaFetchCore'

type ApiItem = Record<string, unknown>
type AuthMode = 'X-Api-Key header' | 'api_key query parameter'
type Mapping = { requested: string; code: string | null; matchedName: string | null }
type RequestFailure = { endpoint: string; status: number | null; message: string }

const API_BASE = 'https://api.fas.usda.gov'
const METADATA_ENDPOINTS = ['/api/psd/commodities', '/api/psd/countries', '/api/psd/commodityAttributes', '/api/psd/unitsOfMeasure'] as const
const RAW_ROOT = join(process.cwd(), 'data', 'raw', 'usda_psd_api')
const OUTPUT_DIRECTORY = join(process.cwd(), 'output')

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

function safeMessage(value: unknown, apiKey: string): string {
  return redactSecret(value, apiKey)
}

function unwrapItems(body: unknown): ApiItem[] {
  if (Array.isArray(body)) return body.filter((item): item is ApiItem => Boolean(item) && typeof item === 'object')
  if (body && typeof body === 'object') {
    const record = body as Record<string, unknown>
    for (const key of ['data', 'Data', 'items', 'Items', 'results', 'Results']) {
      if (Array.isArray(record[key])) return record[key].filter((item): item is ApiItem => Boolean(item) && typeof item === 'object')
    }
  }
  return []
}

function valueFor(item: ApiItem, candidates: string[]): string | null {
  const normalized = new Map(Object.entries(item).map(([key, value]) => [key.replace(/[^a-z0-9]/gi, '').toLowerCase(), value]))
  for (const candidate of candidates) {
    const value = normalized.get(candidate.replace(/[^a-z0-9]/gi, '').toLowerCase())
    if (value !== undefined && value !== null) return String(value).trim()
  }
  return null
}

function mapMetadata(items: ApiItem[], requestedNames: string[], descriptionFields: string[], codeFields: string[]): Mapping[] {
  return requestedNames.map((requested) => {
    const match = items.find((item) => valueFor(item, descriptionFields)?.toLowerCase() === requested.toLowerCase())
    return { requested, code: match ? valueFor(match, codeFields) : null, matchedName: match ? valueFor(match, descriptionFields) : null }
  })
}

function requestJson(endpoint: string, apiKey: string, authMode: AuthMode, proxyUrl: string | undefined, requestCounter: { value: number }): Promise<TransportResponse> {
  const url = new URL(`${API_BASE}${endpoint}`)
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (authMode === 'X-Api-Key header') headers['X-Api-Key'] = apiKey
  else url.searchParams.set('api_key', apiKey)
  const agent = proxyUrl?.toLowerCase().startsWith('socks') ? new SocksProxyAgent(proxyUrl) : undefined
  requestCounter.value += 1
  return new Promise((resolve, reject) => {
    const handle = request(url, { method: 'GET', headers, agent, timeout: 30000 }, (response) => {
      const chunks: Buffer[] = []
      response.on('data', (chunk: Buffer) => chunks.push(chunk))
      response.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8')
        try {
          resolve({ status: response.statusCode ?? 0, body: text === '' ? null : JSON.parse(text), headers: response.headers })
        } catch { reject(new Error(`Endpoint ${endpoint} returned non-JSON content.`)) }
      })
    })
    handle.on('timeout', () => handle.destroy(new Error(`Endpoint ${endpoint} timed out.`)))
    handle.on('error', reject)
    handle.end()
  })
}

async function requestWithAuthFallback(endpoint: string, apiKey: string, state: { authMode: AuthMode; proxyUrl?: string; requestCounter: { value: number } }): Promise<TransportResponse> {
  const response = await requestJson(endpoint, apiKey, state.authMode, state.proxyUrl, state.requestCounter)
  if ((response.status === 401 || response.status === 403) && state.authMode === 'X-Api-Key header') {
    state.authMode = 'api_key query parameter'
    return requestJson(endpoint, apiKey, state.authMode, state.proxyUrl, state.requestCounter)
  }
  return response
}

function requestEntry(manifest: FetchManifest, key: string): ManifestRequest {
  const entry = manifest.requests.find((item) => item.key === key)
  if (!entry) throw new Error(`Manifest request is missing: ${key}`)
  return entry
}

function updateEntry(entry: ManifestRequest, response: TransportResponse, attempts: number, outputFile: string | null, apiKey: string) {
  const items = unwrapItems(response.body)
  entry.attempts = attempts
  entry.httpStatus = response.status
  entry.rowCount = items.length
  entry.months = [...new Set(items.map((item) => valueFor(item, ['month'])).filter((value): value is string => value !== null))].sort()
  if (response.status >= 200 && response.status < 300) {
    entry.status = items.length === 0 && entry.kind === 'psd' ? 'empty' : 'success'
    entry.success = true
    entry.finalOutputFile = outputFile
  } else {
    entry.status = 'failed'
    entry.error = safeMessage(`HTTP ${response.status}`, apiKey)
  }
}

function writeRunReport(runDirectory: string, report: Record<string, unknown>) {
  writeJsonAtomic(join(runDirectory, 'fetch_report.json'), report)
  mkdirSync(OUTPUT_DIRECTORY, { recursive: true })
  writeJsonAtomic(join(OUTPUT_DIRECTORY, 'usda_api_fetch_report.json'), report)
  const rateLimit = report.rateLimit as RateLimitEvidence
  const lines = [
    '# USDA FAS PSD API fetch report', '',
    `- Report month: ${report.reportMonth}`,
    `- Run ID: ${report.runId}`,
    `- Status: ${report.status}`,
    `- API requests including retries: ${report.apiRequestCount}`,
    `- Metadata success: ${report.successfulMetadataRequests}/${report.expectedMetadataRequests}`,
    `- PSD success: ${report.successfulPsdRequests}/${report.expectedPsdRequests}`,
    `- Failed requests: ${(report.failedRequests as RequestFailure[]).length}`,
    `- Retries: ${rateLimit.retryCount}`,
    `- First observed rate limit: ${rateLimit.firstLimit ?? 'not returned'}`,
    `- Minimum remaining: ${rateLimit.minimumRemaining ?? 'not returned'}`,
    `- First 429 remaining: ${rateLimit.first429Remaining ?? 'not returned'}`,
    `- First 429 Retry-After: ${rateLimit.first429RetryAfter ?? 'not returned'}`,
    `- Promoted: ${report.promoted}`,
  ]
  writeFileSync(join(runDirectory, 'fetch_report.md'), `${lines.join('\n')}\n`, 'utf8')
  writeFileSync(join(OUTPUT_DIRECTORY, 'usda_api_fetch_report.md'), `${lines.join('\n')}\n`, 'utf8')
}

async function fetchUsdaPsdApi() {
  const apiKey = process.env.USDA_API_KEY
  if (!apiKey) throw new Error('USDA_API_KEY is not configured.')
  const config = readUsdaApiConfig()
  const settings = readFetchSettings(process.env)
  const reportMonth = process.env.USDA_FETCH_REPORT_MONTH ?? config.reportMonth
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(reportMonth)) throw new Error('USDA_FETCH_REPORT_MONTH must use YYYY-MM format.')
  const startYear = process.env.USDA_FETCH_START_YEAR ? Number(process.env.USDA_FETCH_START_YEAR) : config.startYear
  const endYear = process.env.USDA_FETCH_END_YEAR ? Number(process.env.USDA_FETCH_END_YEAR) : config.endYear
  if (!Number.isInteger(startYear) || !Number.isInteger(endYear) || startYear > endYear) throw new Error('Smoke-test year range is invalid.')
  const smokeCommodity = process.env.USDA_FETCH_COMMODITY
  const smokeCountry = process.env.USDA_FETCH_COUNTRY
  const includeWorld = process.env.USDA_FETCH_INCLUDE_WORLD !== 'false'
  const scope: Record<string, readonly string[]> = smokeCommodity
    ? { [smokeCommodity]: smokeCountry ? [smokeCountry] : (RESEARCH_SCOPE[smokeCommodity] ?? []) }
    : RESEARCH_SCOPE
  if (Object.keys(scope).length === 0 || Object.values(scope).some((countries) => countries.length === 0)) throw new Error('Fetch scope is empty or invalid.')
  const promote = process.env.USDA_FETCH_PROMOTE !== 'false' && smokeCommodity === undefined && smokeCountry === undefined
  const runId = createRunId()
  const runDirectory = join(RAW_ROOT, '_runs', reportMonth, runId)
  const metadataDirectory = join(runDirectory, 'metadata')
  const psdDirectory = join(runDirectory, 'psd')
  mkdirSync(runDirectory, { recursive: true })
  mkdirSync(metadataDirectory, { recursive: false })
  mkdirSync(psdDirectory, { recursive: false })
  const commodityNames = Object.keys(scope)
  const manifest = createPlannedManifest(reportMonth, runId, METADATA_ENDPOINTS, commodityNames, scope, startYear, endYear, includeWorld)
  const manifestPath = join(runDirectory, 'manifest.json')
  writeJsonAtomic(manifestPath, manifest)

  const proxyUrl = process.env.ALL_PROXY || process.env.all_proxy
  const state = { authMode: 'X-Api-Key header' as AuthMode, proxyUrl, requestCounter: { value: 0 } }
  const evidence = createRateLimitEvidence()
  const pacer = new RequestPacer(settings.intervalMs, sleep)
  const failedRequests: RequestFailure[] = []
  const metadata = new Map<string, ApiItem[]>()
  console.log(`USDA fetch run ${runId}: concurrency=${settings.concurrency}, intervalMs=${settings.intervalMs}, maxRetries=${settings.maxRetries}`)

  for (const endpoint of METADATA_ENDPOINTS) {
    const entry = requestEntry(manifest, `metadata:${endpoint.split('/').at(-1)}`)
    try {
      const result = await requestWithRetry(() => requestWithAuthFallback(endpoint, apiKey, state), settings, evidence, { sleep, pacer })
      const relative = entry.expectedOutputFile
      updateEntry(entry, result.response, result.attempts, relative, apiKey)
      if (entry.success) {
        writeFileSync(join(runDirectory, relative), `${JSON.stringify(result.response.body, null, 2)}\n`, 'utf8')
        metadata.set(endpoint, unwrapItems(result.response.body))
      } else failedRequests.push({ endpoint, status: result.response.status, message: entry.error ?? 'Request failed.' })
    } catch (error) {
      entry.status = 'failed'; entry.error = safeMessage(error, apiKey)
      failedRequests.push({ endpoint, status: null, message: entry.error })
    }
    writeJsonAtomic(manifestPath, manifest)
  }

  const commodityMappings = mapMetadata(metadata.get('/api/psd/commodities') ?? [], commodityNames, ['commodityDescription', 'description', 'commodityName'], ['commodityCode', 'code'])
  const countryNames = [...new Set(Object.values(scope).flat())]
  const countryMappings = mapMetadata(metadata.get('/api/psd/countries') ?? [], countryNames, ['countryName', 'description', 'name'], ['countryCode', 'code'])
  const commodityCodes = new Map(commodityMappings.filter((item): item is Mapping & { code: string } => item.code !== null).map((item) => [item.requested, item.code]))
  const countryCodes = new Map(countryMappings.filter((item): item is Mapping & { code: string } => item.code !== null).map((item) => [item.requested, item.code]))

  for (const entry of manifest.requests.filter((item) => item.kind === 'psd')) {
    const commodityCode = commodityCodes.get(entry.commodity ?? '')
    const countryCode = entry.country === 'World' ? 'WORLD' : countryCodes.get(entry.country ?? '')
    if (commodityCode && countryCode && entry.marketYear !== null) {
      entry.endpoint = entry.country === 'World'
        ? `/api/psd/commodity/${encodeURIComponent(commodityCode)}/world/year/${entry.marketYear}`
        : `/api/psd/commodity/${encodeURIComponent(commodityCode)}/country/${encodeURIComponent(countryCode)}/year/${entry.marketYear}`
    }
  }
  writeJsonAtomic(manifestPath, manifest)

  const tasks = manifest.requests.filter((item) => item.kind === 'psd').map((entry) => async () => {
    if (entry.endpoint === null) {
      entry.status = 'failed'; entry.error = 'Metadata mapping is unavailable.'
      failedRequests.push({ endpoint: entry.key, status: null, message: entry.error })
      writeJsonAtomic(manifestPath, manifest)
      return
    }
    try {
      const result = await requestWithRetry(() => requestWithAuthFallback(entry.endpoint!, apiKey, state), settings, evidence, { sleep, pacer })
      updateEntry(entry, result.response, result.attempts, entry.expectedOutputFile, apiKey)
      if (entry.success) writeFileSync(join(runDirectory, entry.expectedOutputFile), `${JSON.stringify(result.response.body, null, 2)}\n`, 'utf8')
      else failedRequests.push({ endpoint: entry.endpoint, status: result.response.status, message: entry.error ?? 'Request failed.' })
    } catch (error) {
      entry.status = 'failed'; entry.error = safeMessage(error, apiKey)
      failedRequests.push({ endpoint: entry.endpoint, status: null, message: entry.error })
    }
    writeJsonAtomic(manifestPath, manifest)
  })
  await runWithConcurrency(tasks, settings.concurrency)

  manifest.completedAt = new Date().toISOString()
  const preliminaryErrors = validateManifest(manifest, runDirectory)
  manifest.status = failedRequests.length === 0 && preliminaryErrors.length === 0 ? 'success' : 'failed'
  writeJsonAtomic(manifestPath, manifest)
  const validationErrors = validateManifest(manifest, runDirectory)
  const report = {
    reportMonth, runId, runDirectory, fetchedAt: manifest.completedAt, status: manifest.status,
    settings, proxyUsed: Boolean(proxyUrl), proxyProtocol: proxyUrl?.split(':')[0] ?? null,
    authMode: state.authMode, apiRequestCount: state.requestCounter.value,
    expectedMetadataRequests: manifest.expectedMetadataRequests, expectedPsdRequests: manifest.expectedPsdRequests,
    successfulMetadataRequests: manifest.requests.filter((item) => item.kind === 'metadata' && item.success).length,
    successfulPsdRequests: manifest.requests.filter((item) => item.kind === 'psd' && item.success).length,
    emptyDataRequests: manifest.requests.filter((item) => item.kind === 'psd' && item.status === 'empty').map((item) => item.key),
    failedRequests, validationErrors, commodityMappings, countryMappings, rateLimit: evidence,
    apiKeyStatus: 'configured (redacted)', promoted: false, officialRawDirectory: join(RAW_ROOT, reportMonth), legacyPath: null as string | null,
  }
  writeRunReport(runDirectory, report)
  if (manifest.status === 'success' && promote) {
    const promoted = promoteRunAtomically(runDirectory, join(RAW_ROOT, reportMonth), join(RAW_ROOT, '_legacy'))
    report.promoted = true
    report.legacyPath = promoted.legacyPath
    report.runDirectory = join(RAW_ROOT, reportMonth)
    writeRunReport(report.runDirectory, report)
  }
  console.log(`USDA fetch ${manifest.status}: PSD ${report.successfulPsdRequests}/${manifest.expectedPsdRequests}, failures ${failedRequests.length}, retries ${evidence.retryCount}, promoted ${report.promoted}.`)
  if (manifest.status !== 'success') process.exitCode = 1
}

fetchUsdaPsdApi().catch((error: unknown) => {
  console.error(safeMessage(error, process.env.USDA_API_KEY ?? ''))
  process.exitCode = 1
})
