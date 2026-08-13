export type SourceBasis = 'usda_psd_world' | 'legacy_csv_synthetic_global' | 'synthetic_sum'

export type SourceAwareMatrix = {
  country: string
  sourceBasis?: SourceBasis
}

export type SourceCompatibility = {
  comparable: boolean
  reason: 'source_basis_changed' | null
  currentSourceBasis: SourceBasis | null
  previousSourceBasis: SourceBasis | null
}

export function sourceCompatibility(
  current: SourceAwareMatrix,
  previous: SourceAwareMatrix,
  currentLegacyBasis: SourceBasis | null = null,
  previousLegacyBasis: SourceBasis | null = null,
): SourceCompatibility {
  if (current.country !== 'Global' || previous.country !== 'Global') {
    return { comparable: true, reason: null, currentSourceBasis: null, previousSourceBasis: null }
  }
  const currentSourceBasis = current.sourceBasis ?? currentLegacyBasis
  const previousSourceBasis = previous.sourceBasis ?? previousLegacyBasis
  const comparable = currentSourceBasis !== null && currentSourceBasis === previousSourceBasis
  return {
    comparable,
    reason: comparable ? null : 'source_basis_changed',
    currentSourceBasis,
    previousSourceBasis,
  }
}
