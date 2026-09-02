/**
 * The only place the client talks to the server.
 *
 * The UI has no knowledge of where the underlying data came from or whether it
 * could be obtained at all -- it asks the API and renders what it is told,
 * including the "we could not get this" answers.
 */
import type {
  AdminUsage,
  Me,
  AnalysisCreated,
  AnalysisList,
  AnalysisReport,
  AnalysisStatus,
  CandidateAnalysis,
  CityPlanningKinds,
  CityPlanningResponse,
  ClinicDetail,
  CompareResponse,
  DataStatus,
  GeoJSONResponse,
  Meta,
  MisreadingList,
  PrefectureList,
  RankingResponse,
  SpecialtyList,
} from "./types";

import { auth } from "./auth";

const BASE = import.meta.env.VITE_API_BASE ?? "";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    /** What the server suggests doing about it, when it says. */
    readonly hint?: string,
  ) {
    super(message);
  }
}

async function request<T>(
  method: "GET" | "POST",
  path: string,
  params: Record<string, unknown> = {},
  token?: string,
): Promise<T> {
  const url = new URL(`${BASE}/api${path}`, window.location.origin);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }
  // ログインしていれば、その JWT を必ず添えます。サーバ側がアカウントを引き、
  // 枠と権限を見ます。**API がこの製品の唯一の境界**なので、ここを通らない
  // 経路を作らないでください。
  const headers: Record<string, string> = {};
  const jwt = await auth.token();
  if (jwt) headers["Authorization"] = `Bearer ${jwt}`;
  // アカウントを使わない構成（手元）では共有シークレットで守ります。
  if (token) headers["X-Analysis-Token"] = token;
  const res = await fetch(url.toString(), { method, headers });
  if (!res.ok) {
    let detail = res.statusText;
    let hint: string | undefined;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
      hint = body.hint;
    } catch {
      /* keep statusText */
    }
    throw new ApiError(detail, res.status, hint);
  }
  return res.json() as Promise<T>;
}

const get = <T,>(path: string, params: Record<string, unknown> = {}) =>
  request<T>("GET", path, params);

export const api = {
  meta: () => get<Meta>("/meta"),
  prefectures: () => get<PrefectureList>("/prefectures"),
  dataStatus: () => get<DataStatus>("/data-status"),

  /** 絞り込みに使える標榜科目。読み込み済みのデータが決めるので API に聞く。 */
  specialties: (params: { prefecture_code?: string } = {}) =>
    get<SpecialtyList>("/specialties", params),

  clinics: (params: {
    bbox?: string;
    clinic_type?: string;
    /** 正規化した標榜科目キー（pediatric / orthodontics ...）で絞り込む。 */
    specialty?: string;
    fields?: "points" | "minimal" | "full";
    limit?: number;
  }) => get<GeoJSONResponse>("/clinics", params),

  clinic: (id: number) => get<ClinicDetail>(`/clinics/${id}`),

  stations: (params: { bbox?: string; q?: string; limit?: number }) =>
    get<GeoJSONResponse>("/stations", params),

  meshes: (params: {
    bbox?: string;
    profile?: string;
    radius_m?: number;
    mesh_size_m?: number;
    limit?: number;
  }) => get<GeoJSONResponse>("/meshes", params),

  /** 1地点の全データ。LLMや他のプログラムに渡すための構造化JSON。 */
  dataset: (params: {
    lat: number;
    lng: number;
    radius: number;
    catchment?: "circle" | "walk";
    profile?: string;
    max_clinics?: number;
    geometry?: boolean;
  }) => get<Record<string, unknown>>("/dataset", params),

  landPrices: (params: {
    bbox?: string;
    use_category_code?: string;
    year?: number;
    limit?: number;
  } = {}) => get<GeoJSONResponse>("/land-prices", params),

  misreadings: (params: { category?: string } = {}) =>
    get<MisreadingList>("/misreadings", params),

  cityPlanningKinds: (params: { prefecture_code?: string } = {}) =>
    get<CityPlanningKinds>("/city-planning/kinds", params),

  cityPlanning: (params: {
    kind: string;
    bbox?: string;
    prefecture_code?: string;
    simplify_deg?: number;
    limit?: number;
  }) => get<CityPlanningResponse>("/city-planning", params),

  municipalities: (params: {
    prefecture_code?: string;
    bbox?: string;
    simplify_deg?: number;
  } = {}) => get<GeoJSONResponse>("/municipalities", params),

  candidateAnalysis: (params: {
    lat: number;
    lng: number;
    radius: number;
    profile?: string;
    catchment?: "circle" | "walk";
    prefecture_code?: string;
  }) => get<CandidateAnalysis>("/candidate-analysis", params),

  rankings: (params: {
    limit?: number;
    offset?: number;
    profile?: string;
    radius?: number;
    min_population?: number;
    area?: string;
    prefecture_code?: string;
  }) => get<RankingResponse>("/rankings", params),

  compare: (params: {
    points: string;
    labels?: string;
    radius: number;
    profile?: string;
    prefecture_code?: string;
  }) => get<CompareResponse>("/compare", params),

  /**
   * 商圏インテリジェンス。ここで分析は走りません。
   *
   * 4ステップとWeb検索は1リクエストに収まらないので、APIはJobを作るだけで、
   * 実行はワークステーション側のworkerです。UIはその進捗を見に行きます。
   */
  analysis: {
    create: (
      params: {
        lat: number;
        lng: number;
        radius: number;
        catchment?: "circle" | "walk";
        profile?: string;
        location_name?: string;
      },
      token?: string,
    ) => request<AnalysisCreated>("POST", "/analysis", params, token),

    status: (jobId: string) => get<AnalysisStatus>(`/analysis/${jobId}`),

    report: (jobId: string) => get<AnalysisReport>(`/analysis/${jobId}/report`),

    retryFrom: (jobId: string, step: number, token?: string) =>
      request<{ restarted_from: number }>(
        "POST", `/analysis/${jobId}/steps/${step}/retry`, {}, token),

    /** いまサインインしている利用者。管理リンクの出し分けに使います。 */
    me: () => get<Me>("/me"),

    /** 自分が作ったレポートの一覧。失くしても取り直せるように。 */
    list: (limit = 50) => get<AnalysisList>("/analyses", { limit }),

    /** Markdown をファイルとして落とす。名前はサーバが付けます。 */
    async download(jobId: string) {
      const url = new URL(`${BASE}/api/analysis/${jobId}/report.md`,
        window.location.origin);
      const jwt = await auth.token();
      const res = await fetch(url.toString(), {
        headers: jwt ? { Authorization: `Bearer ${jwt}` } : {},
      });
      if (!res.ok) throw new ApiError("レポートを取得できませんでした。", res.status);
      const blob = await res.blob();
      const name = decodeURIComponent(
        (res.headers.get("Content-Disposition") ?? "").split("''")[1] ?? "report.md");
      const href = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = href;
      link.download = name;
      link.click();
      URL.revokeObjectURL(href);
    },
  },

  /** 運営者用。アカウントの発行と、今期の利用状況。 */
  admin: {
    usage: () => get<AdminUsage>("/admin/usage"),

    async upsert(userId: string, body: Record<string, unknown>) {
      const url = new URL(`${BASE}/api/admin/accounts/${encodeURIComponent(userId)}`,
        window.location.origin);
      const jwt = await auth.token();
      const res = await fetch(url.toString(), {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          ...(jwt ? { Authorization: `Bearer ${jwt}` } : {}),
        },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new ApiError(detail.detail ?? res.statusText, res.status);
      }
      return res.json();
    },
  },
};
