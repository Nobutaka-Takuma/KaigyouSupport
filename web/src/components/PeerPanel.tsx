import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { num } from "../lib/format";
import type {
  PeerComparison, PeerRank, PeerRow, PeerStandout, PeerTable,
} from "../lib/types";

/**
 * この地点は、**似た規模の土地の中でどこにいるか。**
 *
 * スコアの隣にこれを置く理由は 1 つです。偏差値は分布の中の位置を返しますが、
 * **相手の顔が見えません。** 「歯科の供給 100（非常に高い）」の次に読み手が
 * 訊くのは「新宿や池袋と比べても多いのか」で、偏差値はそれに答えません。
 *
 * 取りに行くのはこのパネル自身です（`/api/peers`）。地点分析の応答に混ぜると、
 * 地図のクリック 1 回が 1 秒遅くなります——**先に出るものが先に出るほうが、
 * 全部そろってから出るより速く見えます。**
 */
export function PeerPanel(
  { lat, lng, profile, catchment }:
    { lat: number; lng: number; profile?: string; catchment?: string },
) {
  const [view, setView] = useState<PeerComparison | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setView(null);
    setError(null);
    api.peers({ lat, lng, profile: profile || undefined, catchment })
      .then((res) => { if (!cancelled) { setView(res); setOpen(0); } })
      .catch((err) => { if (!cancelled) setError(String(err.message ?? err)); });
    return () => { cancelled = true; };
  }, [lat, lng, profile, catchment]);

  if (error) {
    return (
      <section className="peers">
        <h3>同規模の中でどこか</h3>
        <p className="warn-inline">比較を取得できませんでした: {error}</p>
      </section>
    );
  }
  if (!view) {
    return (
      <section className="peers">
        <h3>同規模の中でどこか</h3>
        <p className="muted small">似た規模の地点を探しています…</p>
      </section>
    );
  }

  const table: PeerTable | undefined = view.tables[open];
  return (
    <section className="peers">
      <h3>同規模の中でどこか</h3>
      <p className="muted small">
        {"偏差値は分布の中の位置です。ここは"}
        <strong>名前のある相手</strong>
        {"と並べます。"}
      </p>

      {view.tables.length > 1 && (
        <div className="peers__tabs" role="tablist">
          {view.tables.map((t, i) => (
            <button key={t.key} type="button" role="tab"
                    aria-selected={i === open}
                    className={i === open ? "is-active" : ""}
                    onClick={() => setOpen(i)}>
              {t.label}
            </button>
          ))}
        </div>
      )}

      {table && <PeerTableView table={table} />}

      {/* **比較できなかったことは書きます。** 空欄は「比較したが差が無い」に
          見えます。相手が名前で出る比較は、居ないことを黙って埋められません。 */}
      {view.unavailable.map((gap) => (
        <p key={gap.what} className="muted small">
          <strong>{gap.what}</strong>：比較していません（{gap.why}）
        </p>
      ))}

      {(view.basis.pool?.prefectures?.length ?? 0) > 0 && (
        <p className="muted small">
          {"比較できるのは、メッシュを取り込んである都道府県だけです（"}
          {view.basis.pool!.prefectures!.join("・")}
          {"）。"}{view.basis.note}
        </p>
      )}
    </section>
  );
}

