export const RESEARCH_SCOPE: Record<string, readonly string[]> = {
  'Oilseed, Soybean': ['United States', 'Brazil', 'Argentina', 'China', 'India'],
  'Meal, Soybean': ['United States', 'Brazil', 'Argentina', 'China', 'India'],
  'Oil, Soybean': ['United States', 'Brazil', 'Argentina', 'China', 'India'],
  'Oilseed, Rapeseed': ['Canada', 'Australia', 'European Union', 'Ukraine', 'Russia', 'China'],
  'Meal, Rapeseed': ['Canada', 'Australia', 'European Union', 'Ukraine', 'Russia'],
  'Oil, Rapeseed': ['Canada', 'Australia', 'European Union', 'Ukraine', 'Russia', 'China'],
  'Oil, Palm': ['Malaysia', 'Indonesia', 'Thailand'],
  'Oilseed, Sunflowerseed': ['Russia', 'Ukraine', 'European Union', 'Argentina'],
  'Oil, Sunflowerseed': ['Russia', 'Ukraine', 'European Union', 'Argentina'],
}

export const G3_COUNTRIES = ['United States', 'Brazil', 'Argentina']
export const G3_COMMODITIES = ['Oilseed, Soybean', 'Oil, Soybean']

const LOCAL_SOYBEAN_PATTERN = /soybeans?\s*\(?local\)?|大豆\s*[（(]?本地[）)]?|本地大豆/i

export function isResearchCombination(commodity: string, country: string): boolean {
  if (LOCAL_SOYBEAN_PATTERN.test(commodity)) return false
  return RESEARCH_SCOPE[commodity]?.includes(country) ?? false
}

export function researchScopeReportLines(): string[] {
  return [
    '- 大豆链：Oilseed, Soybean / Meal, Soybean / Oil, Soybean；United States、Brazil、Argentina、China、India。',
    '- 菜籽与菜油：Oilseed, Rapeseed / Oil, Rapeseed；Canada、Australia、European Union、Ukraine、Russia、China。菜粕仍保留 Canada、Australia、European Union、Ukraine、Russia。',
    '- 棕榈油：Oil, Palm；Malaysia、Indonesia、Thailand。',
    '- 葵花籽链：Oilseed, Sunflowerseed / Oil, Sunflowerseed；Russia、Ukraine、European Union、Argentina。',
    '- 已排除 Soybean (Local)、Soybeans Local、大豆（本地）等本地大豆变体。',
  ]
}
