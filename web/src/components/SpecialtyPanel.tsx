/**
 * 標榜科目別の競合と診療時間。
 *
 * 「歯科医院 186 件」は、小児歯科をやるつもりの人にとっては 186 件ではない。
 * この表はその 186 件を看板で分け、1 件あたりの商圏人口まで出す。
 *
 * 気をつけていること 2 つ。
 *
 * 分母を必ず出す。科目別の件数は「診療科目が分かっている医院のうち何件か」で
 * あって「商圏内に何件あるか」ではない。被覆率が 100% でないときは表の頭に
 * 書く。
 *
 * 自由記載を区別する。インプラント・審美・訪問診療は標榜診療科目ではなく
 * 「その他」欄への自由記載にしかなく、東京都でインプラントと書いているのは
 * 全体の 1.2% しかない。実施している医院はもっと多い。だから件数の横に
 * 「自由記載」と出し、別の枠に分けて、少ないことを競合の少なさと読ませない。
 */
import type { CandidateAnalysis } from "../lib/types";

function number(value: number | null | undefined): string {
  return value == null ? "—" : value.toLocaleString("ja-JP");
}

export function SpecialtyPanel({ analysis }: { analysis: CandidateAnalysis }) {
  const spec = analysis.specialties;
  if (!spec || spec.with_data === 0) return null;

  const declared = spec.breakdown.filter((r) => !r.declared_only && r.key !== "other_medical");
  const freeText = spec.breakdown.filter((r) => r.declared_only);
  const population = analysis.population;
  const coverage = spec.coverage == null ? null : Math.round(spec.coverage * 100);

  return (
    <>
      <h3>標榜診療科目別の競合</h3>
      <p className="muted small">
        商圏内の歯科医院 {number(spec.total_clinics)} 件のうち、診療科目が分かるのは{" "}
        {number(spec.with_data)} 件
        {coverage != null && coverage < 100 ? `（${coverage}%）` : ""}。
        下の件数はその範囲での数字です。
      </p>

      {declared.length > 0 && (
        <table className="metrics">
          <thead>
            <tr>
              <th>標榜科目</th>
              <th>医院数</th>
              <th>占有率</th>
              <th>1件あたり商圏人口</th>
            </tr>
          </thead>
          <tbody>
            {declared.map((row) => (
              <tr key={row.key}>
                <td>{row.label}</td>
                <td>{number(row.count)}</td>
                <td>
                  {spec.with_data ? `${Math.round((row.count / spec.with_data) * 100)}%` : "—"}
                </td>
                <td>
                  {population == null || row.count === 0
                    ? "—"
                    : `${Math.round(population / row.count).toLocaleString("ja-JP")} 人`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {freeText.length > 0 && (
        <>
          <h4 className="small">自由記載の診療内容</h4>
          <p className="muted small">{spec.note}</p>
          <ul className="chips">
            {freeText.map((row) => (
              <li key={row.key}>
                {row.label} {number(row.count)}件
              </li>
            ))}
          </ul>
        </>
      )}

      <h4 className="small">診療時間</h4>
      <table className="metrics">
        <tbody>
          {spec.hours.counts.map((row) => (
            <tr key={row.key}>
              <th>{row.label}</th>
              <td>
                {number(row.count)} 件
                {spec.hours.declared && row.count != null
                  ? `（${Math.round((row.count / spec.hours.declared) * 100)}%）`
                  : ""}
              </td>
            </tr>
          ))}
          <tr>
            <th>週間診療時間の中央値</th>
            <td>
              {spec.hours.weekly_hours_median == null
                ? "—"
                : `${spec.hours.weekly_hours_median} 時間`}
            </td>
          </tr>
        </tbody>
      </table>
      <p className="muted small">
        診療時間の記載がある医院 {number(spec.hours.declared)} 件を分母としています。
        夜間診療は終了時刻が18:30以降の枠がある医院。
      </p>
    </>
  );
}

/**
 * 同じ地点を、競合を科目で絞ったモデルと絞らないモデルで採点した結果。
 *
 * 総合点だけ並べても「なぜ違うのか」が伝わらないので、どの科目で競合を
 * 数えたかを行に書く。ここが分かれば、小児歯科寄りの点が低いのは
 * 「子どもが少ないから」ではなく「小児歯科を出している医院が多いから」だと
 * 読み手が自分で判断できる。
 */
export function SpecialtyProfiles({ analysis }: { analysis: CandidateAnalysis }) {
  const rows = (analysis.scores_by_profile ?? []).filter((r) => r.competition_specialty);
  if (rows.length === 0) return null;
  const base = (analysis.scores_by_profile ?? []).find((r) => !r.competition_specialty);

  return (
    <>
      <h3>標榜科目で競合を絞ると</h3>
      <table className="metrics">
        <thead>
          <tr>
            <th>モデル</th>
            <th>競合の数え方</th>
            <th>総合スコア</th>
          </tr>
        </thead>
        <tbody>
          {base && (
            <tr>
              <td>{base.label}</td>
              <td className="muted">歯科医院すべて</td>
              <td>{base.overall ?? "—"}</td>
            </tr>
          )}
          {rows.map((row) => (
            <tr
              key={row.profile}
              className={row.profile === analysis.scores.profile ? "is-active" : undefined}
            >
              <td>{row.label}</td>
              <td className="muted">{row.competition_specialty_label} のみ</td>
              <td>{row.overall ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">
        いずれも暫定モデルで、実績データによる較正は行っていません。
        スコアは同一都道府県内のメッシュ分布に対する相対値です。
      </p>
    </>
  );
}