function PeerTableView({ table }: { table: PeerTable }) {
  const referenced = (table.peers ?? []).filter((p) => !p.inside_band);
  return (
    <>
      {table.reference.description && (
        <p className="peers__basis">{table.reference.description}</p>
      )}

      {/* **まず「ここだけ違うところ」。** 全部の軸を先に並べると、読み手は
          自分で差を探すことになります。KSF を探す入口はここです。 */}
      {table.standouts.length > 0 ? (
        <ul className="peers__standouts">
          {table.standouts.map((item) => (
            <li key={item.metric} className={`peers__standout is-${item.direction}`}>
              <span className="peers__standout-label">{item.label}</span>
              <strong>{value(item.value, item.unit)}</strong>
              <span className="muted small">
                中央値 {value(item.median, item.unit)}／{gap(item)}
              </span>
              {item.reading && <span className="peers__reading">{item.reading}</span>}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted small">{emptyReason(table)}</p>
      )}

      {table.ranks.length > 0 && (
        <div className="peers__scroll">
          <table className="datatable peers__ranks">
            <thead>
              <tr><th>軸</th><th>この地点</th><th>同規模の中央値</th><th>差</th><th>順位</th></tr>
            </thead>
            <tbody>
              {table.ranks.map((rank) => (
                <tr key={rank.metric}>
                  <td>{rank.label}</td>
                  <td>{value(rank.value, rank.unit)}</td>
                  <td>{value(rank.median, rank.unit)}</td>
                  <td>{gap(rank)}</td>
                  <td>{rank.rank}/{rank.of}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <details className="peers__details">
        <summary>比べた相手（{table.peer_count} 件）の数値</summary>
        <div className="peers__scroll">
          <table className="datatable peers__matrix">
            <thead>
              <tr>
                <th>地点</th><th>距離</th>
                {table.columns.map((c) => (
                  <th key={c.key}>{c.label}{c.unit ? `（${c.unit}）` : ""}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr className="is-here">
                <td><strong>この地点</strong></td><td>—</td>
                {table.columns.map((c) => (
                  <td key={c.key}>{cell(table.site, c.key, c.unit)}</td>
                ))}
              </tr>
              {table.peers.map((peer) => (
                <tr key={peer.label} className={peer.inside_band ? "" : "is-reference"}>
                  <td>{peer.label}{peer.inside_band ? "" : "（参考）"}</td>
                  <td>{peer.distance_km != null ? `${peer.distance_km}km` : "—"}</td>
                  {table.columns.map((c) => (
                    <td key={c.key}>{cell(peer, c.key, c.unit)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {referenced.length > 0 && (
          <p className="muted small">
            {"「参考」は同規模と呼べない相手です"
             + "（中央値と順位には入れていません）。"}
            {referenced.filter((p) => p.guard_reason)
              .map((p) => `${p.label}：${p.guard_reason}`).join("／")}
          </p>
        )}
      </details>

      {table.supply_gap.length > 0 && (
        <details className="peers__details">
          <summary>
            {"同規模の中で、医院1件あたり人口がここより多い地点"}
            （{table.supply_gap.length} 件）
          </summary>
          {/* **推奨ではありません。** 数えたのは人口÷医院数だけで、動線も
              各院の中身も見ていません。売上・患者数の予測もしません。 */}
          <p className="muted small">
            <strong>推奨ではありません。</strong>
            {"数えたのは「医院1件あたりの人口」だけで、動線も各院の中身も"
             + "見ていません。売上や患者数の予測もしません。"
             + "候補地を絞る入口としてだけ使えます。"}
          </p>
          <ul className="peers__gaps">
            {table.supply_gap.map((item) => (
              <li key={item.label}>
                <strong>{item.label}</strong>{" "}
                {value(item.value, item.unit)}
                <span className="muted small">
                  （ここは {value(item.here, item.unit)}
                  {item.distance_km != null ? `／${item.distance_km}km` : ""}）
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </>
  );
}

/**
 * 際立った軸が出ないときの一言。**「無い」と「出していない」は別物です。**
 *
 * 相手が全部「参考」に落ちたときに「際立った軸はありません」と書くと、
 * 比較した結果 差が無かったように読めます。実際に起きているのは、
 * **その軸だけ同規模で、別の軸が桁違いだった**ことです（東京駅前の
 * 「人口が同規模の地点」に武蔵村山市が並んだときの従業者数は 193 倍）。
 */
function emptyReason(table: PeerTable): string {
  if (table.comparable_count >= 3) {
    return "同規模の地点と比べて、際立って違う軸はありません。";
  }
  const guarded = table.caution?.[0]?.label;
  if (table.comparable_count === 0 && guarded) {
    return `同規模と呼べる相手がいません（${guarded}が桁違いのため、`
      + "全件を「参考」にしています）。";
  }
  return `同規模と呼べる相手が少ないため（${table.comparable_count} 件）、`
    + "際立った軸は出していません。";
}

/** 率は小数第1位まで、実数は整数。**単位まで書かないと、率と人数が並びます。** */
function value(v: number | null | undefined, unit?: string | null): string {
  if (v === null || v === undefined) return "—";
  return unit === "%" ? `${num(v, 1)}%` : `${num(v)}${unit ?? ""}`;
}

function cell(row: PeerRow, key: string, unit?: string): string {
  const v = row.values?.[key];
  if (v === null || v === undefined) return "—";
  return unit === "%" ? num(v, 1) : num(v);
}

/**
 * 中央値との差。**率はポイント、実数は比。**
 *
 * 高齢化率 18.5% と中央値 20.4% の差は「1.9 ポイント」であって「9% 低い」
 * ではありません。後者は、読み手が人数の話だと受け取ります。
 */
function gap(row: PeerRank | PeerStandout): string {
  const points = row.gap_points;
  if (points !== null && points !== undefined) {
    return `${points > 0 ? "+" : ""}${points.toFixed(1)}ポイント`;
  }
  const percent = "gap_vs_median_pct" in row ? row.gap_vs_median_pct : row.gap_pct;
  if (percent === null || percent === undefined) return "—";
  return `${percent > 0 ? "+" : ""}${percent.toFixed(1)}%`;
}
