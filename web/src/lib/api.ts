/**
 * The only place the client talks to the server.
 *
 * The UI has no knowledge of where the underlying data came from or whether it
 * could be obtained at all -- it asks the API and renders what it is told,
 * including the "we could not get this" answers.
 */
import type {
  CandidateAnalysis,
  CompareResponse,
  DataStatus,
  GeoJSONResponse,
  Meta,
  RankingResponse,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE ?? "";

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function get<T>(path: string, params: Record<string, unknown> = {}): Promise<T> {
  const url = new URL(`${BASE}/api${path}`, window.location.origin);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }
  const res = await fetch(url.toString());
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new ApiError(detail, res.status);
  }
  return res.json() as Promise<T>;
}

export const api = {
  meta: () => get<Meta>("/meta"),
  dataStatus: () => get<DataStatus>("/data-status"),

  clinics: (params: { bbox?: string; clinic_type?: string; limit?: number }) =>
    get<GeoJSONResponse>("/clinics", params),

  stations: (params: { bbox?: string; q?: string; limit?: number }) =>
    get<GeoJSONResponse>("/stations", params),

  meshes: (params: {
    bbox?: string;
    profile?: string;
    radius_m?: number;
    mesh_size_m?: number;
    limit?: number;
  }) => get<GeoJSONResponse>("/meshes", params),

  municipalities: (params: { prefecture_code?: string } = {}) =>
    get<GeoJSONResponse>("/municipalities", params),

  candidateAnalysis: (params: {
    lat: number;
    lng: number;
    radius: number;
    profile?: string;
  }) => get<CandidateAnalysis>("/candidate-analysis", params),

  rankings: (params: {
    limit?: number;
    offset?: number;
    profile?: string;
    radius?: number;
    min_population?: number;
    area?: string;
  }) => get<RankingResponse>("/rankings", params),

  compare: (params: {
    points: string;
    labels?: string;
    radius: number;
    profile?: string;
  }) => get<CompareResponse>("/compare", params),
};
