import { readFileSync } from 'node:fs'
import { join } from 'node:path'

export type ReportVersion = {
  currentReportMonth: string
  previousReportMonth: string
}

const VERSION_FILE = join(process.cwd(), 'configs', 'usda_report_version.json')
const MONTH_PATTERN = /^\d{4}-(0[1-9]|1[0-2])$/

export function readReportVersion(): ReportVersion {
  const parsed = JSON.parse(readFileSync(VERSION_FILE, 'utf8')) as Partial<ReportVersion>
  if (!parsed.currentReportMonth || !parsed.previousReportMonth || !MONTH_PATTERN.test(parsed.currentReportMonth) || !MONTH_PATTERN.test(parsed.previousReportMonth)) {
    throw new Error('configs/usda_report_version.json 必须包含 YYYY-MM 格式的 currentReportMonth 和 previousReportMonth。')
  }
  return { currentReportMonth: parsed.currentReportMonth, previousReportMonth: parsed.previousReportMonth }
}
