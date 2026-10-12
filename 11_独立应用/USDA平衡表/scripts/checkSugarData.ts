import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { buildSugarView, SUGAR_DATA_PATH, validateSugarInput } from '../src/utils/sugarBalance'

try {
  const path = resolve(process.argv[2] ?? `public/data/${SUGAR_DATA_PATH}`)
  const input = validateSugarInput(JSON.parse(readFileSync(path, 'utf8')))
  const view = buildSugarView(input)
  console.log(JSON.stringify({ status: 'PASS', path, years: view.years, records: input.records.length, comparisons: view.comparisons.length, balances: view.rows.find(r => r.name === '平衡差')!.values, workbookSha256: input.inputEvidence.workbookSha256, fasPdfSha256: input.inputEvidence.fasPdfSha256 }))
} catch (reason) {
  console.error(reason instanceof Error ? reason.message : String(reason))
  process.exitCode = 1
}
