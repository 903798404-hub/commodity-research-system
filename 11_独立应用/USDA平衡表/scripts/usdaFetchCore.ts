import { existsSync, mkdirSync, readdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

export type FetchSettings = {
  concurrency: number
  intervalMs: number
  maxRetries: number
  backoffBaseMs: number
  backoffMaxMs: number
  jitterMs: number
}

export type RateLimitHeaders = {
  limit: number | null
  remaining: number | null
  retryAfter: string | null
}

export type RateLimitEvidence = {
  firstLimit: number | null
  minimumRemaining: number | null
  first429Remaining: number | null
  first429RetryAfter: string | null
  retryCount: number
}

export type TransportResponse = { status: number; body: unknown; headers: Record<string, string | string[] | undefined> }
export type RequestAttemptResult = { response: TransportResponse; attempts: number; retries: number }

export type ManifestRequest = {
  key: string
  kind: 'metadata' | 'psd'
  endpoint: string | null
  commodity: string | null
  country: string | null
  marketYear: number | null
  expectedOutputFile: string
  status: 'pending' | 'success' | 'empty' | 'failed'
  attempts: number
  httpStatus: number | null
  success: boolean
  finalOutputFile: string | null
  rowCount: number | null
  months: string[]
  error: string | null
}

export type FetchManifest = {
  schemaVersion: 1
  runId: string
  reportMonth: string
  createdAt: string
  completedAt: string | null
  status: 'running' | 'success' | 'failed'
  expectedMetadataRequests: number
  expectedPsdRequests: number
  requests: ManifestRequest[]
}

function safeRequestFileSegment(value: string): string {
  return value.normalize('NFKD').replace(/[^a-z0-9]+/gi, '_').replace(/^_+|_+$/g, '')
}

export function createPlannedManifest(reportMonth: string, runId: string, metadataEndpoints: readonly string[], commodityNames: string[], countriesByCommodity: Record<string, readonly string[]>, startYear: number, endYear: number, includeWorld = true): FetchManifest {
  const requests: ManifestRequest[] = metadataEndpoints.map((endpoint) => ({
    key: `metadata:${endpoint.split('/').at(-1)}`, kind: 'metadata', endpoint, commodity: null, country: null, marketYear: null,
    expectedOutputFile: `metadata/${endpoint.split('/').at(-1)}.json`, status: 'pending', attempts: 0, httpStatus: null,
    success: false, finalOutputFile: null, rowCount: null, months: [], error: null,
  }))
  for (const commodity of commodityNames) {
    const countries = includeWorld ? [...countriesByCommodity[commodity], 'World'] : [...countriesByCommodity[commodity]]
    for (const country of countries) for (let marketYear = startYear; marketYear <= endYear; marketYear += 1) {
      requests.push({ key: `psd:${commodity}:${country}:${marketYear}`, kind: 'psd', endpoint: null, commodity, country, marketYear,
        expectedOutputFile: `psd/${safeRequestFileSegment(commodity)}__${safeRequestFileSegment(country)}__${marketYear}.json`,
        status: 'pending', attempts: 0, httpStatus: null, success: false, finalOutputFile: null, rowCount: null, months: [], error: null })
    }
  }
  return { schemaVersion: 1, runId, reportMonth, createdAt: new Date().toISOString(), completedAt: null, status: 'running',
    expectedMetadataRequests: metadataEndpoints.length, expectedPsdRequests: requests.filter((item) => item.kind === 'psd').length, requests }
}

function positiveInteger(name: string, raw: string | undefined, fallback: number, allowZero = false): number {
  if (raw === undefined || raw.trim() === '') return fallback
  const value = Number(raw)
  if (!Number.isInteger(value) || (allowZero ? value < 0 : value <= 0)) throw new Error(`${name} must be ${allowZero ? 'a non-negative' : 'a positive'} integer.`)
  return value
}

export function readFetchSettings(env: NodeJS.ProcessEnv): FetchSettings {
  return {
    concurrency: positiveInteger('USDA_FETCH_CONCURRENCY', env.USDA_FETCH_CONCURRENCY, 1),
    intervalMs: positiveInteger('USDA_FETCH_INTERVAL_MS', env.USDA_FETCH_INTERVAL_MS, 1000, true),
    maxRetries: positiveInteger('USDA_FETCH_MAX_RETRIES', env.USDA_FETCH_MAX_RETRIES, 5, true),
    backoffBaseMs: positiveInteger('USDA_FETCH_BACKOFF_BASE_MS', env.USDA_FETCH_BACKOFF_BASE_MS, 2000),
    backoffMaxMs: positiveInteger('USDA_FETCH_BACKOFF_MAX_MS', env.USDA_FETCH_BACKOFF_MAX_MS, 30000),
    jitterMs: positiveInteger('USDA_FETCH_JITTER_MS', env.USDA_FETCH_JITTER_MS, 250, true),
  }
}

export function rateLimitHeaders(headers: TransportResponse['headers']): RateLimitHeaders {
  const normalized = new Map(Object.entries(headers).map(([key, value]) => [key.toLowerCase(), Array.isArray(value) ? value[0] : value]))
  const numberValue = (name: string) => {
    const raw = normalized.get(name)
    if (raw === undefined) return null
    const parsed = Number(raw)
    return Number.isFinite(parsed) ? parsed : null
  }
  return {
    limit: numberValue('x-ratelimit-limit'),
    remaining: numberValue('x-ratelimit-remaining'),
    retryAfter: normalized.get('retry-after') ?? null,
  }
}

export function retryAfterMilliseconds(value: string | null, nowMs: number): number | null {
  if (value === null) return null
  const seconds = Number(value)
  if (Number.isFinite(seconds) && seconds >= 0) return Math.ceil(seconds * 1000)
  const dateMs = Date.parse(value)
  return Number.isFinite(dateMs) ? Math.max(0, dateMs - nowMs) : null
}

export function backoffMilliseconds(retryIndex: number, settings: FetchSettings, random: () => number): number {
  const exponential = Math.min(settings.backoffMaxMs, settings.backoffBaseMs * (2 ** retryIndex))
  return Math.min(settings.backoffMaxMs, exponential + Math.floor(random() * (settings.jitterMs + 1)))
}

export function createRateLimitEvidence(): RateLimitEvidence {
  return { firstLimit: null, minimumRemaining: null, first429Remaining: null, first429RetryAfter: null, retryCount: 0 }
}

export function observeRateLimit(evidence: RateLimitEvidence, response: TransportResponse) {
  const observed = rateLimitHeaders(response.headers)
  if (evidence.firstLimit === null && observed.limit !== null) evidence.firstLimit = observed.limit
  if (observed.remaining !== null) evidence.minimumRemaining = evidence.minimumRemaining === null ? observed.remaining : Math.min(evidence.minimumRemaining, observed.remaining)
  if (response.status === 429 && evidence.first429Remaining === null) {
    evidence.first429Remaining = observed.remaining
    evidence.first429RetryAfter = observed.retryAfter
  }
}

export class RequestPacer {
  private nextStartMs = 0
  private chain: Promise<void> = Promise.resolve()

  constructor(private readonly intervalMs: number, private readonly sleep: (ms: number) => Promise<void>, private readonly now: () => number = Date.now) {}

  async waitTurn() {
    let release!: () => void
    const previous = this.chain
    this.chain = new Promise<void>((resolve) => { release = resolve })
    await previous
    const waitMs = Math.max(0, this.nextStartMs - this.now())
    if (waitMs > 0) await this.sleep(waitMs)
    this.nextStartMs = this.now() + this.intervalMs
    release()
  }
}

export async function requestWithRetry(
  send: () => Promise<TransportResponse>,
  settings: FetchSettings,
  evidence: RateLimitEvidence,
  options: { sleep: (ms: number) => Promise<void>; random?: () => number; now?: () => number; pacer?: RequestPacer },
): Promise<RequestAttemptResult> {
  const random = options.random ?? Math.random
  const now = options.now ?? Date.now
  let attempts = 0
  while (true) {
    if (options.pacer) await options.pacer.waitTurn()
    attempts += 1
    const response = await send()
    observeRateLimit(evidence, response)
    const retryable = response.status === 429 || [500, 502, 503, 504].includes(response.status)
    const retries = attempts - 1
    if (!retryable || retries >= settings.maxRetries) return { response, attempts, retries }
    const retryAfter = response.status === 429 || response.status === 503
      ? retryAfterMilliseconds(rateLimitHeaders(response.headers).retryAfter, now())
      : null
    const delay = Math.min(settings.backoffMaxMs, retryAfter ?? backoffMilliseconds(retries, settings, random))
    evidence.retryCount += 1
    await options.sleep(delay)
  }
}

export async function runWithConcurrency<T>(tasks: Array<() => Promise<T>>, limit: number): Promise<T[]> {
  const results: T[] = []
  let nextIndex = 0
  async function worker() {
    while (nextIndex < tasks.length) {
      const index = nextIndex++
      results[index] = await tasks[index]()
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, tasks.length) }, worker))
  return results
}

