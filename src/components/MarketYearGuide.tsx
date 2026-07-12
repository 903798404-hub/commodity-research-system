import { marketYearRules } from '../config/marketYearRules'
import { formatMarketYearDescription, formatMarketYearRange, resolveMarketYearPeriod } from '../utils/marketYear'

type MarketYearGuideProps = { country: string; commodity: string; marketYear?: number }

export function MarketYearGuide({ country, commodity, marketYear }: MarketYearGuideProps) {
  const current = marketYear === undefined ? null : resolveMarketYearPeriod(country, commodity, marketYear, marketYearRules)
  const rows = marketYearRules.reference_rows.flatMap((item) => {
    const rule = marketYearRules.market_year_rules[item.country]?.[item.rule_key]
    if (!rule) return []
    return [{ country: marketYearRules.country_display_names[item.country] ?? item.country, commodityScope: item.commodity_scope, range: formatMarketYearRange(rule) }]
  })

  return <section className="card market-year-guide" aria-label="市场年度口径说明">
    <details>
      <summary>市场年度口径说明</summary>
      <div className="market-year-guide-content">
        <p className="market-year-current">{current ? `当前口径：${formatMarketYearDescription(current)}` : '当前国家或品种暂未配置市场年度说明，请以USDA原始口径为准。'}</p>
        <div className="market-year-table-wrap"><table className="market-year-table"><thead><tr><th>国家或地区</th><th>品种范围</th><th>市场年度</th></tr></thead><tbody>{rows.map((row) => <tr key={`${row.country}-${row.commodityScope}`}><td>{row.country}</td><td>{row.commodityScope}</td><td>{row.range}</td></tr>)}</tbody></table></div>
        <p className="market-year-note">市场年度并不等同于自然年度，不同国家和品种采用不同的起止月份，分析和比较时应以对应的市场年度口径为准。</p>
      </div>
    </details>
  </section>
}
