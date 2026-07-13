import type { Category, Commodity, Country, Selection } from '../types/dashboard'
import { CategoryTabs } from './CategoryTabs'

type SelectorPanelProps = {
  categories: Category[]
  selection: Selection
  commodityOptions: Commodity[]
  countries: Country[]
  onCategory: (category: Category) => void
  onCommodity: (commodityDescription: string) => void
  onCountry: (countryName: string) => void
}

export function SelectorPanel({ categories, selection, commodityOptions, countries, onCategory, onCommodity, onCountry }: SelectorPanelProps) {
  return <section className="card controls-card" aria-label="数据选择器">
    <CategoryTabs categories={categories} selected={selection.category} onSelect={onCategory} />
    <div className="selectors">
      <label>Commodity<select value={selection.commodityDescription} onChange={(event) => onCommodity(event.target.value)}>{commodityOptions.map((commodity) => <option key={commodity.commodityCode} value={commodity.commodityDescription}>{commodity.displayName}</option>)}</select></label>
      <label>Country<select value={selection.countryName} onChange={(event) => onCountry(event.target.value)}>{countries.map((country) => <option key={country.countryCode} value={country.countryName}>{country.displayName ?? country.countryName}</option>)}</select></label>
    </div>
  </section>
}
