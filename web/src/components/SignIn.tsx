import { useEffect, useState } from "react";
import { auth, authConfigured, storedSession } from "../lib/auth";

/**
 * サインイン。
 *
 * 自己登録はありません。アカウントは運営者が発行します。だから「新規登録」
 * ボタンを置かず、代わりに「アカウントをお持ちでない方は」の一文を置きます。
 * 登録できそうに見えて登録できない画面がいちばん不親切です。
 *
 * Supabase を設定していない環境（手元）では何も出しません。
 */
export function SignIn({ onChange }: { onChange?: (email: string | null) => void }) {
  const [email, setEmail] = useState(() => storedSession()?.email ?? "");
  const [password, setPassword] = useState("");
  const [signedIn, setSignedIn] = useState(() => Boolean(storedSession()));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    onChange?.(signedIn ? (storedSession()?.email ?? null) : null);
  }, [signedIn, onChange]);

  if (!authConfigured()) return null;

  if (signedIn) {
    return (
      <div className="signin signin--in">
        <span>{storedSession()?.email}</span>
        <button className="analysis__ghost" onClick={() => {
          auth.signOut();
          setSignedIn(false);
        }}>サインアウト</button>
      </div>
    );
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await auth.signIn(email, password);
      setPassword("");
      setSignedIn(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="signin" onSubmit={submit}>
      <input type="email" placeholder="メールアドレス" value={email} required
             autoComplete="username"
             onChange={(e) => setEmail(e.target.value)} />
      <input type="password" placeholder="パスワード" value={password} required
             autoComplete="current-password"
             onChange={(e) => setPassword(e.target.value)} />
      <button disabled={busy}>{busy ? "…" : "サインイン"}</button>
      {error && <p className="signin__error">{error}</p>}
      <p className="signin__note">
        アカウントは発行制です。お持ちでない方は運営者にお問い合わせください。
      </p>
    </form>
  );
}
