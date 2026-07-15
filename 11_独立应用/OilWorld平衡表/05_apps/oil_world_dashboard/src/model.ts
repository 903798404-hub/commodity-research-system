export type MappingStatus = "direct" | "derived" | "missing" | "not_applicable" | "conflict";
export type ForecastStatus = "historical" | "explicit_forecast" | "implicit_forecast";

export interface MetricData {
  metric: string;
  mapping_status: MappingStatus;
  market_year_basis: string;
  periods: string[];
  original_periods: Record<string, string>;
  forecast_status: Record<string, ForecastStatus>;
  original_metric: string;
  values: Record<string, number | null>;
  annual_change: null | {
    current_period: string;
    previous_period: string;
    value: number;
    unit: string;
  };
  quarter_revision: null;
  quarter_revision_note: string;
  unit: string;
  original_unit: string;
  source_report_id: string[];
  source_report_title: string[];
  source_sheet: string[];
  source_cells: Record<string, string[]>;
  source_cell_or_range: string;
  is_derived: boolean;
  derivation_method: string;
  derivation_components: string;
  quality_note: string;
  has_footnote: boolean;
  has_star: boolean;
}

export interface CombinationData {
  schema_version: number;
  release: string;
  system: string;
  product: string;
  region: string;
  market_year_basis: string[];
  periods: string[];
  forecast_status: Record<string, ForecastStatus>;
  metrics: MetricData[];
  quality_note: string;
}

export interface RegionFile {
  system: string;
  product: string;
  region: string;
  path: string;
}

export interface ProductIndex {
  id: string;
  label: string;
  regions: string[];
}

export interface SystemIndex {
  id: string;
  label: string;
  products: ProductIndex[];
}

export interface ReleaseIndex {
  schema_version: number;
  release: string;
  release_label: string;
  source: string;
  systems: SystemIndex[];
  metric_order: string[];
  files: RegionFile[];
  combination_count: number;
  mapping_record_count: number;
  numeric_observation_count: number;
  status_counts: Record<MappingStatus, number>;
  quarter_revision_available: boolean;
}

export interface ReleaseList {
  releases: Array<{ release: string; label: string; available: boolean }>;
}
