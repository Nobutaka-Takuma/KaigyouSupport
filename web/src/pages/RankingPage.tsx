/**
 * Mesh-level ranking.
 *
 * Deliberately secondary to the map: it answers "where should I look?" and
 * hands off to the point analysis, which is where the actual decision is made.
 */
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { distance, num, percent, score, scoreColor } from "../lib/format";
import type { Meta, RankingResponse } from "../lib/types";
import { Disclaimer, ProvenanceList, ProvisionalBadge } from "../components/DataNotices";
import { useCandidates, MAX_CANDIDATES } from "../lib/candidates";

const PAGE_SIZE = 50;

export function RankingPage() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [profile, setProfile] = useState("");
  const [minPopulation, setMinPopulation] = useState(0);
  const [area, setArea] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<RankingResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const { points, add } = useCandidates();
  const navigate = useNavigate();

  useEffect(() => {
    api.meta().then((m) => {
      setMeta(m);
      setProfile(m.active_profile);
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .rankings({
        limit: PAGE_SIZE,
        offset,
        profile: profile || undefined,
        min_population: minPopulation || undefined,
        area: area || undefined,
      })
      .then((res) => !cancelled && (setData(res), setError(null)))
      .catch((e) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [profile, offset, minPopulation, area]);

  return (
    <div className="page">
      <header className="page__head">
        <h1>東京都 開業候補地ランキング</h1>
        <p className="muted">
          1kmメッシュ単位で、候補地点分析と同じエンジン・同じ重みでスコアを算出しています。
          {data && `（商圏半径 ${distance(data.radius_m)}）`}
        </p>
        {data && <ProvisionalBadge label={data.model.label} />}
      </header>

      <div className="filters">
        <label>
          プロファイル
          <select
            value={profile}
            onChange={(e) => {
              setProfile(e.target.value);
              setOffset(0);
            }}
          >
            {meta?.profiles.map((p) => (
              <option key={p.profile} value={p.profile}>
                {p.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          最低人口
          <input
            type="number"
            min={0}
            step={1000}
            value={minPopulation}
            onChange={(e) => {
              setMinPopulation(Number(e.target.value) || 0);
              setOffset(0);
            }}
          />
        </label>
        <label>
          エリア名
          <input
            value={area}
            placeholder="部分一致"
            onChange={(e) => {
              setArea(e.target.value);
              setOffset(0);
            }}
          />
        </label>
      </div>

      {error && <p className="notice notice--error">{error}</p>}
      {data?.message && <p className="notice notice--warn">{data.message}</p>}
      {data?.warnings?.map((w) => (
        <p key={w} className="warn-inline">
          {w}
        </p>
      ))}
      {loading && <p className="muted">読み込み中…</p>}

      {data && data.items.length > 0 && (
        <>
          <table className="datatable datatable--wide">
            <thead>
              <tr>
                <th>順位</th>
                <th>エリア</th>
                <th>メッシュ</th>
                <th className="right">総合</th>
                <th className="right">需要</th>
                <th className="right">競合</th>
                <th className="right">成長</th>
                <th className="right">アクセス</th>
                <th className="right">人口</th>
                <th className="right">歯科</th>
                <th className="right">人口/歯科</th>
                <th className="right">増減率</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.items.map((item) => (
                <tr key={item.mesh_code}>
                  <td>{item.rank}</td>
                  <td>{item.area_label ?? "—"}</td>
                  <td className="mono">{item.mesh_code}</td>
                  <td className="right">
                    <span
                      className="scorechip"
                      style={{ background: scoreColor(item.overall_score) }}
                    >
                      {score(item.overall_score)}
                    </span>
                  </td>
                  <td className="right">{score(item.demand_score)}</td>
                  <td className="right">{score(item.competition_score)}</td>
                  <td className="right">{score(item.growth_score)}</td>
                  <td className="right">{score(item.accessibility_score)}</td>
                  <td className="right">{num(item.population)}</td>
                  <td className="right">{num(item.facility_count)}</td>
                  <td className="right">{num(item.population_per_facility)}</td>
                  <td className="right">{percent(item.population_growth)}</td>
                  <td className="right nowrap">
                    <button
                      type="button"
                      className="linkish"
                      onClick={() =>
                        navigate(`/?lat=${item.lat}&lng=${item.lng}`)
                      }
                    >
                      地図
                    </button>
                    <button
                      type="button"
                      className="linkish"
                      disabled={points.length >= MAX_CANDIDATES}
                      onClick={() =>
                        add(item.lat, item.lng, item.area_label ?? item.mesh_code)
                      }
                    >
                      比較
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="pager">
            <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
              ← 前
            </button>
            <span>
              {offset + 1}–{Math.min(offset + PAGE_SIZE, data.total)} / {num(data.total)}
            </span>
            <button
              type="button"
              disabled={offset + PAGE_SIZE >= data.total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              次 →
            </button>
          </div>

          <ProvenanceList provenance={data.provenance} />
          <Disclaimer text={data.disclaimer} />
        </>
      )}
    </div>
  );
}
