# PCを常駐させずに運用する（Vercel + Supabase）

手元の PC で worker を回さずに、Vercel と Supabase だけで完結させる構成です。
利用者にアカウントを発行し、月あたりのレポート生成回数で管理します。
API 費用は運営者持ち、利用料は月額で請求（請求書は手で発行）。

---

## 1. なぜ「1呼び出し = 1ステップ」なのか

分析は 5 段あり、通しで 10〜20 分かかります。**Vercel の関数はそこまで動けません。**

| | 関数の最大実行時間 | Cron の粒度 |
|---|---|---|
| Hobby | 300秒（5分） | **1日1回のみ** |
| Pro（$20/月） | 800秒（13分） | 1分ごと |

そこで `/api/worker/tick` は **1 回につき 1 ステップだけ**進めて、Job を待ち行列に
戻します。次の呼び出しが続きを拾います。状態は全部 DB にあるので、途中で関数が
消えても続きから流れます（消えた実行は `stale_after_minutes` で待ち行列に戻ります）。

Hobby では Vercel の Cron が使えないので、**Supabase の pg_cron から叩きます**。
Pro に上げたら `vercel.json` の `crons` に移すだけで、コードの変更はありません。

---

## 2. Supabase 側の設定

### 2.1 テーブル

```powershell
$env:DATABASE_URL = 'postgresql://...supabase.com:5432/postgres?sslmode=require'
python -m kaigyou_etl migrate
```

`accounts` テーブルができます。認証情報は持ちません（Supabase Auth に任せます）。
ここにあるのは、認証では分からないこと — プラン・上限・停止フラグ — だけです。

### 2.2 認証

Supabase の **Authentication → Providers → Email** を有効にし、
**Sign-ups を無効**にしてください。自己登録は許しません。アカウントは
運営者が発行します。

**Project Settings → API** から次の 3 つを控えます。

| 値 | 使う場所 |
|---|---|
| Project URL | Vercel の `VITE_SUPABASE_URL` |
| `anon` public key | Vercel の `VITE_SUPABASE_ANON_KEY` |
| JWT Secret | Vercel の `SUPABASE_JWT_SECRET` |

`anon` キーは公開されて構いません（ブラウザに埋まります）。
**JWT Secret はサーバ側だけ**です。これが漏れると誰でも任意の利用者になれます。

### 2.3 worker を1分ごとに叩く（Hobby のとき）

`db/migrations/022_worker_schedule.sql.example` の URL とトークンを埋めて、
Supabase の **SQL Editor** で 1 回だけ実行します。自動では適用されません
（秘密を含むのでリポジトリに置けません）。

確認は `select * from cron.job_run_details order by start_time desc limit 20;`。

---

## 3. Vercel の環境変数

| 変数 | 値 |
|---|---|
| `DATABASE_URL` | Supabase の Transaction pooler（6543）+ `?sslmode=require` |
| `ANTHROPIC_API_KEY` | **今度は必要です。** 分析が Vercel 上で走るため |
| `KAIGYOU_WORKER_TOKEN` | worker を叩くための鍵。`python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `SUPABASE_JWT_SECRET` | 上で控えた JWT Secret |
| `VITE_SUPABASE_URL` | Project URL |
| `VITE_SUPABASE_ANON_KEY` | anon public key |
| `KAIGYOU_MAX_SEARCHES` | **Hobby では `6`。** 下記参照 |
| `VITE_RASTER_TILES` / `VITE_RASTER_ATTRIBUTION` | 背景地図（既存） |

環境変数を変えたら**再デプロイが必要です**。`VITE_` で始まるものはビルド時に
埋め込まれるので、特にそうです。

### `KAIGYOU_MAX_SEARCHES` について

STEP2 は Web 検索のたびに文脈を読み直すので、**検索回数がそのまま実行時間**に
なります。実測で 15 回のとき入力 110 万トークン・数分〜10分。Hobby の 300 秒には
入りません。**`6` から始めてください。** Pro に上げたらこの変数を消せば 15 に戻ります。

`KAIGYOU_ANALYSIS_TOKEN` は、アカウント機能を使うなら不要です
（ログイン済みの利用者かどうかで判断します）。

---

## 4. アカウントを発行する

1. Supabase の **Authentication → Users → Invite user** でメールアドレスを招待
2. 発行された **User UID** を控える
3. 自分の管理者アカウントで API を叩く

```powershell
$h = @{ Authorization = "Bearer <あなたのJWT>"; "Content-Type" = "application/json" }
$body = @{ email="a@example.co.jp"; display_name="◯◯歯科開業支援";
           organisation="◯◯株式会社"; monthly_quota=5; billing_day=15 } | ConvertTo-Json
Invoke-RestMethod -Method Put -Uri "https://<app>.vercel.app/api/admin/accounts/<UID>" `
                  -Headers $h -Body $body
```

最初の管理者だけは SQL で作ります（鶏と卵）。

```sql
insert into accounts (user_id, email, monthly_quota, is_admin)
values ('<あなたのUID>', 'you@example.com', 999, true)
on conflict (user_id) do update set is_admin = true;
```

---

## 5. 請求

`GET /api/admin/usage` が、請求書を書くための1画面です。

- 利用者ごとの**今期の生成回数**（`used_this_period`）
- 締め日基準の期間（`period_start`）。契約日を締め日にできます（1〜28日）
- **LLM の実費**（`api_cost_this_period_usd`）— 原価が売価を超えていないかの確認用

取り下げたジョブは数えません（押し間違いで枠が減ると、使うのが怖くなります）。
失敗したジョブは数えます（API 費用は発生しているため）。

Stripe を入れるのは、顧客が増えて手作業が割に合わなくなってからで十分です。

---

## 6. Pro に上げるとき

1. Supabase の cron を止める: `select cron.unschedule('kaigyou-worker-tick');`
2. `vercel.json` に足す:
   ```json
   "crons": [{ "path": "/api/worker/tick", "schedule": "* * * * *" }]
   ```
3. Vercel の環境変数に `CRON_SECRET` を設定（Vercel が自動で `Bearer` で送ります）
4. `vercel.json` の `maxDuration` を `300` → `800`
5. `KAIGYOU_MAX_SEARCHES` を削除（15 に戻る）

**コードの変更はありません。** 同じ `/api/worker/tick` を叩くだけです。

---

## 7. 手元の PC で回す使い方も残っています

```powershell
python -m kaigyou_etl analyze --poll 5
```

こちらは 1 呼び出しの制限がないので、5 段を通しで回します。開発時や、
まとめて何件も流したいときはこちらが速い。両方が同じ DB を見るので、
混ぜても壊れません（`FOR UPDATE SKIP LOCKED` で取り合いません）。
