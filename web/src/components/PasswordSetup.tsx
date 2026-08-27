import { useEffect, useState } from "react";
import { setPassword, takeRecoveryGrant, type RecoveryGrant } from "../lib/auth";

/**
 * メールのリンクから戻ってきた人に、パスワードを決めてもらう画面。
 *
 * これが無いと、招待もパスワード再設定も**成立しません**。Supabase の
 * リンクは最終的に Site URL へ `#access_token=...&type=recovery` を付けて
 * 戻すだけで、その先は各アプリの仕事です。読む側が居なければ、利用者には
 * 「ただトップページが開いた」としか見えません。実際そうなっていました。
 *
 * 画面全体を覆います。この状態で他の操作をさせても意味が無く、
 * 「何をすればいいのか」が一目で分かるほうが親切だからです。
 */
export function PasswordSetup() {
  const [grant, setGrant] = useState<RecoveryGrant | null>(null);
  const [linkError, setLinkError] = useState<string | null>(null);
  const [password, setPw] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // 断片は一度きり。取り込んだら履歴から消えるので、mount 時に読みます。
  useEffect(() => {
    const taken = takeRecoveryGrant();
    if (!taken) return;
    if ("error" in taken) setLinkError(taken.error);
    else setGrant(taken);
  }, []);

  if (linkError) {
    return (
      <div className="setup">
        <div className="setup__card">
          <h1>リンクを開けませんでした</h1>
          <p className="setup__error">{linkError}</p>
          <button className="analysis__ghost"
                  onClick={() => setLinkError(null)}>閉じる</button>
        </div>
      </div>
    );
  }

  if (!grant) return null;

  const invited = grant.kind === "invite" || grant.kind === "signup";

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (password !== again) {
      setError("2つの入力が一致しません。");
      return;
    }
    if (password.length < 8) {
      setError("8文字以上にしてください。");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await setPassword(password);
      // 断片から取り込んだセッションはそのまま有効なので、これでサインイン
      // 済みです。読み込み直すのは、ヘッダーなど各所の「サインイン済みか」を
      // 一度に揃えるためで、状態を配り歩くより確実です。
      window.location.replace("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  return (
    <div className="setup">
      <form className="setup__card" onSubmit={submit}>
        <h1>{invited ? "パスワードを設定してください" : "新しいパスワードを設定"}</h1>
        {grant.email && <p className="muted small">{grant.email}</p>}
        <input type="email" value={grant.email ?? ""} autoComplete="username"
               readOnly hidden />
        <input type="password" placeholder="新しいパスワード（8文字以上）"
               value={password} required autoFocus autoComplete="new-password"
               onChange={(e) => setPw(e.target.value)} />
        <input type="password" placeholder="もう一度"
               value={again} required autoComplete="new-password"
               onChange={(e) => setAgain(e.target.value)} />
        <button disabled={busy}>{busy ? "設定しています…" : "設定して開始"}</button>
        {error && <p className="setup__error">{error}</p>}
        <p className="muted small">
          設定するとそのままサインインした状態になります。
        </p>
      </form>
    </div>
  );
}