export function createRunId(now = new Date()): string {
  return now.toISOString().replace(/[-:.]/g, '')
}

export function redactSecret(value: unknown, secret: string): string {
  const message = value instanceof Error ? value.message : String(value)
  if (secret === '') return message.replace(/api_key=[^&\s]+/gi, 'api_key=***')
  return message.replaceAll(secret, '***').replace(/api_key=[^&\s]+/gi, 'api_key=***')
}

let atomicWriteSequence = 0

export function writeJsonAtomic(filePath: string, value: unknown) {
  mkdirSync(dirname(filePath), { recursive: true })
  atomicWriteSequence += 1
  const temporary = `${filePath}.${process.pid}.${atomicWriteSequence}.tmp`
  writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, 'utf8')
  renameSync(temporary, filePath)
}

export function validateManifest(manifest: FetchManifest, runDirectory: string): string[] {
  const errors: string[] = []
  const metadata = manifest.requests.filter((item) => item.kind === 'metadata')
  const psd = manifest.requests.filter((item) => item.kind === 'psd')
  const keys = new Set(manifest.requests.map((item) => item.key))
  if (keys.size !== manifest.requests.length) errors.push('Manifest contains duplicate request keys.')
  if (metadata.length !== manifest.expectedMetadataRequests) errors.push(`Expected ${manifest.expectedMetadataRequests} metadata requests, found ${metadata.length}.`)
  if (psd.length !== manifest.expectedPsdRequests) errors.push(`Expected ${manifest.expectedPsdRequests} PSD requests, found ${psd.length}.`)
  for (const item of manifest.requests) {
    if (!['success', 'empty'].includes(item.status) || !item.success || item.finalOutputFile === null) errors.push(`Request ${item.key} is incomplete.`)
    else if (!existsSync(join(runDirectory, item.finalOutputFile))) errors.push(`Output file is missing for ${item.key}.`)
  }
  const expectedFiles = manifest.requests.filter((item) => item.success).map((item) => item.finalOutputFile).filter((item): item is string => item !== null)
  if (new Set(expectedFiles).size !== expectedFiles.length) errors.push('Multiple requests reference the same output file.')
  const metadataFiles = existsSync(join(runDirectory, 'metadata')) ? readdirSync(join(runDirectory, 'metadata')).filter((name) => name.endsWith('.json')).length : 0
  const psdFiles = existsSync(join(runDirectory, 'psd')) ? readdirSync(join(runDirectory, 'psd')).filter((name) => name.endsWith('.json')).length : 0
  if (metadataFiles !== metadata.filter((item) => item.success).length) errors.push('Metadata file count does not match the manifest.')
  if (psdFiles !== psd.filter((item) => item.success).length) errors.push('PSD file count does not match the manifest.')
  return errors
}

export function promoteRunAtomically(runDirectory: string, officialDirectory: string, legacyDirectory: string): { legacyPath: string | null } {
  const manifestPath = join(runDirectory, 'manifest.json')
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8')) as FetchManifest
  const errors = validateManifest(manifest, runDirectory)
  if (manifest.status !== 'success') errors.push(`Manifest status is ${manifest.status}, not success.`)
  if (errors.length > 0) throw new Error(`Candidate run is not promotable: ${errors.join(' ')}`)
  mkdirSync(dirname(officialDirectory), { recursive: true })
  let legacyPath: string | null = null
  if (existsSync(officialDirectory)) {
    mkdirSync(legacyDirectory, { recursive: true })
    legacyPath = join(legacyDirectory, `${manifest.reportMonth}_${manifest.runId}_preexisting`)
    if (existsSync(legacyPath)) throw new Error(`Legacy destination already exists: ${legacyPath}`)
    renameSync(officialDirectory, legacyPath)
  }
  try {
    renameSync(runDirectory, officialDirectory)
  } catch (error) {
    if (legacyPath !== null && !existsSync(officialDirectory)) renameSync(legacyPath, officialDirectory)
    throw error
  }
  return { legacyPath }
}
