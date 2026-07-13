import { parse } from 'yaml'
import source from '../../configs/presentation_layout.yaml?raw'

export type PresentationRegionPair = { region: string; display_name: string }

export type PresentationLayoutConfig = {
  label: string
  title: string
  start_market_year: number
  show_latest_yoy: boolean
  show_monthly_revision: boolean
  market_year_note: string
  seed_commodity: string
  seed_display_name: string
  seed_metrics: string[]
  oil_commodity: string
  oil_display_name: string
  oil_metrics: string[]
  region_pairs: PresentationRegionPair[]
  rows_per_slide: number
}

export type PresentationReportKey = 'rapeseed' | 'soybean'

export type PresentationLayoutsConfig = {
  reports: Record<PresentationReportKey, PresentationLayoutConfig>
}

export const presentationLayouts = parse(source) as PresentationLayoutsConfig
