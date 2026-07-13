import { mkdirSync, writeFileSync } from 'node:fs'
import { request } from 'node:https'
import { join } from 'node:path'
import { SocksProxyAgent } from 'socks-proxy-agent'
import { RESEARCH_SCOPE } from './researchScope'
import { readUsdaApiConfig } from './usdaApiConfig'

type ApiItem = Record<string, unknown>
type ApiResponse = { status: number; body: unknown }
type AuthMode = 'X-Api-Key header' | 'api_key query parameter'
type Mapping = { requested: string; code: string | null; matchedName: string | null }
type RequestFailure = { endpoint: string; status: number | null; message: string }
type DownloadResult = { commodity: string; country: string; marketYear: number; status: 'downloaded' | 'empty' | 'failed'; failure?: RequestFailure }

const API_BASE = 'https://api.fas.usda.gov'
const OUTPUT_DIRECTORY = join(process.cwd(), 'output')

function maskKey(key: string): string {
  return key.length <= 8 ? '***' : `${key.slice(0, 4)}***${key.slice(-4)}`
}

function safeMessage(value: unknown, apiKey: string): string {
  const message = value instanceof Error ? value.message : String(value)
  return message.replaceAll(apiKey, maskKey(apiKey)).replace(/api_key=[^&\s]+/gi, 'api_key=***')
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

function requestJson(endpoint: string, apiKey: string, authMode: AuthMode, proxyUrl: string | undefined, requestCounter: { value: number }): Promise<ApiResponse> {
  const url = new URL(`${API_BASE}${endpoint}`)
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (authMode === 'X-Api-Key header') headers['X-Api-Key'] = apiKey
  else url.searchParams.set('api_key', apiKey)
  const agent = proxyUrl?.toLowerCase().startsWith('socks') ? new SocksProxyAgent(proxyUrl) : undefined
  requestCounter.value += 1
  return new Promise((resolve, reject) => {
    const requestHandle = request(url, { method: 'GET', headers, agent, timeout: 30000 }, (response) => {
      const chunks: Buffer[] = []
      response.on('data', (chunk: Buffer) => chunks.push(chunk))
      response.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8')
        let body: unknown = null
        try { body = text === '' ? null : JSON.parse(text) } catch { reject(new Error(`接口 ${endpoint} 返回了非 JSON 内容。`)); return }
        resolve({ status: response.statusCode ?? 0, body })
      })
    })
    requestHandle.on('timeout', () => requestHandle.destroy(new Error(`接口 ${endpoint} 请求超时。`)))
    requestHandle.on('error', reject)
    requestHandle.end()
  })
}

async function requestWithAuthFallback(endpoint: string, apiKey: string, state: { authMode: AuthMode; proxyUrl?: string; requestCounter: { value: number } }): Promise<ApiResponse> {
  const response = await requestJson(endpoint, apiKey, state.authMode, state.proxyUrl, state.requestCounter)
  if ((response.status === 401 || response.status === 403) && state.authMode === 'X-Api-Key header') {
    state.authMode = 'api_key query parameter'
    return requestJson(endpoint, apiKey, state.authMode, state.proxyUrl, state.requestCounter)
  }
  return response
}

