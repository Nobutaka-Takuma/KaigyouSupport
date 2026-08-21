/** Side-by-side comparison of up to three candidate locations. */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { useCandidates, MAX_CANDIDATES } from "../lib/candidates";
import { usePrefecture } from "../lib/prefecture";
import { distance, num, percent, score, scoreColor } from "../lib/format";
import type { CompareResponse, Meta } from "../lib/types";
import { Disclaimer, ProvenanceList, ProvisionalBadge } from "../components/DataNotices";

type Row = {
  label: string;
  value: (i: number, d: CompareResponse) => string;
  highlight?: "high" | "low";
  raw?: (i: number, d: CompareResponse) => number | null;
};

const ROWS: Row[] = [
  { label: "総人口", value: (i, d) => num(d.locations[i].population), raw: (i, d) => d.locations[i].population, highlight: "high" },
  { label: "0〜14歳", value: (i, d) => num(d.locations[i].children), raw: (i, d) => d.locations[i].children, highlight: "high" },
  { label: "15〜64歳", value: (i, d) => num(d.locations[i].working_age), raw: (i, d) => d.locations[i].working_age, highlight: "high" },
  { label: "65歳以上", value: (i, d) => num(d.locations[i].elderly), raw: (i, d) => d.locations[i].elderly, highlight: "high" },
  { label: "世帯数", value: (i, d) => num(d.locations[i].households), raw: (i, d) => d.locations[i].households, highlight: "high" },
  { label: "従業者数（昼）", value: (i, d) => num(d.locations[i].workers), raw: (i, d) => d.locations[i].workers, highlight: "high" },
  { label: "歯科医院数", value: (i, d) => num(d.locations[i].dental_clinics), raw: (i, d) => d.locations[i].dental_clinics, highlight: "low" },
  { label: "人口/歯科医院", value: (i, d) => num(d.locations[i].population_per_clinic), raw: (i, d) => d.locations[i].population_per_clinic, highlight: "high" },
  { label: "最寄歯科距離", value: (i, d) => distance(d.locations[i].nearest_clinic.distance_m), raw: (i, d) => d.locations[i].nearest_clinic.distance_m, highlight: "high" },
  { label: "人口増減率", value: (i, d) => percent(d.locations[i].population_growth), raw: (i, d) => d.locations[i].population_growth, highlight: "high" },
  { label: "最寄駅", value: (i, d) => d.locations[i].nearest_station.name ?? "—" },
  { label: "駅距離", value: (i, d) => distance(d.locations[i].nearest_station.distance_m), raw: (i, d) => d.locations[i].nearest_station.distance_m, highlight: "low" },
  { label: "駅乗降客数", value: (i, d) => num(d.locations[i].nearest_station.daily_passengers), raw: (i, d) => d.locations[i].nearest_station.daily_passengers, highlight: "high" },
];

const SCORE_ROWS: Row[] = [
  { label: "需要 Demand", value: (i, d) => score(d.locations[i].scores.demand), raw: (i, d) => d.locations[i].scores.demand, highlight: "high" },
  { label: "競合 Competition", value: (i, d) => score(d.locations[i].scores.competition), raw: (i, d) => d.locations[i].scores.competition, highlight: "high" },
  { label: "成長 Growth", value: (i, d) => score(d.locations[i].scores.growth), raw: (i, d) => d.locations[i].scores.growth, highlight: "high" },
  { label: "アクセス Accessibility", value: (i, d) => score(d.locations[i].scores.accessibility), raw: (i, d) => d.locations[i].scores.accessibility, highlight: "high" },
];

