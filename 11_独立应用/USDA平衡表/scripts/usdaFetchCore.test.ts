import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import {
  RequestPacer, createPlannedManifest, createRateLimitEvidence, promoteRunAtomically, redactSecret, requestWithRetry,
  validateManifest, writeJsonAtomic, type FetchManifest, type FetchSettings, type ManifestRequest, type TransportResponse,
} from './usdaFetchCore'

const settings: FetchSettings = { concurrency: 1, intervalMs: 0, maxRetries: 3, backoffBaseMs: 2000, backoffMaxMs: 30000, jitterMs: 0 }
const response = (status: number, headers: TransportResponse['headers'] = {}, body: unknown = []): TransportResponse => ({ status, headers, body })
const noSleep = async (_ms: number) => {}

test('normal 200 succeeds without retry and captures rate-limit evidence', async () => {
  const evidence = createRateLimitEvidence()
  const result = await requestWithRetry(async () => response(200, { 'x-ratelimit-limit': '1000', 'x-ratelimit-remaining': '999' }), settings, evidence, { sleep: noSleep })
  assert.equal(result.attempts, 1)
  assert.equal(evidence.firstLimit, 1000)
  assert.equal(evidence.minimumRemaining, 999)
})

test('request pacer enforces the configured interval between starts', async () => {
  let now = 1000; const sleeps: number[] = []
  const pacer = new RequestPacer(1000, async (ms) => { sleeps.push(ms); now += ms }, () => now)
  await pacer.waitTurn()
  await pacer.waitTurn()
  await pacer.waitTurn()
  assert.deepEqual(sleeps, [1000, 1000])
})

test('429 honors Retry-After and then succeeds', async () => {
  const sleeps: number[] = []; let calls = 0; const evidence = createRateLimitEvidence()
  const result = await requestWithRetry(async () => ++calls === 1 ? response(429, { 'retry-after': '3', 'x-ratelimit-remaining': '0' }) : response(200), settings, evidence, { sleep: async (ms) => { sleeps.push(ms) } })
  assert.equal(result.attempts, 2)
  assert.deepEqual(sleeps, [3000])
  assert.equal(evidence.first429RetryAfter, '3')
  assert.equal(evidence.retryCount, 1)
})

test('429 without Retry-After uses bounded exponential backoff', async () => {
  const sleeps: number[] = []; let calls = 0
  const result = await requestWithRetry(async () => ++calls <= 2 ? response(429) : response(200), settings, createRateLimitEvidence(), { sleep: async (ms) => { sleeps.push(ms) }, random: () => 0 })
  assert.equal(result.attempts, 3)
  assert.deepEqual(sleeps, [2000, 4000])
})

test('429 stops after maxRetries', async () => {
  const result = await requestWithRetry(async () => response(429), { ...settings, maxRetries: 2 }, createRateLimitEvidence(), { sleep: noSleep, random: () => 0 })
  assert.equal(result.response.status, 429)
  assert.equal(result.attempts, 3)
})

test('500 and 503 retry but ordinary 400 does not', async () => {
  for (const status of [500, 503]) {
    let calls = 0
    const result = await requestWithRetry(async () => ++calls === 1 ? response(status) : response(200), settings, createRateLimitEvidence(), { sleep: noSleep, random: () => 0 })
    assert.equal(result.attempts, 2)
  }
  const result = await requestWithRetry(async () => response(400), settings, createRateLimitEvidence(), { sleep: noSleep })
  assert.equal(result.attempts, 1)
})

test('full research manifest contains 4 metadata and 468 PSD request keys', () => {
  const scope = {
    a: Array.from({ length: 5 }, (_, i) => `a${i}`), b: Array.from({ length: 5 }, (_, i) => `b${i}`), c: Array.from({ length: 5 }, (_, i) => `c${i}`),
    d: Array.from({ length: 6 }, (_, i) => `d${i}`), e: Array.from({ length: 5 }, (_, i) => `e${i}`), f: Array.from({ length: 6 }, (_, i) => `f${i}`),
    g: Array.from({ length: 3 }, (_, i) => `g${i}`), h: Array.from({ length: 4 }, (_, i) => `h${i}`), i: Array.from({ length: 4 }, (_, i) => `i${i}`),
  }
  const manifest = createPlannedManifest('2026-08', 'run', ['m1', 'm2', 'm3', 'm4'], Object.keys(scope), scope, 2018, 2026)
  assert.equal(manifest.expectedMetadataRequests, 4)
  assert.equal(manifest.expectedPsdRequests, 468)
  assert.equal(new Set(manifest.requests.map((item) => item.key)).size, 472)
})

