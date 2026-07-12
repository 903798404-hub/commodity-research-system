import { parse } from 'yaml'
import source from '../../configs/usda_market_years.yaml?raw'
import type { MarketYearRulesConfig } from '../utils/marketYear'

export const marketYearRules = parse(source) as MarketYearRulesConfig
