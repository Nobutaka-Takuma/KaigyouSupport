export interface SourceProvenance {
  dataset: string;
  dataset_label: string;
  source_id: string;
  name: string;
  publisher: string;
  dataset_kind: "official" | "sample";
  license: string | null;
  homepage_url: string | null;
  terms_url: string | null;
  row_count: number;
  source_date: string | null;
  last_updated: string | null;
}

export interface Provenance {
  sources: SourceProvenance[];
  contains_sample_data: boolean;
  datasets_unavailable: { dataset: string; dataset_label: string }[];
}

export interface ComponentBreakdown {
  value: number | null;
  parts: Record<string, number | null>;
  missing: string[];
  note: string | null;
}

export interface Scores {
  profile: string;
  profile_label: string;
  is_provisional: boolean;
  demand: number | null;
  competition: number | null;
  growth: number | null;
  accessibility: number | null;
  overall: number | null;
  unavailable_components: string[];
  breakdown: Record<string, ComponentBreakdown>;
  normalization_scope?: string;
}

export interface RadiusMetrics {
  population: number | null;
  children: number | null;
  working_age: number | null;
  elderly: number | null;
  households: number | null;
  dental_clinics: number | null;
  population_per_clinic: number | null;
}

export interface CandidateAnalysis {
  location: { lat: number; lng: number };
  radius_m: number;
  label?: string;
  population: number | null;
  children: number | null;
  working_age: number | null;
  elderly: number | null;
  households: number | null;
  population_growth: number | null;
  dental_clinics: number | null;
  population_per_clinic: number | null;
  nearest_clinic: { name: string | null; distance_m: number | null };
  nearest_station: {
    name: string | null;
    distance_m: number | null;
    daily_passengers: number | null;
  };
  mesh_count: number | null;
  mesh_size_m?: number | null;
  scores: Scores;
  warnings: string[];
  by_radius?: Record<string, RadiusMetrics>;
  model?: ScoringModelInfo;
  provenance?: Provenance;
  disclaimer?: string;
  score_disclaimer?: string;
}

export interface ScoringModelInfo {
  profile: string;
  label: string;
  description: string | null;
  overall_weights: Record<string, number>;
  demand_weights: Record<string, number>;
  competition: Record<string, unknown>;
  growth: Record<string, unknown>;
  accessibility: Record<string, unknown>;
  normalization: { method: string; clamp: number[] };
  trade_area_radii_m: number[];
  is_provisional: boolean;
}

export interface Meta {
  active_profile: string;
  profiles: ScoringModelInfo[];
  trade_area_radii_m: number[];
  mesh_scoring_radius_m: number;
  disclaimer: string;
  score_disclaimer: string;
  out_of_scope: string[];
  caveats: string[];
}

export interface RankingItem {
  rank: number;
  mesh_code: string;
  area_label: string | null;
  lat: number;
  lng: number;
  overall_score: number | null;
  demand_score: number | null;
  competition_score: number | null;
  growth_score: number | null;
  accessibility_score: number | null;
  population: number | null;
  age_0_14: number | null;
  age_65_plus: number | null;
  households: number | null;
  population_growth: number | null;
  facility_count: number | null;
  population_per_facility: number | null;
  nearest_station: string | null;
  station_distance_m: number | null;
  daily_passengers: number | null;
}

export interface RankingResponse {
  items: RankingItem[];
  total: number;
  mesh_size_m: number | null;
  warnings?: string[];
  limit: number;
  offset: number;
  radius_m: number;
  model: ScoringModelInfo;
  message: string | null;
  provenance: Provenance;
  disclaimer: string;
  score_disclaimer: string;
}

export interface CompareResponse {
  radius_m: number;
  warnings?: string[];
  locations: CandidateAnalysis[];
  model: ScoringModelInfo;
  provenance: Provenance;
  disclaimer: string;
  score_disclaimer: string;
}

export interface SourceStatus {
  source_id: string;
  name: string;
  publisher: string | null;
  dataset_kind: "official" | "sample";
  license: string | null;
  homepage_url: string | null;
  terms_url: string | null;
  configured_url: string | null;
  target_table: string | null;
  state: "loaded" | "failed" | "never_attempted" | "empty" | "incomplete";
  reason: string | null;
  row_counts: Record<string, number>;
  row_total: number;
  steps: Record<
    string,
    {
      status: string;
      target_url: string | null;
      http_status: number | null;
      record_count: number | null;
      error_type: string | null;
      error_message: string | null;
      finished_at: string | null;
    }
  >;
  last_attempt_at: string | null;
}

export interface DataStatus {
  sources: SourceStatus[];
  contains_sample_data: boolean;
  mixed_datasets: { dataset: string; dataset_label: string; message: string }[];
  official_sources_loaded: number;
  official_sources_configured: number;
  sample_sources_loaded: number;
  table_row_totals: Record<string, number>;
  table_row_totals_official: Record<string, number>;
  disclaimer: string;
  sample_data_warning?: string;
  no_official_data_warning?: string;
}

export interface GeoJSONResponse {
  type: "FeatureCollection";
  features: GeoJSON.Feature[];
  provenance: Provenance;
  truncated: boolean;
}

export interface CandidatePoint {
  id: string;
  lat: number;
  lng: number;
  label: string;
}
