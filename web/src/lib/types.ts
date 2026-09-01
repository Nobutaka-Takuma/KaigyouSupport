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
  /** コスト軸。重みを持つプロファイルのときだけ入る。 */
  cost?: number | null;
  overall: number | null;
  unavailable_components: string[];
  /** これが空でないとき、総合スコアは算出されない（欠測を無罰にしないため）。 */
  missing_required_components?: string[];
  breakdown: Record<string, ComponentBreakdown>;
  normalization_scope?: string;
}

export interface RadiusMetrics {
  population: number | null;
  children: number | null;
  working_age: number | null;
  elderly: number | null;
  households: number | null;
  /** 従業者数（経済センサス）。昼間人口そのものではなく、その最大の構成要素。 */
  workers: number | null;
  establishments: number | null;
  dental_clinics: number | null;
  population_per_clinic: number | null;
  workers_per_clinic: number | null;
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
  /** どの形の商圏で測った数値か。円と徒歩圏では3倍違うこともあるため必須の文脈。 */
  catchment_kind: "circle" | "walk" | null;
  /** どの都道府県の正規化で出したスコアか。県をまたぐ比較はできない。 */
  prefecture_code?: string;
  prefecture_name?: string;
  /** 参考表示のみ。取り込んでいないときは null。 */
  land_price?: LandPriceSummary | null;
  population_outlook?: PopulationOutlook | null;
  /** 成長スコアが実際に見ている値（将来推計）。population_growth は過去の実績で別物。 */
  population_change_projected?: number | null;
  population_change_from_year?: number | null;
  population_change_to_year?: number | null;
  /** コスト軸の入力。商圏内の地価公示の中央値。 */
  land_price_yen_per_sqm?: number | null;
  land_price_points?: number | null;
  /** "commercial"（商業地のみ）か "all"（全用途区分）。 */
  land_price_basis?: string | null;
  /** 全プロファイルの総合スコア。地価を勘案すると順位が変わることを見せるため。 */
  scores_by_profile?: ProfileScore[];
  /** 標榜科目別の競合と診療時間。診療科目データが無い環境では undefined。 */
  specialties?: SpecialtyBreakdown | null;
  catchment_area_km2: number | null;
  /** 実際に使われた商圏ポリゴン。地図はこれを描く（自前で円を描かない）。 */
  catchment: { geometry: GeoJSON.Geometry; kind: "circle" | "walk" } | null;
  workers: number | null;
  establishments: number | null;
  dental_clinics: number | null;
  population_per_clinic: number | null;
  workers_per_clinic: number | null;
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
  /** 地価考慮プロファイルのときだけ入る。 */
  cost_score?: number | null;
  land_price_yen_per_sqm?: number | null;
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
  prefecture_code?: string;
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

/** 地図に出せる都市計画の層。件数は API が数えたもの（0件の層は返らない）。 */
export interface CityPlanningKind {
  kind: string;
  label: string;
  features: number;
}

export interface CityPlanningKinds {
  available: boolean;
  kinds: CityPlanningKind[];
}

/**
 * 都市計画の面 1 つ分。吹き出しに出す文は**サーバが組み立てたもの**を使う。
 * 画面で文言を作ると、レポートと違うことを言い出す。
 */
export interface CityPlanningProperties {
  zone_kind: string;
  zone_kind_label: string | null;
  zone_type: string | null;
  zone_name: string | null;
  far: number | null;
  bcr: number | null;
  municipality_name: string | null;
  decided_on: string | null;
  description: string | null;
  /** その業態の施設を建てられるか。規則が無い区分では null（空欄）。 */
  buildable: boolean | null;
  buildable_note: string | null;
  facility_label: string | null;
}

export interface CandidatePoint {
  id: string;
  lat: number;
  lng: number;
  label: string;
}

export interface ClinicDetail {
  id: number;
  facility_id: string;
  name: string;
  address: string | null;
  facility_category: string;
  clinic_types: string[];
  opening_date: string | null;
  founder_type: string | null;
  attributes: Record<string, string>;
  source_date: string | null;
  source_name: string;
}

/** 読み込み済みの都道府県。何が分析できるかはDBの中身で決まる。 */
export interface Prefecture {
  code: string;
  name: string;
  mesh_count: number;
  population: number;
  /** [minLng, minLat, maxLng, maxLat] */
  bbox: [number, number, number, number];
  /** 人口重心。東京都の外接矩形の中心は小笠原の沖合になるため。 */
  center: [number, number];
}

export interface PrefectureList {
  prefectures: Prefecture[];
  default: string;
  note: string;
}

/** 地価公示（国土数値情報 L01）。参考情報であり、スコアには使っていない。 */
export interface LandPriceByUse {
  use_category: string | null;
  points: number;
  median_yen_per_sqm: number | null;
  min_yen_per_sqm: number | null;
  max_yen_per_sqm: number | null;
  mean_change_pct: number | null;
  survey_year: number | null;
}

export interface LandPricePoint {
  address: string | null;
  municipality_name: string | null;
  use_category: string | null;
  price_yen_per_sqm: number;
  change_rate_pct: number | null;
  current_use: string | null;
  zoning: string | null;
  survey_year: number | null;
  distance_m: number;
  lat: number;
  lng: number;
}

export interface LandPriceSummary {
  radius_m: number;
  by_use: LandPriceByUse[];
  nearest: LandPricePoint[];
  note: string;
}


/** 1プロファイルぶんの結果。地価あり／なしの比較に使う。 */
export interface ProfileScore {
  profile: string;
  label: string;
  overall: number | null;
  cost: number | null;
  uses_cost: boolean;
  /** 競合をどの標榜科目で数えたか。null なら歯科医院すべて。 */
  competition_specialty?: string | null;
  competition_specialty_label?: string | null;
}

/** 標榜科目1件分の内訳。 */
export interface SpecialtyCount {
  key: string;
  label: string;
  count: number;
  /**
   * 自由記載欄からしか取れない科目（インプラント・審美・訪問診療など）。
   * 記載した医院しか数えられないので、件数は実施医院数の下限にすぎない。
   * この印が付いた行は「競合が少ない」の根拠に使ってはいけない。
   */
  declared_only: boolean;
}

/** 商圏内の医院を標榜科目と診療時間で分けたもの。 */
export interface SpecialtyBreakdown {
  total_clinics: number;
  /** 診療科目が分かっている医院の数。科目別件数の分母はこれ。 */
  with_data: number;
  coverage: number | null;
  breakdown: SpecialtyCount[];
  hours: {
    declared: number | null;
    counts: { key: string; label: string; count: number | null }[];
    weekly_hours_median: number | null;
  };
  note: string;
}

export interface SpecialtyOption {
  key: string;
  label: string;
  clinics: number;
  share: number | null;
  declared_only: boolean;
  /** 歯科の科目か。併設の内科などは false で、絞り込みには出さない。 */
  dental: boolean;
}

export interface SpecialtyList {
  available: boolean;
  clinics?: number;
  clinics_with_data?: number;
  coverage?: number | null;
  specialties: SpecialtyOption[];
  note?: string;
}

// --------------------------------------------------- 商圏インテリジェンス
export interface AnalysisStep {
  step_number: number;
  step_name: string;
  status: "pending" | "running" | "completed" | "failed" | "skipped";
  error_message?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  web_searches?: number | null;
  /** やり直した回数。0 なら一発で通っています。 */
  attempts?: number | null;
  cache_read_tokens?: number | null;
  cache_write_tokens?: number | null;
  model?: string | null;
  prompt_version?: string | null;
}

export interface AnalysisCreated {
  job_id: string;
  status: string;
  steps: AnalysisStep[];
  worker_required: boolean;
  note: string;
}

export interface AnalysisStatus {
  job: {
    id: string;
    status: string;
    location_name?: string | null;
    latitude: number;
    longitude: number;
    radius_m: number;
    error_message?: string | null;
    created_at?: string | null;
    started_at?: string | null;
    completed_at?: string | null;
  };
  steps: AnalysisStep[];
  source_count: number;
  report_available: boolean;
  trace_ok: boolean | null;
  usage: {
    input_tokens: number;
    cache_read_tokens: number;
    cache_write_tokens: number;
    output_tokens: number;
    web_searches: number;
    /** 概算。単価の分からないモデルが混じると null。 */
    estimated_cost_usd: number | null;
  };
  /** ホスティング環境では null。APIサーバはLLMを呼ばないので判定できない。 */
  llm_configured: boolean | null;
  status_note?: string | null;
}

/** 根拠つきの1文。§25 の追跡はこの id を辿る。 */
export interface Evidenced {
  statement: string;
  evidence: string[];
}

export interface ReportBlock {
  tag: "FACT" | "BENCHMARK" | "PATTERN" | "WHY" | "INSIGHT" | "IMPLICATION" | "ACTION";
  text: string;
  evidence?: string[];
}

export interface ReportSection {
  number: number;
  title: string;
  blocks: ReportBlock[];
}

export interface ReportJson {
  executive_summary: string;
  decision: {
    primary_patients: Evidenced;
    secondary_patients: Evidenced;
    avoid_competing_on: Evidenced;
    acquisition_area: Evidenced;
    reason_to_visit: Evidenced;
    clinic_model: Evidenced;
    advantages: Evidenced[];
    risks: Evidenced[];
    confidence: "high" | "medium" | "low";
  };
  sections: ReportSection[];
  actions: Evidenced[];
}

/** 最終段（STEP4）：顧客に渡す文書。散文で、タグは無い。 */
export interface ClientReportJson {
  title: string;
  summary: string;
  verdict: {
    label: string;
    statement: string;
    basis: string[];
    counterpoint: string;
  };
  why_here: string;
  support_needed?: { item: string; why: string; category: string;
                     evidence?: string[] }[];
  /** 古い形で保存されたレポートには無いことがあります。読む側で守ります。 */
  further_research?: { topic: string; why: string; how: string }[];
  sections?: { heading: string; body: string; takeaway?: string | null;
               evidence?: string[] }[];
  judgement_note: string;
}

export interface AnalysisSource {
  pattern_id: string | null;
  url: string;
  title: string | null;
  source_type: string | null;
  retrieved_at?: string | null;
}

export interface AnalysisReport {
  /** 最終段の出力。顧客提出用まで走ったジョブは ClientReportJson。 */
  report_json: ClientReportJson | ReportJson;
  report_markdown: string | null;
  trace_ok: boolean | null;
  trace_problems: { where: string; problem: string }[] | null;
  created_at: string;
  sources: AnalysisSource[];
  disclaimer: string;
}

export interface QuotaView {
  monthly_quota: number;
  used: number;
  remaining: number;
  period_start: string;
}

export interface AnalysisListItem {
  id: string;
  location_name: string | null;
  latitude: number;
  longitude: number;
  radius_m: number;
  profile: string | null;
  status: string;
  created_at: string;
  report_at: string | null;
  trace_ok: boolean | null;
  title: string | null;
  verdict: string | null;
}

export interface AnalysisList {
  items: AnalysisListItem[];
  quota: QuotaView | null;
}

export interface AdminAccount {
  user_id: string;
  email: string | null;
  display_name: string | null;
  organisation: string | null;
  monthly_quota: number;
  billing_day: number;
  status: string;
  is_admin: boolean;
  note: string | null;
  period_start: string;
  used_this_period: number;
  remaining: number;
  api_cost_this_period_usd: number | null;
}

export interface AdminUsage {
  accounts: AdminAccount[];
  reports_this_period: number;
  api_cost_this_period_usd: number | null;
}

export interface Me {
  signed_in: boolean;
  accounts_enabled: boolean;
  email?: string | null;
  is_admin?: boolean;
  quota?: QuotaView | null;
}


/** 将来推計人口。取れていないときは available: false で理由が入る。 */
export interface PopulationOutlook {
  available: boolean;
  reason?: string;
  note?: string;
  base_year?: number;
  estimate_label?: string | null;
  years?: OutlookYear[];
  definition?: string;
}

export interface OutlookYear {
  year: number;
  population: number | null;
  age_0_14: number | null;
  age_15_64: number | null;
  age_65_plus: number | null;
  age_75_plus: number | null;
  elderly_share: number | null;
  late_elderly_share: number | null;
  index_vs_base: number | null;
  mesh_count: number;
}
