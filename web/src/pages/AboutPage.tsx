/**
 * Data sources, acquisition status and disclaimers.
 *
 * This page states plainly which datasets were obtained and which were not,
 * including the reason for each failure. It is reachable from every screen.
 */
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { date, num } from "../lib/format";
import type { DataStatus, Meta } from "../lib/types";

const STATE_LABELS: Record<string, string> = {
  loaded: "取得済み",
  failed: "取得失敗",
  never_attempted: "未試行",
  empty: "0件",
  incomplete: "未完了",
};

const STEP_LABELS: Record<string, string> = {
  download: "ダウンロード",
  validate: "検証",
  transform: "変換",
  load: "投入",
};

export function AboutPage() {
  const [status, setStatus] = useState<DataStatus | null>(null);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.dataStatus().then(setStatus).catch((e) => setError(e.message));
    api.meta().then(setMeta).catch(() => undefined);
  }, []);

  return (
    <div className="page page--prose">
      <h1>データソースと注意事項</h1>

      <section>
        <h2>免責事項</h2>
        <blockquote className="disclaimer disclaimer--block">
          本サービスの分析結果は、公開統計およびオープンデータを基にした参考情報です。
          <br />
          特定地域における歯科医院の開業成功、収益性、患者数等を保証するものではありません。
          <br />
          各データには取得時点があります。
        </blockquote>
        {meta && (
          <p className="muted">
            本サービスが行わないこと: {meta.out_of_scope.join(" / ")}
          </p>
        )}
      </section>

      <section>
        <h2>データ取得状況</h2>
        {error && <p className="notice notice--error">取得状況を読み込めません: {error}</p>}
        {status && (
          <>
            <p>
              公的データを取得できた情報源:{" "}
              <strong>
                {status.official_sources_loaded} / {status.official_sources_configured}
              </strong>
              {status.sample_sources_loaded > 0 && (
                <>
                  {" "}／ サンプル（合成）データ: <strong>{status.sample_sources_loaded}</strong> 件
                </>
              )}
            </p>
            {status.no_official_data_warning && (
              <p className="notice notice--error">{status.no_official_data_warning}</p>
            )}
            {status.sample_data_warning && (
              <p className="notice notice--warn">{status.sample_data_warning}</p>
            )}

            <div className="sourcelist">
              {status.sources.map((s) => (
                <article key={s.source_id} className={`sourcecard sourcecard--${s.state}`}>
                  <header>
                    <span className={`statechip statechip--${s.state}`}>
                      {STATE_LABELS[s.state] ?? s.state}
                    </span>
                    {s.dataset_kind === "sample" && (
                      <span className="badge badge--sample">サンプル（合成データ）</span>
                    )}
                    <h3>{s.name}</h3>
                  </header>
                  <dl>
                    <div>
                      <dt>提供元</dt>
                      <dd>{s.publisher ?? "—"}</dd>
                    </div>
                    <div>
                      <dt>ライセンス</dt>
                      <dd>{s.license ?? "—"}</dd>
                    </div>
                    <div>
                      <dt>取込件数</dt>
                      <dd>{num(s.row_total)} 件</dd>
                    </div>
                    <div>
                      <dt>最終試行</dt>
                      <dd>{date(s.last_attempt_at)}</dd>
                    </div>
                    {s.homepage_url && (
                      <div>
                        <dt>公式ページ</dt>
                        <dd>
                          <a href={s.homepage_url} target="_blank" rel="noreferrer">
                            {s.homepage_url}
                          </a>
                        </dd>
                      </div>
                    )}
                    {s.configured_url && (
                      <div>
                        <dt>取得URL</dt>
                        <dd className="mono small">{s.configured_url}</dd>
                      </div>
                    )}
                  </dl>

                  {s.reason && <p className="sourcecard__reason">{s.reason}</p>}

                  {Object.keys(s.steps).length > 0 && (
                    <table className="datatable datatable--steps">
                      <thead>
                        <tr>
                          <th>処理</th>
                          <th>結果</th>
                          <th>件数</th>
                          <th>詳細</th>
                        </tr>
                      </thead>
                      <tbody>
                        {["download", "validate", "transform", "load"]
                          .filter((step) => s.steps[step])
                          .map((step) => {
                            const info = s.steps[step];
                            return (
                              <tr key={step}>
                                <td>{STEP_LABELS[step]}</td>
                                <td className={`step step--${info.status}`}>{info.status}</td>
                                <td className="right">{num(info.record_count)}</td>
                                <td className="small">
                                  {info.error_type
                                    ? `${info.error_type}: ${info.error_message}`
                                    : "—"}
                                </td>
                              </tr>
                            );
                          })}
                      </tbody>
                    </table>
                  )}
                </article>
              ))}
            </div>
          </>
        )}
      </section>

      {meta && (
        <section>
          <h2>スコアリングモデル（暫定）</h2>
          <p>
            スコアは<strong>暫定モデル</strong>です。重みは仮説値であり、開業実績による較正は行っていません。
            重みは <code>config/scoring.yaml</code> で変更でき、コードの修正は不要です。
          </p>
          {meta.profiles.map((p) => (
            <div key={p.profile} className="modelcard">
              <h3>
                {p.label} <code>{p.profile}</code>
              </h3>
              {p.description && <p className="muted">{p.description}</p>}
              <p className="small">
                総合 ={" "}
                {Object.entries(p.overall_weights)
                  .map(([k, v]) => `${k} × ${v}`)
                  .join(" + ")}
              </p>
              <p className="small">
                需要の内訳:{" "}
                {Object.entries(p.demand_weights)
                  .map(([k, v]) => `${k} ${v}`)
                  .join(" / ")}
              </p>
              <p className="small muted">
                正規化: {p.normalization.method}（範囲 {p.normalization.clamp.join("–")}）／
                商圏半径 {p.trade_area_radii_m.join(" / ")} m
              </p>
            </div>
          ))}
        </section>
      )}

      <section>
        <h2>分析方法</h2>
        <ul>
          <li>商圏は候補地点を中心とした円で、PostGIS の geography バッファで生成しています。</li>
          <li>
            商圏人口は、円と重なる人口メッシュの面積按分で算出しています（メッシュ全体の面積に対する重複面積の比率で加重）。
          </li>
          <li>競合指標は「商圏人口 ÷ 商圏内歯科医院数」です。0件の場合はゼロ除算を避け、設定値を適用します。</li>
          <li>スコアは読み込み済みデータの分布に対する相対値です（5–95パーセンタイルで正規化）。</li>
          <li>算出に必要なデータが欠けている指標は、0とはせず「算出不可」として扱います。</li>
        </ul>
      </section>
    </div>
  );
}
