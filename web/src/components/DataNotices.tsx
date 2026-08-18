/**
 * Standing notices about what the numbers on screen are.
 *
 * These are not dismissible and not conditional on the page: if the database
 * holds synthetic data, or holds no official data at all, that is stated at the
 * top of every screen.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import type { DataStatus, Provenance } from "../lib/types";
import { date } from "../lib/format";

export function GlobalNotices() {
  const [status, setStatus] = useState<DataStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .dataStatus()
      .then(setStatus)
      .catch((e) => setError(e.message));
  }, []);

  if (error) {
    return (
      <div className="notice notice--error">
        APIに接続できません（{error}）。データ取得状況を確認できません。
      </div>
    );
  }
  if (!status) return null;

  return (
    <>
      {status.no_official_data_warning && (
        <div className="notice notice--error">
          <strong>公的データ未取得</strong>
          <span>{status.no_official_data_warning}</span>
          <Link to="/about">取得状況を見る →</Link>
        </div>
      )}
      {status.contains_sample_data && (
        <div className="notice notice--warn">
          <strong>サンプルデータ表示中</strong>
          <span>{status.sample_data_warning}</span>
          <Link to="/about">データソース一覧 →</Link>
        </div>
      )}
    </>
  );
}

export function Disclaimer({ text }: { text?: string }) {
  return (
    <p className="disclaimer">
      {text ??
        "本サービスの分析結果は、公開統計およびオープンデータを基にした参考情報です。特定地域における歯科医院の開業成功、収益性、患者数等を保証するものではありません。各データには取得時点があります。"}
    </p>
  );
}

export function ProvisionalBadge({ label }: { label?: string }) {
  return (
    <span className="badge badge--provisional" title="重みは仮説値であり、実績による較正は行っていません">
      暫定モデル{label ? `・${label}` : ""}
    </span>
  );
}

export function ProvenanceList({ provenance }: { provenance?: Provenance }) {
  if (!provenance) return null;
  return (
    <div className="provenance">
      <h4>この画面の数値の出典</h4>
      {provenance.sources.length === 0 && <p className="muted">読み込まれているデータがありません。</p>}
      <ul>
        {provenance.sources.map((s) => (
          <li key={`${s.dataset}-${s.source_id}`}>
            <span className="provenance__dataset">{s.dataset_label}</span>
            {s.dataset_kind === "sample" && <span className="badge badge--sample">サンプル</span>}
            <span className="provenance__name">
              {s.homepage_url ? (
                <a href={s.homepage_url} target="_blank" rel="noreferrer">
                  {s.name}
                </a>
              ) : (
                s.name
              )}
            </span>
            <span className="muted">
              {s.publisher} / データ時点 {date(s.source_date)} / 取込 {date(s.last_updated)} /{" "}
              {s.row_count.toLocaleString("ja-JP")}件
            </span>
          </li>
        ))}
      </ul>
      {provenance.datasets_unavailable.length > 0 && (
        <p className="provenance__missing">
          未取得のデータ:{" "}
          {provenance.datasets_unavailable.map((d) => d.dataset_label).join("、")}
          （該当する指標は算出されません）
        </p>
      )}
    </div>
  );
}
