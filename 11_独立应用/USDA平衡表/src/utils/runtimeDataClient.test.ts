import assert from 'node:assert/strict'
import test from 'node:test'
import { APP_CONTRACT_VERSION, RuntimeDataClient, resolveRuntimeDataClient, validateRuntimePointer } from './runtimeDataClient'

const pointer = (release = 'usda-2026-08-0123456789abcdef') => ({
  release_id: release,
  report_month: '2026-08',
  previous_report_month: '2026-07',
  data_schema_version: 1,
  minimum_app_contract_version: APP_CONTRACT_VERSION,
})

const jsonResponse = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })

test('current pointer schema rejects traversal, URLs, unsupported schema and app contract', () => {
  assert.equal(validateRuntimePointer(pointer()).report_month, '2026-08')
  for (const release_id of ['../x', '/absolute', 'https://evil/x', 'usda-2026-08-XYZ', 'usda-2026-08-0123456789abcdef/x']) {
    assert.throws(() => validateRuntimePointer({ ...pointer(), release_id }), /release_id/)
  }
  assert.throws(() => validateRuntimePointer({ ...pointer(), data_schema_version: 2 }), /data schema/)
  assert.throws(() => validateRuntimePointer({ ...pointer(), minimum_app_contract_version: 2 }), /contract/)
})

test('default fetch wrapper invokes global fetch without rebinding it as a client method', async () => {
  const originalFetch = globalThis.fetch
  const urls: string[] = []
  globalThis.fetch = (async function (this: unknown, input: RequestInfo | URL) {
    assert.equal(this, undefined)
    const url = String(input)
    urls.push(url)
    if (url.endsWith('/data/current.json')) return jsonResponse(pointer())
    return jsonResponse({ url })
  }) as typeof fetch
  try {
    const client = await resolveRuntimeDataClient()
    await client.readJson('index.json')
    assert.equal(client.pointer.release_id, pointer().release_id)
    assert.ok(urls[1].includes(`/releases/${pointer().release_id}/data/index.json`))
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('one resolved page client remains pinned when current switches', async () => {
  const urls: string[] = []
  let current = pointer('usda-2026-08-aaaaaaaaaaaaaaaa')
  const fetcher = (async (input: RequestInfo | URL) => {
    const url = String(input)
    urls.push(url)
    if (url.endsWith('/data/current.json')) return jsonResponse(current)
    return jsonResponse({ url })
  }) as typeof fetch
  const client = await resolveRuntimeDataClient(fetcher)
  current = { ...current, release_id: 'usda-2026-09-bbbbbbbbbbbbbbbb', report_month: '2026-09', previous_report_month: '2026-08' }
  await client.readJson('index.json')
  await client.readJson('matrix/test.json')
  assert.equal(urls.filter((url) => url.endsWith('/data/current.json')).length, 1)
  assert.ok(urls.slice(1).every((url) => url.includes('/releases/usda-2026-08-aaaaaaaaaaaaaaaa/data/')))
  const refreshed = await resolveRuntimeDataClient(fetcher)
  assert.equal(refreshed.pointer.release_id, 'usda-2026-09-bbbbbbbbbbbbbbbb')
})

test('runtime data client rejects paths that can escape its immutable release', async () => {
  const client = new RuntimeDataClient(pointer(), (async () => jsonResponse({})) as typeof fetch)
  await assert.rejects(() => client.readJson('../secret.json'), /path/)
  await assert.rejects(() => client.readJson('matrix\\secret.json'), /path/)
})