export function ComparePage() {
  const { points, remove, clear } = useCandidates();
  const { code: prefecture } = usePrefecture();
  const [meta, setMeta] = useState<Meta | null>(null);
  const [profile, setProfile] = useState("");
  const [radius, setRadius] = useState(1000);
  const [data, setData] = useState<CompareResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api.meta().then((m) => {
      setMeta(m);
      setProfile(m.active_profile);
    });
  }, []);

  useEffect(() => {
    if (points.length === 0) {
      setData(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    api
      .compare({
        points: points.map((p) => `${p.lat},${p.lng}`).join(";"),
        labels: points.map((p) => p.label).join(";"),
        radius,
        profile: profile || undefined,
        prefecture_code: prefecture ?? undefined,
      })
      .then((res) => !cancelled && (setData(res), setError(null)))
      .catch((e) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [points, radius, profile, prefecture]);

  const best = (row: Row): number | null => {
    if (!data || !row.raw || !row.highlight) return null;
    let bestIdx: number | null = null;
    let bestVal: number | null = null;
    for (let i = 0; i < data.locations.length; i += 1) {
      const v = row.raw(i, data);
      if (v === null || v === undefined) continue;
      if (bestVal === null || (row.highlight === "high" ? v > bestVal : v < bestVal)) {
        bestVal = v;
        bestIdx = i;
      }
    }
    return bestIdx;
  };

  return (
    <div className="page">
      <header className="page__head">
        <h1>候補地比較</h1>
        <p className="muted">
          最大{MAX_CANDIDATES}地点まで比較できます。地図画面またはランキングから追加してください。
        </p>
        {data && <ProvisionalBadge label={data.model.label} />}
      </header>

      {points.length === 0 ? (
        <p className="notice notice--info">
          比較する候補地がありません。<Link to="/">地図画面</Link>で地点をクリックし「比較に追加」してください。
        </p>
      ) : (
        <>
          <div className="filters">
            <label>
              プロファイル
              <select value={profile} onChange={(e) => setProfile(e.target.value)}>
                {meta?.profiles.map((p) => (
                  <option key={p.profile} value={p.profile}>
                    {p.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              商圏半径
              <select value={radius} onChange={(e) => setRadius(Number(e.target.value))}>
                {(meta?.trade_area_radii_m ?? [500, 1000, 2000]).map((r) => (
                  <option key={r} value={r}>
                    {r >= 1000 ? `${r / 1000}km` : `${r}m`}
                  </option>
                ))}
              </select>
            </label>
            <button type="button" onClick={clear}>
              すべて削除
            </button>
          </div>

          {error && <p className="notice notice--error">{error}</p>}
          {loading && <p className="muted">読み込み中…</p>}

          {data?.warnings?.map((w) => (
            <p key={w} className="warn-inline">
              {w}
            </p>
          ))}

          {data && (
            <>
              <table className="datatable datatable--compare">
                <thead>
                  <tr>
                    <th>指標</th>
                    {data.locations.map((l, i) => (
                      <th key={i} className="right">
                        {l.label}
                        <div className="muted small">
                          {l.location.lat.toFixed(4)}, {l.location.lng.toFixed(4)}
                        </div>
                        <button type="button" className="linkish" onClick={() => remove(points[i].id)}>
                          削除
                        </button>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  <tr className="row--overall">
                    <td>総合スコア</td>
                    {data.locations.map((l, i) => (
                      <td key={i} className="right">
                        <span className="scorechip" style={{ background: scoreColor(l.scores.overall) }}>
                          {score(l.scores.overall)}
                        </span>
                      </td>
                    ))}
                  </tr>
                  {SCORE_ROWS.map((row) => {
                    const b = best(row);
                    return (
                      <tr key={row.label}>
                        <td>{row.label}</td>
                        {data.locations.map((_, i) => (
                          <td key={i} className={`right${b === i ? " cell--best" : ""}`}>
                            {row.value(i, data)}
                          </td>
                        ))}
                      </tr>
                    );
                  })}
                  <tr className="row--divider">
                    <td colSpan={data.locations.length + 1}>実数値</td>
                  </tr>
                  {ROWS.map((row) => {
                    const b = best(row);
                    return (
                      <tr key={row.label}>
                        <td>{row.label}</td>
                        {data.locations.map((_, i) => (
                          <td key={i} className={`right${b === i ? " cell--best" : ""}`}>
                            {row.value(i, data)}
                          </td>
                        ))}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <p className="muted small">
                網掛けは各指標で最も有利な地点です（歯科医院数と駅距離は小さいほど有利として扱っています）。
              </p>

              <ProvenanceList provenance={data.provenance} />
              <Disclaimer text={data.disclaimer} />
            </>
          )}
        </>
      )}
    </div>
  );
}
