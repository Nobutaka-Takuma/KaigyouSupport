/** The four component scores and the overall, with the raw catchment figures. */
import type { CandidateAnalysis } from "../lib/types";
import { distance, num, percent, score, scoreColor } from "../lib/format";
import { ProvisionalBadge } from "./DataNotices";

const COMPONENT_LABELS: Record<string, string> = {
  demand: "需要 Demand",
  competition: "競合 Competition",
  growth: "成長 Growth",
  accessibility: "アクセス Accessibility",
};

export function ScoreBar({ label, value }: { label: string; value: number | null }) {
  return (
    <div className="scorebar">
      <div className="scorebar__head">
        <span>{label}</span>
        <strong>{score(value)}</strong>
      </div>
      <div className="scorebar__track">
        <div
          className="scorebar__fill"
          style={{
            width: `${value ?? 0}%`,
            background: scoreColor(value),
            opacity: value === null ? 0.25 : 1,
          }}
        />
      </div>
    </div>
  );
}

export function ScorePanel({ analysis }: { analysis: CandidateAnalysis }) {
  const s = analysis.scores;
  return (
    <div className="scorepanel">
      <div className="scorepanel__overall">
        <div className="scorepanel__value" style={{ color: scoreColor(s.overall) }}>
          {score(s.overall)}
        </div>
        <div>
          <div className="scorepanel__caption">総合スコア</div>
          <ProvisionalBadge label={s.profile_label} />
        </div>
      </div>

      {s.unavailable_components.length > 0 && (
        <p className="warn-inline">
          データ不足で算出できない指標:{" "}
          {s.unavailable_components.map((c) => COMPONENT_LABELS[c] ?? c).join("、")}
        </p>
      )}

      <ScoreBar label={COMPONENT_LABELS.demand} value={s.demand} />
      <ScoreBar label={COMPONENT_LABELS.competition} value={s.competition} />
      <ScoreBar label={COMPONENT_LABELS.growth} value={s.growth} />
      <ScoreBar label={COMPONENT_LABELS.accessibility} value={s.accessibility} />

      {s.breakdown?.competition?.note && (
        <p className="muted small">競合スコア: {s.breakdown.competition.note}</p>
      )}
    </div>
  );
}

export function CatchmentNote({ analysis }: { analysis: CandidateAnalysis }) {
  /** Which shape the figures were measured in. Without it they are ambiguous. */
  if (!analysis.catchment_kind && !analysis.prefecture_name) return null;
  const walk = analysis.catchment_kind === "walk";
  return (
    <p className="muted small catchment-note">
      {/* Which prefecture this point was analysed as. The map's selector can
          say one thing while the click is somewhere else entirely, and a score
          normalised in another prefecture is not the score for this one. */}
      {analysis.prefecture_name && (
        <>分析対象: <strong>{analysis.prefecture_name}</strong>（スコアは同一都道府県内で正規化）<br /></>
      )}
      {analysis.catchment_kind && (
        <>
          商圏の形: <strong>{walk ? "徒歩圏（街路網に沿った距離）" : "円（直線距離）"}</strong>
          {analysis.catchment_area_km2 != null && ` ／ 面積 ${analysis.catchment_area_km2} km²`}
          {walk && "。川・線路・幹線道路による分断を反映しています。"}
        </>
      )}
    </p>
  );
}

export function PopulationTable({ analysis }: { analysis: CandidateAnalysis }) {
  const radii = analysis.by_radius ? Object.keys(analysis.by_radius) : [];
  if (radii.length === 0) return null;
  const rows: [string, (r: string) => string][] = [
    ["総人口", (r) => num(analysis.by_radius![r].population)],
    ["0〜14歳", (r) => num(analysis.by_radius![r].children)],
    ["15〜64歳", (r) => num(analysis.by_radius![r].working_age)],
    ["65歳以上", (r) => num(analysis.by_radius![r].elderly)],
    ["世帯数", (r) => num(analysis.by_radius![r].households)],
    ["従業者数（昼）", (r) => num(analysis.by_radius![r].workers)],
    ["事業所数", (r) => num(analysis.by_radius![r].establishments)],
    ["歯科医院数", (r) => num(analysis.by_radius![r].dental_clinics)],
    ["人口/歯科医院", (r) => num(analysis.by_radius![r].population_per_clinic)],
    ["従業者/歯科医院", (r) => num(analysis.by_radius![r].workers_per_clinic)],
  ];

  return (
    <table className="datatable">
      <thead>
        <tr>
          <th>指標</th>
          {radii.map((r) => (
            <th key={r} className="right">
              {Number(r) >= 1000 ? `${Number(r) / 1000}km` : `${r}m`}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map(([label, fn]) => (
          <tr key={label}>
            <td>{label}</td>
            {radii.map((r) => (
              <td key={r} className="right">
                {fn(r)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function AccessTable({ analysis }: { analysis: CandidateAnalysis }) {
  return (
    <table className="datatable">
      <tbody>
        <tr>
          <td>最寄歯科医院</td>
          <td className="right">{analysis.nearest_clinic.name ?? "—"}</td>
        </tr>
        <tr>
          <td>最寄歯科までの距離</td>
          <td className="right">{distance(analysis.nearest_clinic.distance_m)}</td>
        </tr>
        <tr>
          <td>最寄駅</td>
          <td className="right">{analysis.nearest_station.name ?? "—"}</td>
        </tr>
        <tr>
          <td>駅までの距離</td>
          <td className="right">{distance(analysis.nearest_station.distance_m)}</td>
        </tr>
        <tr>
          <td>駅乗降客数（日）</td>
          <td className="right">{num(analysis.nearest_station.daily_passengers)}</td>
        </tr>
        <tr>
          <td>人口増減率</td>
          <td className="right">{percent(analysis.population_growth)}</td>
        </tr>
      </tbody>
    </table>
  );
}
