import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api } from "../lib/api";
import { authConfigured, storedSession } from "../lib/auth";
import type { AnalysisList } from "../lib/types";

/**
 * 過去に作ったレポートの一覧。
 *
 * ファイルを失くしたら作り直し、では運営者の API 費用が増えるだけです。
 * DB には残っているので、いつでも取り直せるようにします。
 *
 * 残り回数もここに出します。使い切ってから「上限です」と言われるより、
 * 使う前に見えているほうがいい。
 */
export function ReportsPage() {
  const [data, setData] = useState<AnalysisList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const signedIn = Boolean(storedSession());

  const load = useCallback(async () => {
    try {
      setData(await api.analysis.list());
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (authConfigured() && !signedIn) {
    return (
      <div className="page">
        <h2>マイレポート</h2>
        <p>サインインすると、過去に作成したレポートを一覧・再ダウンロードできます。</p>
      </div>
    );
  }

  return (
    <div className="page">
      <h2>マイレポート</h2>
      {data?.quota && (
        <p className="reports__quota">
          今期の残り <strong>{data.quota.remaining}</strong> 回
          （{data.quota.period_start} からの期間で {data.quota.used} /{" "}
          {data.quota.monthly_quota} 回使用）
        </p>
      )}
      {error && <p className="analysis__error">{error}</p>}
      {data && data.items.length === 0 && (
        <p>まだレポートがありません。<Link to="/">地図</Link>から地点を選んで開始してください。</p>
      )}

      <ul className="reports">
        {data?.items.map((item) => (
          <li key={item.id}>
            <div className="reports__head">
              <strong>
                {item.title || item.location_name
                  || `${item.latitude.toFixed(5)}, ${item.longitude.toFixed(5)}`}
              </strong>
              {item.verdict && <span className="report__verdict">{item.verdict}</span>}
            </div>
            <div className="reports__meta">
              {new Date(item.created_at).toLocaleString("ja-JP")} ／ 半径{" "}
              {item.radius_m.toLocaleString()}m ／ {item.status}
              {item.trace_ok === false && "（根拠の追跡に未解決あり）"}
            </div>
            <div className="reports__actions">
              {item.report_at ? (
                <button className="analysis__ghost" disabled={busy === item.id}
                        onClick={async () => {
                          setBusy(item.id);
                          try {
                            await api.analysis.download(item.id);
                          } catch (err) {
                            setError(err instanceof Error ? err.message : String(err));
                          } finally {
                            setBusy(null);
                          }
                        }}>
                  {busy === item.id ? "取得中…" : "Markdownをダウンロード"}
                </button>
              ) : (
                <span className="reports__pending">レポートはまだありません</span>
              )}
              <Link className="analysis__ghost"
                    to={`/?lat=${item.latitude}&lng=${item.longitude}&radius=${item.radius_m}`}>
                地図で開く
              </Link>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
