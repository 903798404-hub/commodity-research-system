import { readFileSync } from 'node:fs'
import { join } from 'node:path'

export type UsdaApiConfig = {
  reportMonth: string
  startYear: number
  endYear: number
  useApi: boolean
}

const CONFIG_FILE = join(process.cwd(), 'configs', 'usda_api_config.json')
const MONTH_PATTERN = /^\d{4}-(0[1-9]|1[0-2])$/

export function readUsdaApiConfig(): UsdaApiConfig {
  const parsed = JSON.parse(readFileSync(CONFIG_FILE, 'utf8')) as Partial<UsdaApiConfig>
  if (!parsed.reportMonth || !MONTH_PATTERN.test(parsed.reportMonth) || !Number.isInteger(parsed.startYear) || !Number.isInteger(parsed.endYear) || parsed.startYear > parsed.endYear || typeof parsed.useApi !== 'boolean') {
    throw new Error('configs/usda_api_config.json 的 reportMonth、年份范围或 useApi 配置无效。')
  }
  return { reportMonth: parsed.reportMonth, startYear: parsed.startYear, endYear: parsed.endYear, useApi: parsed.useApi }
}