function successfulManifest(runDirectory: string, runId: string): FetchManifest {
  mkdirSync(join(runDirectory, 'metadata'), { recursive: true }); mkdirSync(join(runDirectory, 'psd'), { recursive: true })
  const requests: ManifestRequest[] = [
    { key: 'metadata:x', kind: 'metadata', endpoint: '/x', commodity: null, country: null, marketYear: null, expectedOutputFile: 'metadata/x.json', status: 'success', attempts: 1, httpStatus: 200, success: true, finalOutputFile: 'metadata/x.json', rowCount: 1, months: [], error: null },
    { key: 'psd:a:b:2026', kind: 'psd', endpoint: '/p', commodity: 'a', country: 'b', marketYear: 2026, expectedOutputFile: 'psd/a_b_2026.json', status: 'success', attempts: 1, httpStatus: 200, success: true, finalOutputFile: 'psd/a_b_2026.json', rowCount: 1, months: ['08'], error: null },
  ]
  writeFileSync(join(runDirectory, 'metadata/x.json'), '[]'); writeFileSync(join(runDirectory, 'psd/a_b_2026.json'), '[]')
  const manifest: FetchManifest = { schemaVersion: 1, runId, reportMonth: '2026-08', createdAt: '', completedAt: '', status: 'success', expectedMetadataRequests: 1, expectedPsdRequests: 1, requests }
  writeJsonAtomic(join(runDirectory, 'manifest.json'), manifest)
  return manifest
}

test('missing request or file cannot pass manifest completeness', () => {
  const root = mkdtempSync(join(tmpdir(), 'usda-fetch-')); const run = join(root, 'run'); const manifest = successfulManifest(run, 'run')
  manifest.expectedPsdRequests = 2
  assert.ok(validateManifest(manifest, run).length > 0)
})

test('failed candidate cannot promote and leaves official raw unchanged', () => {
  const root = mkdtempSync(join(tmpdir(), 'usda-fetch-')); const run = join(root, 'run'); const official = join(root, '2026-08'); mkdirSync(official); writeFileSync(join(official, 'sentinel'), 'old')
  const manifest = successfulManifest(run, 'run'); manifest.status = 'failed'; writeJsonAtomic(join(run, 'manifest.json'), manifest)
  assert.throws(() => promoteRunAtomically(run, official, join(root, '_legacy')))
  assert.equal(readFileSync(join(official, 'sentinel'), 'utf8'), 'old')
})

test('successful candidate promotes atomically and isolates old official directory', () => {
  const root = mkdtempSync(join(tmpdir(), 'usda-fetch-')); const run = join(root, '_runs/run'); const official = join(root, '2026-08'); mkdirSync(official, { recursive: true }); writeFileSync(join(official, 'sentinel'), 'old'); successfulManifest(run, 'run')
  const result = promoteRunAtomically(run, official, join(root, '_legacy'))
  assert.ok(result.legacyPath)
  assert.equal(readFileSync(join(result.legacyPath!, 'sentinel'), 'utf8'), 'old')
  assert.equal(JSON.parse(readFileSync(join(official, 'manifest.json'), 'utf8')).status, 'success')
})

test('separate run directories and stale official files cannot satisfy a new manifest', () => {
  const root = mkdtempSync(join(tmpdir(), 'usda-fetch-')); const oldRun = join(root, 'old'); const newRun = join(root, 'new'); successfulManifest(oldRun, 'old'); const manifest = successfulManifest(newRun, 'new')
  manifest.requests[1].finalOutputFile = 'psd/missing.json'; manifest.requests[1].expectedOutputFile = 'psd/missing.json'
  assert.ok(validateManifest(manifest, newRun).some((error) => error.includes('missing')))
})

test('API key is redacted from errors and query strings', () => {
  const secret = 'super-secret-key'
  const output = redactSecret(new Error(`failed ${secret} api_key=${secret}`), secret)
  assert.equal(output.includes(secret), false)
  assert.match(output, /\*\*\*/)
})
