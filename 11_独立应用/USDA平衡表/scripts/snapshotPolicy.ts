import { existsSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

type MatrixData = { years: number[]; rows: Array<{ name: string; values: Array<number | null> }> }

const roundOneDecimal = (value: number) => Math.round((value + Number.EPSILON) * 10) / 10
const finite = (value: number | null | undefined): value is number => typeof value === 'number' && Number.isFinite(value)

export function synchronizeCurrentSnapshotRatioRows(snapshotRoot: string): void {
  const matrixDirectory = join(snapshotRoot, 'matrix')
  if (!existsSync(matrixDirectory)) return
  for (const file of readdirSync(matrixDirectory).filter((name) => name.toLowerCase().endsWith('.json'))) {
    const filePath = join(matrixDirectory, file)
    const matrix = JSON.parse(readFileSync(filePath, 'utf8')) as MatrixData
    if (!Array.isArray(matrix.rows) || !Array.isArray(matrix.years)) continue
    const ratio = matrix.rows.find((row) => row.name === '库存/总使用比')
    const ending = matrix.rows.find((row) => row.name === '期末库存')
    const consumption = matrix.rows.find((row) => row.name === '消费量')
    const exports = matrix.rows.find((row) => row.name === '出口量')
    const values = matrix.years.map((_, index) => {
      const totalUse = finite(consumption?.values[index]) && finite(exports?.values[index]) ? consumption.values[index]! + exports.values[index]! : null
      if (finite(ending?.values[index]) && finite(totalUse) && totalUse > 0) return roundOneDecimal((ending.values[index]! / totalUse) * 100)
      return finite(ratio?.values[index]) ? ratio.values[index]! : null
    })
    matrix.rows = matrix.rows.filter((row) => !['期末库存销售比', '库存/国内消费比', '库存/总使用比'].includes(row.name))
    if (values.some(finite)) matrix.rows.push({ name: '库存/总使用比', values })
    writeFileSync(filePath, `${JSON.stringify(matrix, null, 2)}\n`, 'utf8')
  }
}

export function resolveComparisonSnapshotRoot(
  internalSnapshotRoot: string,
  publicSnapshotRoot: string,
  reportMonth: string,
): string | null {
  const internalMonthRoot = join(internalSnapshotRoot, reportMonth)
  if (existsSync(join(internalMonthRoot, 'index.json'))) return internalMonthRoot
  const publicMonthRoot = join(publicSnapshotRoot, reportMonth)
  if (existsSync(join(publicMonthRoot, 'index.json'))) return publicMonthRoot
  return null
}
