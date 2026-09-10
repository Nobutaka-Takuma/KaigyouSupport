import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ApiError, api } from "../lib/api";
import { renderInline } from "../lib/markdown";
import type { TownDiagnosis, TownFact } from "../lib/types";

/**
 * 「この街、どんな街？」
 *
 * **入口を 1 行にします。** 地図を操作してもらうための画面ではありません。
 * 駅名を 1 つ入れたら、その街の意外なところが数枚返ってくる——それだけの
 * 画面です。詳しく見たくなった人だけが地図へ行きます。
 *
 * 数字を作らないのはここも同じです。カードの中身はサーバが選んでいて、
 * **画面は何が意外かを判断しません。** 根拠が弱いものはここに届く前に
 * 落ちています。
 */

/** 話題の色分け。**良し悪しではありません**——話題の違いです。 */
const KIND_LABEL: Record<string, string> = {
  level: "この街の水準",
  contrast: "ちぐはぐなところ",
  peers: "似た街と比べて",
};

export function TownPage() {
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState(params.get("q") ?? "");
  const [view, setView] = useState<TownDiagnosis | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const look = async (q: string, at?: { lat: number; lng: number }) => {
    if (!q.trim() && !at) return;
    setLoading(true);
    setError(null);
    setView(null);
    try {
      const res = await api.town(at ? { ...at } : { q: q.trim() });
      setView(res);
      setParams(at ? { lat: String(at.lat), lng: String(at.lng) } : { q: q.trim() },
                { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="town">
      <form
        className="town__search"
        onSubmit={(e) => { e.preventDefault(); void look(query); }}
      >
        <h1>この街、どんな街？</h1>
        <p className="muted">
          {"駅名か市区町村名を入れてください。公開データの比較から、"}
          <strong>この街の意外なところ</strong>
          {"を探します。"}
        </p>
        <div className="town__row">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="沼津駅／三軒茶屋／世田谷区"
            aria-label="駅名・市区町村名"
          />
          <button type="submit" disabled={loading || !query.trim()}>
            {loading ? "調べています…" : "調べる"}
          </button>
        </div>
      </form>

      {error && <p className="notice notice--error">{error}</p>}

      {view && (
        <>
          <div className="town__head">
            <h2>
              {view.matched.name ?? "この地点"}のまわりは、こんな街です
            </h2>
            <p className="muted small">
              {[view.place.prefecture, view.place.municipality]
                .filter(Boolean).join(" ")}
              ／半径 {view.place.radius_m}m の範囲で集計。
              {view.considered} 件の比較から {view.facts.length} 件を選びました。
            </p>
          </div>

          {view.facts.length === 0 && (
            <p className="notice">
              {"この地点では、根拠のはっきりした特徴を出せませんでした。"
               + "理由は下の「この街について、まだ言えないこと」に出しています"
               + "——どの指標も平均的だったのか、そもそも比較できなかったのかで、"
               + "意味がまったく違うためです。"}
            </p>
          )}

          <ol className="town__cards">
            {view.facts.map((fact, i) => (
              <li key={fact.id}>
                <FactCard fact={fact} index={i + 1} place={view.place} />
              </li>
            ))}
          </ol>

          {/* **別の街を調べたくなる導線。** この画面のいちばんの目的です。 */}
          {view.alternatives.length > 0 && (
            <section className="town__alts">
              <h3>同じ名前の別の街</h3>
              <p className="muted small">
                {"名前が同じ駅は全国にあります。別の街の話を、自分の街の話として"
                 + "読まないように、候補を出しています。"}
              </p>
              <ul>
                {view.alternatives.map((alt) => (
                  <li key={`${alt.name}-${alt.lat}`}>
                    <button type="button" className="linkish"
                            onClick={() => { void look(alt.name,
                                                       { lat: alt.lat, lng: alt.lng }); }}>
                      {alt.name}
                    </button>
                    {alt.note && <span className="muted small">　{alt.note}</span>}
                  </li>
                ))}
              </ul>
            </section>
          )}

          <section className="town__more">
            <Link to={`/?lat=${view.place.lat}&lng=${view.place.lng}`}>
              もっと詳しく見る（地図・商圏の数字・同規模の街との比較）→
            </Link>
          </section>

          {/* **出せなかった比較を、空欄にしません。** 全国順位が無いのは
              「意味がないから」ではなく「取り込んでいないから」です。 */}
          {view.unavailable.length > 0 && (
            <section className="town__gaps">
              <h3>この街について、まだ言えないこと</h3>
              <ul>
                {view.unavailable.map((gap) => (
                  <li key={gap.what}>
                    <strong>{gap.what}</strong>　{gap.why}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {view.disclaimer && (
            <p className="town__disclaimer">{view.disclaimer}</p>
          )}
        </>
      )}
    </div>
  );
}

function FactCard(
  { fact, index, place }:
    { fact: TownFact; index: number; place: TownDiagnosis["place"] },
) {
  return (
    <article className={`towncard towncard--${fact.kind}`}>
      <div className="towncard__head">
        <span className="towncard__no">{index}</span>
        <span className="towncard__kind">{KIND_LABEL[fact.kind] ?? fact.kind}</span>
      </div>
      <h3 className="towncard__title">{fact.title}</h3>
      {/* 文中の `**…**` を太字にします。renderInline はすべてエスケープして
          から組み立てるので、ここに渡してよい文字列です（lib/markdown.ts）。 */}
      <p className="towncard__fact"
         dangerouslySetInnerHTML={{ __html: renderInline(fact.fact) }} />
      {/* **比較が本体です。** 値だけでは、多いのか少ないのかが決まりません。 */}
      <p className="towncard__comparison"
         dangerouslySetInnerHTML={{ __html: renderInline(fact.comparison) }} />
      <p className="towncard__why"
         dangerouslySetInnerHTML={{ __html: renderInline(fact.why_interesting) }} />
      <footer className="towncard__foot">
        <span className="muted small">出典：{fact.source}</span>
        <Link className="small"
              to={`/?lat=${place.lat}&lng=${place.lng}`}>
          地図で見る →
        </Link>
      </footer>
    </article>
  );
}
