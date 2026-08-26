import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../lib/api";
import { authConfigured, storedSession } from "../lib/auth";
import type { AdminAccount } from "../lib/types";

/**
 * アカウントの発行と、今期の利用状況。
 *
 * ここが答えるのは1つだけです ―― **今期、誰が何回使ったか**。それが分かれば
 * 請求書は書けますし、原価（LLM の実費）と突き合わせられます。
 *
 * 発行が PowerShell の Invoke-RestMethod だった頃は、利用者を1人増やすたびに
 * コマンドを組み立てていました。数クリックで済むべき作業です。
 */
export function AdminPage() {
  const [rows, setRows] = useState<AdminAccount[] | null>(null);
  const [summary, setSummary] = useState<{ reports: number; cost: number | null }>();
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Partial<AdminAccount> | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await api.admin.usage();
      setRows(data.accounts);
      setSummary({ reports: data.reports_this_period,
                   cost: data.api_cost_this_period_usd });
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (authConfigured() && !storedSession()) {
    return <div className="page"><h2>管理</h2><p>サインインしてください。</p></div>;
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!editing?.user_id) return;
    setSaving(true);
    try {
      await api.admin.upsert(editing.user_id, {
        email: editing.email ?? null,
        display_name: editing.display_name ?? null,
        organisation: editing.organisation ?? null,
        monthly_quota: Number(editing.monthly_quota ?? 0),
        billing_day: Number(editing.billing_day ?? 1),
        status: editing.status ?? "active",
        is_admin: Boolean(editing.is_admin),
        note: editing.note ?? null,
      });
      setEditing(null);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="page">
      <h2>アカウントと利用状況</h2>
      {summary && (
        <p className="reports__quota">
          今期のレポート生成 <strong>{summary.reports}</strong> 件
          {summary.cost !== null && <> ／ LLM 実費 ${summary.cost.toFixed(2)}</>}
          <br />
          <small>
            請求書は手で発行します。ここは「誰に何回ぶん請求するか」と
            「原価が売価を超えていないか」を見るための画面です。
          </small>
        </p>
      )}
      {error && <p className="analysis__error">{error}</p>}

      <div className="admin__actions">
        <button onClick={() => setEditing({ monthly_quota: 5, billing_day: 1,
                                            status: "active" })}>
          アカウントを発行
        </button>
      </div>

      {editing && (
        <form className="admin__form" onSubmit={save}>
          <h3>{rows?.some((r) => r.user_id === editing.user_id)
            ? "アカウントを更新" : "アカウントを発行"}</h3>
          <label>
            <span>ユーザーID</span>
            <input required value={editing.user_id ?? ""}
                   placeholder="Supabase の Authentication → Users で発行された UUID"
                   onChange={(e) => setEditing({ ...editing, user_id: e.target.value })} />
            <small>
              Supabase の Authentication → Users → Invite user で招待し、
              一覧に出た User UID を貼り付けてください。
            </small>
          </label>
          <label>
            <span>メールアドレス</span>
            <input type="email" value={editing.email ?? ""}
                   onChange={(e) => setEditing({ ...editing, email: e.target.value })} />
          </label>
          <label>
            <span>担当者名</span>
            <input value={editing.display_name ?? ""}
                   onChange={(e) => setEditing({ ...editing, display_name: e.target.value })} />
          </label>
          <label>
            <span>会社名</span>
            <input value={editing.organisation ?? ""}
                   onChange={(e) => setEditing({ ...editing, organisation: e.target.value })} />
          </label>
          <label>
            <span>月あたりの上限</span>
            <input type="number" min={0} value={editing.monthly_quota ?? 0}
                   onChange={(e) => setEditing({ ...editing,
                     monthly_quota: Number(e.target.value) })} />
            <small>0 にすると新規の分析を開始できなくなります。</small>
          </label>
          <label>
            <span>締め日</span>
            <input type="number" min={1} max={28} value={editing.billing_day ?? 1}
                   onChange={(e) => setEditing({ ...editing,
                     billing_day: Number(e.target.value) })} />
            <small>1〜28。契約日を締め日にできます（29〜31は月により存在しません）。</small>
          </label>
          <label>
            <span>状態</span>
            <select value={editing.status ?? "active"}
                    onChange={(e) => setEditing({ ...editing, status: e.target.value })}>
              <option value="active">利用中</option>
              <option value="suspended">停止</option>
            </select>
          </label>
          <label>
            <span>備考</span>
            <input value={editing.note ?? ""}
                   onChange={(e) => setEditing({ ...editing, note: e.target.value })} />
          </label>
          <div className="admin__form-actions">
            <button disabled={saving}>{saving ? "保存中…" : "保存"}</button>
            <button type="button" className="analysis__ghost"
                    onClick={() => setEditing(null)}>取消</button>
          </div>
        </form>
      )}

      <div className="admin__table-wrap">
        <table className="admin__table">
          <thead>
            <tr>
              <th>会社 / 担当</th><th>今期</th><th>上限</th><th>締め日</th>
              <th>実費</th><th>状態</th><th></th>
            </tr>
          </thead>
          <tbody>
            {rows?.map((row) => (
              <tr key={row.user_id} className={row.remaining === 0 ? "is-out" : ""}>
                <td>
                  <strong>{row.organisation || row.display_name || row.email
                           || row.user_id}</strong>
                  <br /><small>{row.email}</small>
                </td>
                <td>{row.used_this_period} / {row.monthly_quota}</td>
                <td>{row.monthly_quota}</td>
                <td>{row.billing_day}日</td>
                <td>{row.api_cost_this_period_usd === null
                  ? "—" : `$${row.api_cost_this_period_usd.toFixed(2)}`}</td>
                <td>{row.status === "active" ? "利用中" : "停止"}
                  {row.is_admin && " / 管理者"}</td>
                <td>
                  <button className="analysis__ghost"
                          onClick={() => setEditing(row)}>編集</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