async function runWithConcurrency<T>(tasks: Array<() => Promise<T>>, limit: number): Promise<T[]> {
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

function writeReport(report: Record<string, unknown>) {
  mkdirSync(OUTPUT_DIRECTORY, { recursive: true })
  writeFileSync(join(OUTPUT_DIRECTORY, 'usda_api_fetch_report.json'), `${JSON.stringify(report, null, 2)}\n`, 'utf8')
  const commodityMappings = report.commodityMappings as Mapping[]
  const countryMappings = report.countryMappings as Mapping[]
  const failures = report.failedRequests as RequestFailure[]
  const emptyRequests = report.emptyDataRequests as DownloadResult[]
  const yearsByCommodity = report.marketYearsByCommodity as Record<string, number[]>
  const lines = [
    '# USDA FAS PSD API 拉取报告', '',
    `- 报告月份：${report.reportMonth}`,
    `- 拉取时间：${report.fetchedAt}`,
    `- API key：${report.apiKeyStatus}`,
    `- 代理：${report.proxyUsed ? '已使用 SOCKS5 代理' : '未使用代理'}`,
    `- 认证方式：${report.authMode}`,
    `- API 请求数量：${report.apiRequestCount}`,
    '', '## 商品映射', '', '| 研究商品 | API 名称 | Commodity Code |', '| --- | --- | --- |',
    ...commodityMappings.map((item) => `| ${item.requested} | ${item.matchedName ?? '未匹配'} | ${item.code ?? '未匹配'} |`),
    '', '## 国家映射', '', '| 研究国家 | API 名称 | Country Code |', '| --- | --- | --- |',
    ...countryMappings.map((item) => `| ${item.requested} | ${item.matchedName ?? '未匹配'} | ${item.code ?? '未匹配'} |`),
    '', '## 各商品拉取的市场年度', '',
    ...Object.entries(yearsByCommodity).map(([commodity, years]) => `- ${commodity}: ${years.join(', ') || '无'}`),
    '', `## 失败接口（${failures.length}）`, '',
    ...(failures.length ? failures.map((item) => `- ${item.endpoint}: ${item.status ?? '无 HTTP 响应'}，${item.message}`) : ['无。']),
    '', `## 空数据接口（${emptyRequests.length}）`, '',
    ...(emptyRequests.length ? emptyRequests.map((item) => `- ${item.commodity} / ${item.country} / ${item.marketYear}`) : ['无。']),
  ]
  writeFileSync(join(OUTPUT_DIRECTORY, 'usda_api_fetch_report.md'), `${lines.join('\n')}\n`, 'utf8')
}

async function fetchUsdaPsdApi() {
  const apiKey = process.env.USDA_API_KEY
  if (!apiKey) throw new Error('未设置 USDA_API_KEY 环境变量。')
  const config = readUsdaApiConfig()
  const proxyUrl = process.env.ALL_PROXY || process.env.all_proxy
  const state = { authMode: 'X-Api-Key header' as AuthMode, proxyUrl, requestCounter: { value: 0 } }
  const rawRoot = join(process.cwd(), 'data', 'raw', 'usda_psd_api', config.reportMonth)
  const metadataDirectory = join(rawRoot, 'metadata')
  const psdDirectory = join(rawRoot, 'psd')
  mkdirSync(metadataDirectory, { recursive: true })
  mkdirSync(psdDirectory, { recursive: true })
  console.log(`USDA API key 已启用：${maskKey(apiKey)}`)

  const metadataEndpoints = ['/api/psd/commodities', '/api/psd/countries', '/api/psd/commodityAttributes', '/api/psd/unitsOfMeasure']
  const metadata = new Map<string, ApiItem[]>()
  const failedRequests: RequestFailure[] = []
  for (const endpoint of metadataEndpoints) {
    try {
      const response = await requestWithAuthFallback(endpoint, apiKey, state)
      if (response.status < 200 || response.status >= 300) {
        failedRequests.push({ endpoint, status: response.status, message: '接口未返回成功状态。' })
        continue
      }
      writeFileSync(join(metadataDirectory, `${endpoint.split('/').at(-1)}.json`), `${JSON.stringify(response.body, null, 2)}\n`, 'utf8')
      metadata.set(endpoint, unwrapItems(response.body))
    } catch (error: unknown) {
      failedRequests.push({ endpoint, status: null, message: safeMessage(error, apiKey) })
    }
  }

  const commodityNames = Object.keys(RESEARCH_SCOPE)
  const countryNames = [...new Set(Object.values(RESEARCH_SCOPE).flat())]
  const commodityMappings = mapMetadata(metadata.get('/api/psd/commodities') ?? [], commodityNames, ['commodityDescription', 'description', 'commodityName'], ['commodityCode', 'code'])
  const countryMappings = mapMetadata(metadata.get('/api/psd/countries') ?? [], countryNames, ['countryName', 'description', 'name'], ['countryCode', 'code'])
  const commodityByName = new Map(commodityMappings.filter((item): item is Mapping & { code: string } => item.code !== null).map((item) => [item.requested, item]))
  const countryByName = new Map(countryMappings.filter((item): item is Mapping & { code: string } => item.code !== null).map((item) => [item.requested, item]))
  const downloads: DownloadResult[] = []
  const marketYearsByCommodity: Record<string, number[]> = Object.fromEntries(commodityNames.map((commodity) => [commodity, []]))
  const tasks: Array<() => Promise<DownloadResult>> = []
  for (const [commodity, countries] of Object.entries(RESEARCH_SCOPE)) {
    const commodityCode = commodityByName.get(commodity)?.code
    if (!commodityCode) continue
    for (const country of countries) {
      const countryCode = countryByName.get(country)?.code
      if (!countryCode) continue
      for (let year = config.startYear; year <= config.endYear; year += 1) {
        tasks.push(async () => {
          const endpoint = `/api/psd/commodity/${encodeURIComponent(commodityCode)}/country/${encodeURIComponent(countryCode)}/year/${year}`
          try {
            const response = await requestWithAuthFallback(endpoint, apiKey, state)
            if (response.status < 200 || response.status >= 300) return { commodity, country, marketYear: year, status: 'failed', failure: { endpoint, status: response.status, message: '接口未返回成功状态。' } }
            const items = unwrapItems(response.body)
            writeFileSync(join(psdDirectory, `${commodityCode}_${countryCode}_${year}.json`), `${JSON.stringify(response.body, null, 2)}\n`, 'utf8')
            return { commodity, country, marketYear: year, status: items.length === 0 ? 'empty' : 'downloaded' }
          } catch (error: unknown) {
            return { commodity, country, marketYear: year, status: 'failed', failure: { endpoint, status: null, message: safeMessage(error, apiKey) } }
          }
        })
      }
    }
  }
  for (const commodity of commodityNames) {
    const commodityCode = commodityByName.get(commodity)?.code
    if (!commodityCode) continue
    for (let year = config.startYear; year <= config.endYear; year += 1) {
      tasks.push(async () => {
        const endpoint = `/api/psd/commodity/${encodeURIComponent(commodityCode)}/world/year/${year}`
        try {
          const response = await requestWithAuthFallback(endpoint, apiKey, state)
          if (response.status < 200 || response.status >= 300) return { commodity, country: 'World', marketYear: year, status: 'failed', failure: { endpoint, status: response.status, message: '接口未返回成功状态。' } }
          const items = unwrapItems(response.body)
          writeFileSync(join(psdDirectory, `${commodityCode}_WORLD_${year}.json`), `${JSON.stringify(response.body, null, 2)}\n`, 'utf8')
          return { commodity, country: 'World', marketYear: year, status: items.length === 0 ? 'empty' : 'downloaded' }
        } catch (error: unknown) {
          return { commodity, country: 'World', marketYear: year, status: 'failed', failure: { endpoint, status: null, message: safeMessage(error, apiKey) } }
        }
      })
    }
  }
  downloads.push(...await runWithConcurrency(tasks, 4))
  for (const item of downloads.filter((item) => item.status === 'downloaded' || item.status === 'empty')) marketYearsByCommodity[item.commodity].push(item.marketYear)
  for (const item of downloads.filter((item) => item.status === 'failed' && item.failure)) failedRequests.push(item.failure!)
  for (const commodity of Object.keys(marketYearsByCommodity)) marketYearsByCommodity[commodity] = [...new Set(marketYearsByCommodity[commodity])].sort((a, b) => a - b)

  const report = {
    reportMonth: config.reportMonth,
    fetchedAt: new Date().toISOString(),
    apiKeyStatus: `已使用（${maskKey(apiKey)}）`,
    proxyUsed: Boolean(proxyUrl),
    proxyProtocol: proxyUrl?.split(':')[0] ?? null,
    authMode: state.authMode,
    commodityMappings,
    countryMappings,
    marketYearsByCommodity,
    failedRequests,
    emptyDataRequests: downloads.filter((item) => item.status === 'empty'),
    apiRequestCount: state.requestCounter.value,
    rawDataDirectory: rawRoot,
  }
  writeReport(report)
  console.log(`USDA API 拉取完成：${state.requestCounter.value} 个请求，失败 ${failedRequests.length} 个，空数据 ${report.emptyDataRequests.length} 个。`)
  if (failedRequests.length > 0) process.exitCode = 1
}

fetchUsdaPsdApi().catch((error: unknown) => {
  const apiKey = process.env.USDA_API_KEY ?? ''
  console.error(safeMessage(error, apiKey))
  process.exitCode = 1
})
