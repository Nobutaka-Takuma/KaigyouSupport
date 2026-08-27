import { useEffect, useState } from "react";
import { auth, AuthUnreachable, authConfigured, requestPasswordReset, storedSession }
  from "../lib/auth";

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
  const [notice, setNotice] = useState<string | null>(null);
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

  /** 再設定メールを送る。
   *
   * 発行制なので「新規登録」はありませんが、パスワードを忘れる人は居ます。
   * これが無いと、そのたびに運営者が Supabase の管理画面を開くことになります。
   *
   * 送れたかどうかに関わらず同じ文言を返します。どのアドレスが登録済みかを
   * 画面から数えられるようにしないためです。
   */
  async function reset() {
    if (!email) {
      setError("メールアドレスを入力してから押してください。");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await requestPasswordReset(email);
      setNotice("登録されているアドレスであれば、再設定用のメールを送りました。");
    } catch (err) {
      // 「届かなかった」だけは伏せません。送れていないのに送ったと言うのは
      // 嘘で、利用者はメールを待ち続けることになります。それ以外の理由
      // （そのアドレスは無い等）は伏せたままにします。
      if (err instanceof AuthUnreachable) setError(err.message);
      else setNotice("登録されているアドレスであれば、再設定用のメールを送りました。");
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
      <button type="button" className="signin__link" disabled={busy} onClick={reset}>
        パスワードを忘れた
      </button>
      {error && <p className="signin__error">{error}</p>}
      {notice && <p className="signin__note">{notice}</p>}
      <p className="signin__note">
        アカウントは発行制です。お持ちでない方は運営者にお問い合わせください。
      </p>
    </form>
  );
}
