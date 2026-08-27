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

/**
 * Supabase の Auth を叩く。届かなかったときは、届かなかったと言う。
 *
 * `fetch` は接続そのものに失敗すると TypeError("Failed to fetch") を投げます。
 * これをそのまま画面に出すと、利用者には英語で「取得に失敗」とだけ見えて、
 * パスワードを間違えたのか、こちらの設定が壊れているのか、回線が悪いのかが
 * 区別できません。実際にこれで詰まりました。
 *
 * 分けられる情報は分けて出します。原因の切り分けはこちらの仕事で、
 * 利用者に推測させるものではありません。
 */
export class AuthUnreachable extends Error {
  constructor(readonly url: string) {
    super("認証サーバに接続できませんでした。"
      + "ネットワークか、サイト側の設定（接続先）に問題があります。"
      + "しばらく待っても直らないときは、運営者にこの画面を伝えてください。");
    this.name = "AuthUnreachable";
  }
}

async function call(path: string, init: RequestInit): Promise<Response> {
  if (!URL_KEY || !ANON_KEY) {
    throw new Error("サインインの接続先が設定されていません。運営者にお知らせください。");
  }
  const url = `${URL_KEY.replace(/\/+$/, "")}/auth/v1${path}`;
  try {
    return await fetch(url, init);
  } catch {
    // ここに来るのは HTTP 応答が返らなかったときだけ。401 や 400 は下で扱います。
    throw new AuthUnreachable(url);
  }
}

async function post(path: string, body: unknown): Promise<Session> {
  const res = await call(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", apikey: ANON_KEY },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    // 資格情報の誤りだけは、はっきりそう言います。いちばん多い失敗で、
    // かつ利用者が自分で直せる唯一の失敗だからです。
    if (res.status === 400 || res.status === 401) {
      throw new Error("メールアドレスかパスワードが違います。");
    }
    throw new Error(data.error_description || data.msg || data.error
      || `サインインできませんでした（${res.status}）。`);
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
  // パス は `/token`。ここを落とすと `/auth/v1?grant_type=password` になり、
  // Supabase は 404 を返す。その 404 には CORS ヘッダが付かないので、
  // ブラウザには CORS 違反として見え、fetch は "Failed to fetch" を投げる。
  // 原因（パスの間違い）から最も遠い症状が出るので、一度これで詰まった。
  signIn: (email: string, password: string) =>
    post("/token?grant_type=password", { email, password }),

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

/* ------------------------------------------------------- パスワードの設定 */

/**
 * メールのリンクから戻ってきたときの URL 断片。
 *
 * Supabase の招待・パスワード復旧のリンクは、最終的に Site URL へ
 * `#access_token=...&refresh_token=...&type=recovery` の形で戻します。
 * トークンが `#` の後ろにあるのは、サーバのログに残さないためです。
 * 誰も読まなければ、ただのトップページが表示されて終わります。
 */
export interface RecoveryGrant {
  kind: "recovery" | "invite" | "signup" | "magiclink";
  email?: string;
}

/** JWT の中身。検証はサーバ側の仕事で、ここでは表示のために覗くだけ。 */
function claims(token: string): Record<string, unknown> {
  try {
    const body = token.split(".")[1];
    return JSON.parse(atob(body.replace(/-/g, "+").replace(/_/g, "/")));
  } catch {
    return {};
  }
}

/**
 * URL 断片を読み、あれば取り込む。読んだ断片は履歴から消します。
 *
 * 消すのは、リロードや「戻る」でもう一度使われないようにするためと、
 * アドレスバーにトークンを晒したままにしないためです。
 */
export function takeRecoveryGrant(): RecoveryGrant | { error: string } | null {
  const hash = window.location.hash.replace(/^#/, "");
  if (!hash.includes("access_token=") && !hash.includes("error=")) return null;
  const params = new URLSearchParams(hash);
  const clear = () =>
    window.history.replaceState(null, "", window.location.pathname + window.location.search);

  const error = params.get("error_description") || params.get("error");
  if (error) {
    clear();
    // 期限切れがいちばん多いので、そうと分かる言葉にします。
    return { error: /expired|invalid/i.test(error)
      ? "リンクの有効期限が切れています。もう一度パスワード再設定を依頼してください。"
      : decodeURIComponent(error) };
  }

  const access_token = params.get("access_token");
  const refresh_token = params.get("refresh_token");
  if (!access_token || !refresh_token) return null;

  const payload = claims(access_token);
  store({
    access_token,
    refresh_token,
    expires_at: Math.floor(Date.now() / 1000) + Number(params.get("expires_in") ?? 3600),
    email: typeof payload.email === "string" ? payload.email : undefined,
  });
  clear();
  const type = params.get("type") ?? "recovery";
  return {
    kind: (["recovery", "invite", "signup", "magiclink"].includes(type)
      ? type : "recovery") as RecoveryGrant["kind"],
    email: typeof payload.email === "string" ? payload.email : undefined,
  };
}

/** いま持っているトークンで、自分のパスワードを設定する。 */
export async function setPassword(password: string): Promise<void> {
  const session = storedSession();
  if (!session) throw new Error("セッションがありません。リンクをもう一度開いてください。");
  const res = await call("/user", {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      apikey: ANON_KEY,
      Authorization: `Bearer ${session.access_token}`,
    },
    body: JSON.stringify({ password }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.error_description || data.msg || data.error
      || "パスワードを設定できませんでした。");
  }
}

/** 再設定メールを送る。存在しないアドレスでも同じ応答を返します。 */
export async function requestPasswordReset(email: string): Promise<void> {
  const res = await call("/recover", {
    method: "POST",
    headers: { "Content-Type": "application/json", apikey: ANON_KEY },
    body: JSON.stringify({ email }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error_description || data.msg || data.error
      || "メールを送信できませんでした。");
  }
}
