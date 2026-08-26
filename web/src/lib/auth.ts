/**
 * ログイン。Supabase Auth を、SDK を足さずに使う。
 *
 * 必要なのは3つ（サインイン・トークンの更新・サインアウト）だけで、そのために
 * 依存を1つ増やす理由がありません。Supabase の Auth は素の REST で、
 * 返ってくるのは JWT です。その JWT を API に持っていくと、サーバ側が
 * 検証してアカウントを引きます。
 *
 * トークンは localStorage に置きます。この端末にだけ残り、サーバには
 * リクエストのたびに Authorization ヘッダで送られます。
 */
const URL_KEY = import.meta.env.VITE_SUPABASE_URL ?? "";
const ANON_KEY = import.meta.env.VITE_SUPABASE_ANON_KEY ?? "";
const STORE = "kaigyou.session";

export interface Session {
  access_token: string;
  refresh_token: string;
  /** 期限（epoch 秒）。少し手前で更新します。 */
  expires_at: number;
  email?: string;
}

export function authConfigured(): boolean {
  return Boolean(URL_KEY && ANON_KEY);
}

export function storedSession(): Session | null {
  try {
    const raw = localStorage.getItem(STORE);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}

function store(session: Session | null) {
  try {
    if (session) localStorage.setItem(STORE, JSON.stringify(session));
    else localStorage.removeItem(STORE);
  } catch {
    /* プライベートウィンドウ等。ログインは通るが記憶されないだけ。 */
  }
}

async function post(path: string, body: unknown): Promise<Session> {
  const res = await fetch(`${URL_KEY}/auth/v1${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", apikey: ANON_KEY },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.error_description || data.msg || data.error
      || "サインインできませんでした。");
  }
  const session: Session = {
    access_token: data.access_token,
    refresh_token: data.refresh_token,
    expires_at: Math.floor(Date.now() / 1000) + (data.expires_in ?? 3600),
    email: data.user?.email,
  };
  store(session);
  return session;
}

export const auth = {
  signIn: (email: string, password: string) =>
    post("?grant_type=password", { email, password }),

  signOut() {
    store(null);
  },

  /**
   * いま使えるトークン。期限が近ければ更新します。
   *
   * 分析は数分かかるので、待っている間に期限が切れることがあります。
   * 切れたまま投げると 401 になり、利用者には「急にログアウトした」
   * ように見えます。
   */
  async token(): Promise<string | null> {
    const session = storedSession();
    if (!session) return null;
    if (session.expires_at - 60 > Date.now() / 1000) return session.access_token;
    try {
      const refreshed = await post("/token?grant_type=refresh_token",
        { refresh_token: session.refresh_token });
      return refreshed.access_token;
    } catch {
      store(null);
      return null;
    }
  },
};
