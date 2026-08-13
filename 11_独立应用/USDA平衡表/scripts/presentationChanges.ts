import { existsSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { buildMonthlyRevisionLookup, buildPalmG2MonthlyRevisionLookup, buildSoybeanG3MonthlyRevisionLookup } from '../src/utils/monthlyRevision'
import { normalizeSeries, safeDifference } from '../src/utils/number'
import type { Catalog, MatrixData, MatrixReference } from '../src/types/dashboard'
import { sourceCompatibility, type SourceBasis } from '../src/utils/sourceCompatibility'

type PresentationChangeRow = { name: string; latestYoY: number | null; monthlyRevision: number | null }
type PresentationChangeMatrix = { commodityCode: string; commodity: string; countryCode: string; country: string; latestMarketYear: number | null; previousMarketYear: number | null; rows: PresentationChangeRow[] }

function readMatrix(path: string): MatrixData | null {
  if (!existsSync(path)) return null
  try { return JSON.parse(readFileSync(path, 'utf8')) as MatrixData } catch { return null }
}

function snapshotMonths(snapshotDirectory: string): string[] {
  if (!existsSync(snapshotDirectory)) return []
  return readdirSync(snapshotDirectory).filter((name) => /^\d{4}-\d{2}$/.test(name)).sort((a, b) => a.localeCompare(b))
}

function matrixReference(catalog: Catalog, commodity: string, country: string): MatrixReference | undefined {
  return catalog.matrices.find((item) => item.commodity === commodity && item.country === country)
}

function sourceMatrices(catalog: Catalog, commodity: string, countries: string[], baseDirectory: string): Array<MatrixData | null> {
  return countries.map((country) => {
    const reference = matrixReference(catalog, commodity, country)
    return reference ? readMatrix(join(baseDirectory, reference.file)) : null
  })
}

export function writePresentationChanges(dataDirectory: string) {
  const matrixDirectory = join(dataDirectory, 'matrix')
  const snapshotDirectory = join(dataDirectory, 'snapshots', 'usda_psd')
  const [snapshotPrevious, snapshotCurrent] = snapshotMonths(snapshotDirectory).slice(-2)
  const matrices: PresentationChangeMatrix[] = []
  const provenancePath = join(process.cwd(), 'configs', 'usda_source_provenance.json')
  const provenance = existsSync(provenancePath) ? JSON.parse(readFileSync(provenancePath, 'utf8')) as { legacyGlobalSourceBasis?: Record<string, SourceBasis> } : {}
  const catalog = readMatrix(join(dataDirectory, 'index.json')) as unknown as Catalog | null
  if (!catalog || !Array.isArray(catalog.matrices)) throw new Error('Unable to read index.json while preparing presentation changes.')
  const currentMalaysia = readMatrix(join(matrixDirectory, '4243000_MY.json'))
  const currentIndonesia = readMatrix(join(matrixDirectory, '4243000_ID.json'))
  const previousMalaysia = snapshotPrevious ? readMatrix(join(snapshotDirectory, snapshotPrevious, 'matrix', '4243000_MY.json')) : null
  const previousIndonesia = snapshotPrevious ? readMatrix(join(snapshotDirectory, snapshotPrevious, 'matrix', '4243000_ID.json')) : null

  for (const file of readdirSync(matrixDirectory).filter((name) => name.toLowerCase().endsWith('.json'))) {
    const current = readMatrix(join(matrixDirectory, file))
    if (!current) continue
    const latestMarketYear = current.years.at(-1) ?? null
    const previousMarketYear = current.years.at(-2) ?? null
    const previous = snapshotPrevious ? readMatrix(join(snapshotDirectory, snapshotPrevious, 'matrix', file)) : null
    const compatible = previous ? sourceCompatibility(current, previous, snapshotCurrent ? provenance.legacyGlobalSourceBasis?.[snapshotCurrent] ?? null : null, snapshotPrevious ? provenance.legacyGlobalSourceBasis?.[snapshotPrevious] ?? null : null).comparable : false
    const monthlyLookup = !compatible ? {}
      : current.commodity === 'Oil, Palm' && current.countryCode === 'G2'
      ? buildPalmG2MonthlyRevisionLookup(current, currentMalaysia, previousMalaysia, currentIndonesia, previousIndonesia)
      : current.countryCode === 'G3' && ['Oilseed, Soybean', 'Oil, Soybean'].includes(current.commodity)
        ? buildSoybeanG3MonthlyRevisionLookup(
          current,
          sourceMatrices(catalog, current.commodity, ['United States', 'Brazil', 'Argentina'], dataDirectory),
          snapshotPrevious ? sourceMatrices(catalog, current.commodity, ['United States', 'Brazil', 'Argentina'], join(snapshotDirectory, snapshotPrevious)) : [null, null, null],
        )
        : buildMonthlyRevisionLookup(current, previous)
    const rows = current.rows.map((row) => {
      const values = normalizeSeries(row.values, current.years.length)
      const latestValue = values.at(-1)
      const previousValue = values.at(-2)
      return {
        name: row.name,
        latestYoY: safeDifference(latestValue, previousValue),
        monthlyRevision: latestMarketYear === null ? null : monthlyLookup[row.name]?.[latestMarketYear]?.revision ?? null,
      }
    })
    matrices.push({ commodityCode: current.commodityCode, commodity: current.commodity, countryCode: current.countryCode, country: current.country, latestMarketYear, previousMarketYear, rows })
  }

  const payload = { snapshotCurrent: snapshotCurrent ?? null, snapshotPrevious: snapshotPrevious ?? null, matrices }
  writeFileSync(join(dataDirectory, 'presentation_changes.json'), `${JSON.stringify(payload, null, 2)}\n`, 'utf8')
}
