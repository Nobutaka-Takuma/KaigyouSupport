/**
 * The main screen: pick a point on the map, read its trade area.
 *
 * This is the product's centre of gravity -- the ranking is a by-product of the
 * same engine, not the other way round.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import maplibregl, { Map as MapLibreMap } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

import { api, ApiError } from "../lib/api";
import { basemapStyle, hasBasemap } from "../lib/basemap";
import { circlePolygon, EMPTY_FC } from "../lib/geo";
import { MAX_CANDIDATES, useCandidates } from "../lib/candidates";
import { distance, num } from "../lib/format";
import type { CandidateAnalysis, Meta } from "../lib/types";
import { Disclaimer, ProvenanceList } from "../components/DataNotices";
import { AccessTable, PopulationTable, ScorePanel } from "../components/ScorePanel";

const TOKYO_CENTER: [number, number] = [139.7671, 35.6812];

/** Below these zooms a layer covers too much ground to be worth sending. */
const MIN_ZOOM = { meshes: 10, clinics: 11, stations: 10 };

const EMPTY_RESPONSE = {
  type: "FeatureCollection" as const,
  features: [],
  provenance: { sources: [], contains_sample_data: false, datasets_unavailable: [] },
  truncated: false,
};

type MeshMetric = "population" | "overall_score";

interface LayerState {
  municipalities: boolean;
  meshes: boolean;
  clinics: boolean;
  stations: boolean;
}

