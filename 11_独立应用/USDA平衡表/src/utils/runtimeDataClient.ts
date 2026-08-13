import { appPath } from './appPath'

export const APP_CONTRACT_VERSION = 1
export const SUPPORTED_DATA_SCHEMA_VERSION = 1

export type RuntimePointer = {
  release_id: string
  report_month: string
  previous_report_month: string
  data_schema_version: number
  minimum_app_contract_version: number
  updated_at?: string
  bundle_sha256?: string
}

type Fetcher = typeof fetch
const defaultFetcher: Fetcher = (input, init) => fetch(input, init)

const RELEASE_ID = /^usda-\d{4}-(?:0[1-9]|1[0-2])-[0-9a-f]{16}$/
const REPORT_MONTH = /^\d{4}-(?:0[1-9]|1[0-2])$/
const SHA256 = /^[0-9a-f]{64}$/

export function validateRuntimePointer(value: unknown): RuntimePointer {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('USDA Runtime pointer 必须是 JSON object')
  const pointer = value as Record<string, unknown>
  const allowed = new Set(['release_id', 'report_month', 'previous_report_month', 'data_schema_version', 'minimum_app_contract_version', 'updated_at', 'bundle_sha256'])
  if (Object.keys(pointer).some((key) => !allowed.has(key))) throw new Error('USDA Runtime pointer 含有未知字段')
  if (typeof pointer.release_id !== 'string' || !RELEASE_ID.test(pointer.release_id)) throw new Error('USDA Runtime release_id 非法')
  if (typeof pointer.report_month !== 'string' || !REPORT_MONTH.test(pointer.report_month)) throw new Error('USDA Runtime report_month 非法')
  if (typeof pointer.previous_report_month !== 'string' || !REPORT_MONTH.test(pointer.previous_report_month)) throw new Error('USDA Runtime previous_report_month 非法')
  if (pointer.report_month === pointer.previous_report_month) throw new Error('USDA Runtime 报告月份不可相同')
  if (pointer.data_schema_version !== SUPPORTED_DATA_SCHEMA_VERSION) throw new Error('当前应用不支持该 USDA data schema')
  if (!Number.isInteger(pointer.minimum_app_contract_version) || Number(pointer.minimum_app_contract_version) < 1) throw new Error('USDA minimum_app_contract_version 非法')
  if (Number(pointer.minimum_app_contract_version) > APP_CONTRACT_VERSION) throw new Error('当前应用 contract 版本过低')
  if (pointer.bundle_sha256 !== undefined && (typeof pointer.bundle_sha256 !== 'string' || !SHA256.test(pointer.bundle_sha256))) throw new Error('USDA bundle_sha256 非法')
  if (pointer.updated_at !== undefined && (typeof pointer.updated_at !== 'string' || Number.isNaN(Date.parse(pointer.updated_at)))) throw new Error('USDA updated_at 非法')
  return pointer as RuntimePointer
}

function safeRelativePath(path: string): string {
  const normalized = path.replace(/^\/+/, '')
  if (!normalized || normalized.includes('\\') || normalized.split('/').some((part) => !part || part === '.' || part === '..')) throw new Error('USDA Runtime data path 非法')
  return normalized
}

export class RuntimeDataClient {
  readonly pointer: RuntimePointer
  readonly baseUrl: string
  private readonly fetcher: Fetcher

  constructor(pointer: RuntimePointer, fetcher: Fetcher = defaultFetcher) {
    this.pointer = validateRuntimePointer(pointer)
    this.fetcher = fetcher
    this.baseUrl = appPath(`data/releases/${pointer.release_id}/data/`)
  }

  async readJson<T>(path: string, signal?: AbortSignal): Promise<T | null> {
    const response = await this.fetcher(`${this.baseUrl}${safeRelativePath(path)}`, { signal })
    const contentType = response.headers.get('content-type') ?? ''
    if (!response.ok || !contentType.includes('application/json')) return null
    return response.json() as Promise<T>
  }
}

export async function resolveRuntimeDataClient(fetcher: Fetcher = defaultFetcher): Promise<RuntimeDataClient> {
  const response = await fetcher(appPath('data/current.json'), { cache: 'no-store' })
  const contentType = response.headers.get('content-type') ?? ''
  if (!response.ok || !contentType.includes('application/json')) throw new Error(`无法读取 USDA Runtime pointer（${response.status}）`)
  return new RuntimeDataClient(validateRuntimePointer(await response.json()), fetcher)
}

let pageClient: Promise<RuntimeDataClient> | null = null

export function getRuntimeDataClient(): Promise<RuntimeDataClient> {
  pageClient ??= resolveRuntimeDataClient()
  return pageClient
}
