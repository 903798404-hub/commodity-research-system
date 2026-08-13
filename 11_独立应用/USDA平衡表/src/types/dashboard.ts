import type { ChartMode } from '../utils/chart'
import type { SourceBasis } from '../utils/sourceCompatibility'

export type Category = 'Oilseeds' | 'Oils' | 'Meals'
export type BalanceRow = { name: string; values: unknown }
export type MatrixData = {
  commodityCode: string
  commodity: string
  category: Category
  countryCode: string
  country: string
  years: number[]
  rows: BalanceRow[]
  sourceBasis?: SourceBasis
}
export type Commodity = { commodityCode: string; commodityDescription: string; category: Category; displayName: string }
export type Country = { countryCode: string; countryName: string; displayName?: string }
export type MatrixReference = { category: Category; commodityCode: string; commodity: string; countryCode: string; country: string; file: string }
export type Catalog = {
  categories: Category[]
  commodities: Commodity[]
  countries: Country[]
  matrices: MatrixReference[]
  defaultSelection: { category: Category; commodityDescription: string; countryName: string }
}
export type Selection = { category: Category; commodityDescription: string; countryName: string }
export type DashboardChartMode = ChartMode