export function MapPage() {
  const mapRef = useRef<MapLibreMap | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [ready, setReady] = useState(false);

  const [meta, setMeta] = useState<Meta | null>(null);
  const [profile, setProfile] = useState<string>("");
  const [radius, setRadius] = useState(1000);
  const [meshMetric, setMeshMetric] = useState<MeshMetric>("overall_score");
  const [layers, setLayers] = useState<LayerState>({
    municipalities: true,
    meshes: true,
    clinics: true,
    stations: true,
  });

  const [point, setPoint] = useState<{ lat: number; lng: number } | null>(null);
  const [analysis, setAnalysis] = useState<CandidateAnalysis | null>(null);
  const [analysing, setAnalysing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [layerError, setLayerError] = useState<string | null>(null);
  const [zoomedOut, setZoomedOut] = useState(false);
  const viewportRequest = useRef(0);
  // On a phone the controls and the panel cannot both be on screen with the
  // map. The radius chips stay visible; everything else folds away, and the
  // analysis arrives as a sheet over the map rather than below it.
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [stationQuery, setStationQuery] = useState("");
  const [stationHits, setStationHits] = useState<GeoJSON.Feature[]>([]);

  const { points: candidates, add, remove } = useCandidates();
  const [searchParams, setSearchParams] = useSearchParams();

  useEffect(() => {
    api.meta().then((m) => {
      setMeta(m);
      setProfile(m.active_profile);
      if (m.trade_area_radii_m.includes(1000)) setRadius(1000);
      else setRadius(m.trade_area_radii_m[0]);
    });
  }, []);

  // ------------------------------------------------------------------ map init
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: basemapStyle(),
      center: TOKYO_CENTER,
      zoom: 11,
    });
    map.addControl(new maplibregl.NavigationControl(), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }));
    mapRef.current = map;

    map.on("load", () => {
      for (const id of ["municipalities", "meshes", "clinics", "stations", "candidate"]) {
        map.addSource(id, { type: "geojson", data: EMPTY_FC });
      }

      map.addLayer({
        id: "meshes-fill",
        type: "fill",
        source: "meshes",
        paint: {
          "fill-color": "#cccccc",
          "fill-opacity": 0.55,
        },
      });
      map.addLayer({
        id: "municipalities-line",
        type: "line",
        source: "municipalities",
        paint: { "line-color": "#4a5568", "line-width": 1.2, "line-opacity": 0.8 },
      });
      map.addLayer({
        id: "candidate-buffer",
        type: "fill",
        source: "candidate",
        filter: ["==", ["geometry-type"], "Polygon"],
        paint: { "fill-color": "#1d4ed8", "fill-opacity": 0.12 },
      });
      map.addLayer({
        id: "candidate-buffer-line",
        type: "line",
        source: "candidate",
        filter: ["==", ["geometry-type"], "Polygon"],
        paint: { "line-color": "#1d4ed8", "line-width": 2 },
      });
      map.addLayer({
        id: "stations-circle",
        type: "circle",
        source: "stations",
        paint: {
          "circle-radius": [
            "interpolate", ["linear"], ["coalesce", ["get", "daily_passengers"], 0],
            0, 3, 50000, 6, 500000, 12,
          ],
          "circle-color": "#0f766e",
          "circle-opacity": 0.75,
          "circle-stroke-color": "#ffffff",
          "circle-stroke-width": 1,
        },
      });
      map.addLayer({
        id: "clinics-circle",
        type: "circle",
        source: "clinics",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 9, 2, 14, 5],
          "circle-color": "#b91c1c",
          "circle-opacity": 0.8,
        },
      });
      map.addLayer({
        id: "candidate-point",
        type: "circle",
        source: "candidate",
        filter: ["==", ["geometry-type"], "Point"],
        paint: {
          "circle-radius": 8,
          "circle-color": "#1d4ed8",
          "circle-stroke-color": "#ffffff",
          "circle-stroke-width": 3,
        },
      });

      map.on("click", (e) => {
        setPoint({ lat: e.lngLat.lat, lng: e.lngLat.lng });
      });
      map.on("mouseenter", "clinics-circle", () => (map.getCanvas().style.cursor = "pointer"));
      map.on("mouseleave", "clinics-circle", () => (map.getCanvas().style.cursor = ""));
      map.on("click", "clinics-circle", (e) => {
        const id = e.features?.[0]?.properties?.id;
        if (id === undefined) return;
        // The layer carries positions only; fetch the one record being asked
        // about rather than shipping every name and address to every phone.
        const popup = new maplibregl.Popup()
          .setLngLat(e.lngLat)
          .setHTML("<em>読み込み中…</em>")
          .addTo(map);
        api
          .clinic(Number(id))
          .then((clinic) => {
            const types = clinic.clinic_types?.length
              ? `<br><span class="popup__types">${escapeHtml(clinic.clinic_types.join("、"))}</span>`
              : "";
            popup.setHTML(
              `<strong>${escapeHtml(clinic.name)}</strong><br>` +
                `${escapeHtml(clinic.address ?? "")}${types}`,
            );
          })
          .catch((err) => popup.setHTML(escapeHtml(err.message)));
      });

      setReady(true);
    });

    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  // ------------------------------------------------------------- load layers
  // Every layer follows the viewport. Loading all of Tokyo is
  // 13MB, which is fine on a desktop and unusable on a phone; a city-level
  // viewport is well under one.
  const loadViewport = useCallback(async () => {
    const map = mapRef.current;
    if (!map) return;
    const b = map.getBounds();
    const bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]
      .map((v) => v.toFixed(5))
      .join(",");
    const zoom = map.getZoom();

    const request = ++viewportRequest.current;
    try {
      const [muni, mesh, clinics, stations] = await Promise.all([
        // Most of the boundary payload is Izu and Ogasawara coastline, a
        // thousand kilometres from anything the user is looking at.
        api.municipalities({ prefecture_code: "13", bbox }),
        zoom >= MIN_ZOOM.meshes
          ? api.meshes({ bbox, profile: profile || undefined, limit: 4000 })
          : Promise.resolve(EMPTY_RESPONSE),
        zoom >= MIN_ZOOM.clinics
          ? api.clinics({ bbox, fields: "points", limit: 5000 })
          : Promise.resolve(EMPTY_RESPONSE),
        zoom >= MIN_ZOOM.stations
          ? api.stations({ bbox, limit: 2000 })
          : Promise.resolve(EMPTY_RESPONSE),
      ]);
      // A slow response for an older viewport must not overwrite a newer one.
      if (request !== viewportRequest.current) return;

      (map.getSource("municipalities") as maplibregl.GeoJSONSource)?.setData(muni as never);
      (map.getSource("meshes") as maplibregl.GeoJSONSource)?.setData(mesh as never);
      (map.getSource("clinics") as maplibregl.GeoJSONSource)?.setData(clinics as never);
      (map.getSource("stations") as maplibregl.GeoJSONSource)?.setData(stations as never);
      setZoomedOut(zoom < MIN_ZOOM.clinics);
      setLayerError(null);
    } catch (e) {
      if (request === viewportRequest.current) {
        setLayerError(e instanceof Error ? e.message : String(e));
      }
    }
  }, [profile]);

  useEffect(() => {
    if (!ready || !mapRef.current) return;
    const map = mapRef.current;
    let timer: number | undefined;
    const schedule = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(loadViewport, 250);
    };
    schedule();
    map.on("moveend", schedule);
    return () => {
      window.clearTimeout(timer);
      map.off("moveend", schedule);
    };
  }, [ready, loadViewport]);

  // ------------------------------------------------------- mesh colour ramp
  useEffect(() => {
    if (!ready || !mapRef.current) return;
    const map = mapRef.current;
    if (!map.getLayer("meshes-fill")) return;

    const paint =
      meshMetric === "overall_score"
        ? ([
            "case",
            ["==", ["get", "overall_score"], null],
            "#d1d5db",
            [
              "interpolate", ["linear"], ["coalesce", ["get", "overall_score"], 0],
              0, "#3b4cc0", 25, "#6f8fd6", 50, "#d9d9d9", 75, "#e88b6a", 100, "#b40426",
            ],
          ] as never)
        : ([
            "interpolate", ["linear"], ["coalesce", ["get", "population"], 0],
            0, "#f7fbff", 2000, "#c6dbef", 8000, "#6baed6", 20000, "#2171b5", 40000, "#08306b",
          ] as never);
    map.setPaintProperty("meshes-fill", "fill-color", paint);
  }, [ready, meshMetric]);

  // ----------------------------------------------------------- layer toggles
  useEffect(() => {
    if (!ready || !mapRef.current) return;
    const map = mapRef.current;
    const mapping: Record<keyof LayerState, string[]> = {
      municipalities: ["municipalities-line"],
      meshes: ["meshes-fill"],
      clinics: ["clinics-circle"],
      stations: ["stations-circle"],
    };
    for (const [key, ids] of Object.entries(mapping)) {
      for (const id of ids) {
        if (map.getLayer(id)) {
          map.setLayoutProperty(id, "visibility",
            layers[key as keyof LayerState] ? "visible" : "none");
        }
      }
    }
  }, [ready, layers]);

  // -------------------------------------------------------- candidate marker
  useEffect(() => {
    if (!ready || !mapRef.current) return;
    const source = mapRef.current.getSource("candidate") as maplibregl.GeoJSONSource | undefined;
    if (!source) return;
    if (!point) {
      source.setData(EMPTY_FC as never);
      return;
    }
    source.setData({
      type: "FeatureCollection",
      features: [
        circlePolygon(point.lng, point.lat, radius),
        {
          type: "Feature",
          geometry: { type: "Point", coordinates: [point.lng, point.lat] },
          properties: {},
        },
      ],
    } as never);
  }, [ready, point, radius]);

  // ---------------------------------------------------------------- analysis
  useEffect(() => {
    if (!point) {
      setAnalysis(null);
      setSheetOpen(false);
      return;
    }
    setSheetOpen(true);
    let cancelled = false;
    setAnalysing(true);
    setError(null);
    api
      .candidateAnalysis({ lat: point.lat, lng: point.lng, radius, profile: profile || undefined })
      .then((res) => !cancelled && setAnalysis(res))
      .catch((e: ApiError) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setAnalysing(false));
    return () => {
      cancelled = true;
    };
  }, [point, radius, profile]);

  // Deep link from the ranking table: /?lat=..&lng=..
  useEffect(() => {
    if (!ready) return;
    const lat = Number(searchParams.get("lat"));
    const lng = Number(searchParams.get("lng"));
    if (!Number.isFinite(lat) || !Number.isFinite(lng) || (!lat && !lng)) return;
    mapRef.current?.flyTo({ center: [lng, lat], zoom: 14 });
    setPoint({ lat, lng });
    setSearchParams({}, { replace: true });
  }, [ready, searchParams, setSearchParams]);

  const searchStations = useCallback(async () => {
    if (!stationQuery.trim()) {
      setStationHits([]);
      return;
    }
    const res = await api.stations({ q: stationQuery.trim(), limit: 8 });
    setStationHits(res.features);
  }, [stationQuery]);

  const flyTo = useCallback((lng: number, lat: number) => {
    mapRef.current?.flyTo({ center: [lng, lat], zoom: 14 });
    setPoint({ lat, lng });
    setStationHits([]);
  }, []);

  const radii = useMemo(() => meta?.trade_area_radii_m ?? [500, 1000, 2000], [meta]);
  const canAddCandidate = candidates.length < MAX_CANDIDATES;

  return (
    <div className="mappage">
      <div className={settingsOpen ? "toolbar toolbar--open" : "toolbar"}>
        <button
          type="button"
          className="toolbar__toggle"
          aria-expanded={settingsOpen}
          onClick={() => setSettingsOpen((v) => !v)}
        >
          {settingsOpen ? "設定を閉じる" : "表示設定"}
        </button>

        <label className="toolbar__foldable">
          分析プロファイル
          <select value={profile} onChange={(e) => setProfile(e.target.value)}>
            {meta?.profiles.map((p) => (
              <option key={p.profile} value={p.profile}>
                {p.label}
              </option>
            ))}
          </select>
        </label>

        <div className="toolbar__radii">
          <span>商圏半径</span>
          {radii.map((r) => (
            <button
              key={r}
              type="button"
              className={r === radius ? "chip chip--on" : "chip"}
              onClick={() => setRadius(r)}
            >
              {r >= 1000 ? `${r / 1000}km` : `${r}m`}
            </button>
          ))}
        </div>

        <label className="toolbar__foldable">
          メッシュ表示
          <select value={meshMetric} onChange={(e) => setMeshMetric(e.target.value as MeshMetric)}>
            <option value="overall_score">候補地スコア</option>
            <option value="population">人口</option>
          </select>
        </label>

        <div className="toolbar__layers toolbar__foldable">
          {(
            [
              ["meshes", "メッシュ"],
              ["clinics", "歯科医院"],
              ["stations", "駅"],
              ["municipalities", "境界"],
            ] as [keyof LayerState, string][]
          ).map(([key, label]) => (
            <label key={key} className="toggle">
              <input
                type="checkbox"
                checked={layers[key]}
                onChange={(e) => setLayers((s) => ({ ...s, [key]: e.target.checked }))}
              />
              {label}
            </label>
          ))}
        </div>

        <div className="toolbar__search toolbar__foldable">
          <input
            value={stationQuery}
            placeholder="駅名で移動"
            onChange={(e) => setStationQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && searchStations()}
          />
          <button type="button" onClick={searchStations}>
            検索
          </button>
          {stationHits.length > 0 && (
            <ul className="searchhits">
              {stationHits.map((f) => {
                const c = (f.geometry as GeoJSON.Point).coordinates;
                return (
                  <li key={String(f.properties?.station_id)}>
                    <button type="button" onClick={() => flyTo(c[0], c[1])}>
                      {String(f.properties?.name)}{" "}
                      <span className="muted">
                        {num(f.properties?.daily_passengers as number | null)}人/日
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>

      <div className="mappage__body">
        <div className="mapwrap">
          <div ref={containerRef} className="map" />
          {!hasBasemap && (
            <div className="map__hint">
              背景地図タイルが未設定です（VITE_BASEMAP_STYLE / VITE_RASTER_TILES）。
              データレイヤーのみ表示しています。
            </div>
          )}
          {layerError && <div className="map__error">{layerError}</div>}
          {zoomedOut && !layerError && (
            <div className="map__hint map__hint--zoom">
              広域表示のため歯科医院を省略しています。拡大すると表示されます。
            </div>
          )}
          <div className="map__legend">
            <span className="dot dot--clinic" /> 歯科医院
            <span className="dot dot--station" /> 駅
            <span className="ramp" /> {meshMetric === "overall_score" ? "スコア低→高" : "人口少→多"}
          </div>
        </div>

        <aside className={sheetOpen ? "panel panel--open" : "panel"}>
          {analysis && (
            <button
              type="button"
              className="panel__grip"
              aria-expanded={sheetOpen}
              onClick={() => setSheetOpen((v) => !v)}
            >
              <span className="panel__grip-bar" />
              <span className="panel__grip-label">
                {sheetOpen ? "閉じる" : `総合スコア ${analysis.scores.overall ?? "—"}`}
              </span>
            </button>
          )}
          {!point && (
            <div className="panel__empty">
              <h2>候補地点を指定してください</h2>
              <p>
                地図上をクリックすると、その地点を中心とした商圏（
                {radii.map((r) => (r >= 1000 ? `${r / 1000}km` : `${r}m`)).join(" / ")}）を分析します。
              </p>
              <p className="muted">駅名検索でも地点を指定できます。</p>
            </div>
          )}

          {analysing && <p className="muted">分析中…</p>}
          {error && <p className="notice notice--error">分析に失敗しました: {error}</p>}

          {analysis && (
            <>
              <div className="panel__head">
                <h2>候補地点</h2>
                <p className="muted small">
                  {analysis.location.lat.toFixed(5)}, {analysis.location.lng.toFixed(5)} ／ 半径{" "}
                  {distance(analysis.radius_m)}
                </p>
                <div className="panel__actions">
                  <button
                    type="button"
                    disabled={!canAddCandidate}
                    onClick={() => add(analysis.location.lat, analysis.location.lng)}
                  >
                    比較に追加（{candidates.length}/{MAX_CANDIDATES}）
                  </button>
                  {candidates.length > 0 && <Link to="/compare">比較画面へ →</Link>}
                </div>
              </div>

              {analysis.warnings.map((w) => (
                <p key={w} className="warn-inline">
                  {w}
                </p>
              ))}

              <ScorePanel analysis={analysis} />

              <h3>人口・競合</h3>
              <PopulationTable analysis={analysis} />

              <h3>アクセス・その他</h3>
              <AccessTable analysis={analysis} />

              <ProvenanceList provenance={analysis.provenance} />
              <Disclaimer text={analysis.disclaimer} />
            </>
          )}

          {candidates.length > 0 && (
            <div className="candidates">
              <h3>比較候補</h3>
              <ul>
                {candidates.map((c) => (
                  <li key={c.id}>
                    <button type="button" className="linkish" onClick={() => flyTo(c.lng, c.lat)}>
                      {c.label}
                    </button>
                    <span className="muted small">
                      {c.lat.toFixed(4)}, {c.lng.toFixed(4)}
                    </span>
                    <button type="button" className="linkish" onClick={() => remove(c.id)}>
                      削除
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] ?? c,
  );
}
