export function formatMarketYear(year: number): string {
  return `${String(year).slice(-2)}/${String(year + 1).slice(-2)}`
}
