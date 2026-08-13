import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { resolveComparisonSnapshotRoot, synchronizeCurrentSnapshotRatioRows } from './snapshotPolicy'

function writeMatrix(root: string, month: string, ratio: number): string {
  const directory = join(root, month, 'matrix'); mkdirSync(directory, { recursive: true })
  const file = join(directory, 'matrix.json')
  writeFileSync(file, JSON.stringify({ years: [2026], rows: [
    { name: '期末库存', values: [10] }, { name: '消费量', values: [80] },
    { name: '出口量', values: [20] }, { name: '库存/总使用比', values: [ratio] },
  ] }))
  return file
}

test('processing a new snapshot leaves previous and previous-1 bytes immutable', () => {
  const root = mkdtempSync(join(tmpdir(), 'usda-snapshot-'))
  const june = writeMatrix(root, '2026-06', 77); const july = writeMatrix(root, '2026-07', 88); const august = writeMatrix(root, '2026-08', 99)
  const juneBefore = readFileSync(june); const julyBefore = readFileSync(july)
  synchronizeCurrentSnapshotRatioRows(join(root, '2026-08'))
  assert.deepEqual(readFileSync(june), juneBefore); assert.deepEqual(readFileSync(july), julyBefore)
  const current = JSON.parse(readFileSync(august, 'utf8')); assert.equal(current.rows.find((row: { name: string }) => row.name === '库存/总使用比').values[0], 10)
})

test('repeated current-month processing never rewrites historical snapshots', () => {
  const root = mkdtempSync(join(tmpdir(), 'usda-snapshot-'))
  const july = writeMatrix(root, '2026-07', 88); writeMatrix(root, '2026-08', 99); const before = readFileSync(july)
  synchronizeCurrentSnapshotRatioRows(join(root, '2026-08')); synchronizeCurrentSnapshotRatioRows(join(root, '2026-08'))
  assert.deepEqual(readFileSync(july), before)
})

test('new internal snapshot is directly selected as the next comparison input', () => {
  const root = mkdtempSync(join(tmpdir(), 'usda-snapshot-'))
  const internalRoot = join(root, 'internal')
  const publicRoot = join(root, 'public')
  for (const base of [internalRoot, publicRoot]) {
    const monthRoot = join(base, '2026-08')
    mkdirSync(monthRoot, { recursive: true })
    writeFileSync(join(monthRoot, 'index.json'), '{}')
  }
  assert.equal(resolveComparisonSnapshotRoot(internalRoot, publicRoot, '2026-08'), join(internalRoot, '2026-08'))
})

test('comparison can use a frozen public snapshot without a manual internal copy', () => {
  const root = mkdtempSync(join(tmpdir(), 'usda-snapshot-'))
  const internalRoot = join(root, 'internal')
  const publicRoot = join(root, 'public')
  const publicMonthRoot = join(publicRoot, '2026-07')
  mkdirSync(publicMonthRoot, { recursive: true })
  writeFileSync(join(publicMonthRoot, 'index.json'), '{}')
  assert.equal(resolveComparisonSnapshotRoot(internalRoot, publicRoot, '2026-07'), publicMonthRoot)
  assert.equal(resolveComparisonSnapshotRoot(internalRoot, publicRoot, '2026-06'), null)
})
