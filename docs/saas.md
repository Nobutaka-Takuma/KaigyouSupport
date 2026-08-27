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

### 最初の1回だけ：自分を管理者にする

鶏と卵なので、ここだけ SQL です。**Authentication → Users** で自分を追加し、
SQL Editor でこれを流します（UID は自分で探さなくても引けます）。

```sql
insert into accounts (user_id, email, display_name, monthly_quota, billing_day, is_admin)
select id::text, email, '運営者', 999, 1, true
  from auth.users where email = 'you@example.com'
on conflict (user_id) do update
   set is_admin = true, monthly_quota = 999, status = 'active';
```

パスワードは Admin API で直接入れます（メールを待つ必要はありません）。

```powershell
$SR = '<service_role キー>'   # Project Settings → API。全権限があるので厳重に
curl.exe -X PUT "https://<PROJECT>.supabase.co/auth/v1/admin/users/<UID>" `
  -H "apikey: $SR" -H "Authorization: Bearer $SR" -H "Content-Type: application/json" `
  -d '{\"password\":\"<決めたパスワード>\",\"email_confirm\":true}'
```

`email_confirm` を付けるのは、確認メールを踏んでいないとサインインが
弾かれるからです。

これ以降、**画面の「管理」タブ**から操作します（管理者にだけ表示されます）。

### 利用者を1人増やすたび

1. Supabase の **Authentication → Users → Invite user** でメールアドレスを招待
   （利用者には「パスワードを設定してください」のメールが届きます）。
   **独自 SMTP を設定していないと、ここでほぼ確実に詰まります。次項参照。**
2. 一覧に出た **User UID** をコピー
3. アプリの **管理 → アカウントを発行** に貼り付け、会社名・担当者名・
   月あたりの上限・締め日を入れて保存

同じ画面に、利用者ごとの**今期の生成回数**と**LLM の実費**が並びます。
上限に達したアカウントは行が色付きになります。

上限を変える・止めるときも、同じ画面の「編集」から。**月あたりの上限を 0 に
すると、新規の分析を開始できなくなります**（走っている分析は止めません）。

### メールは独自 SMTP に。売り始める前に

Supabase が最初から用意しているメール送信は動作確認用で、**1時間に2通**しか
通しません。確認メール・招待・パスワード再設定を合わせての数字です。超えた分は
静かに捨てられ、画面にはエラーが出ません。標準の送信元は SPF/DKIM が自分の
ドメインに紐づかないので、届いても迷惑メールに入りがちです。宛先が組織メンバーに
制限されることもあります。

利用者 3 人に招待を出した時点で詰まります。**Project Settings → Authentication →
SMTP Settings** で外部の送信サービス（Resend、Amazon SES など）を設定し、
送信元ドメインの DNS に SPF と DKIM を立ててください。設定すると
**Authentication → Rate Limits** の上限も引き上げられます。

届かないときに見る場所は **Authentication → Logs**。`over_email_send_rate_limit`
や 429 が出ていれば上限です。

急ぐときは、メールを経由せず Admin API で作れます。

```powershell
curl.exe -X POST "https://<PROJECT>.supabase.co/auth/v1/admin/users" `
  -H "apikey: $SR" -H "Authorization: Bearer $SR" -H "Content-Type: application/json" `
  -d '{\"email\":\"user@example.com\",\"password\":\"<初期パスワード>\",\"email_confirm\":true}'
```

### 利用者がパスワードを設定する画面

招待と再設定のリンクは、Site URL に `#access_token=...&type=recovery` を付けて
戻ってくるだけです。受ける画面が `web/src/components/PasswordSetup.tsx` で、
断片を読んでパスワード設定のフォームを出します。**Authentication → URL
Configuration** の Site URL と Redirect URLs を正しく設定していないと、
リンクが localhost やトップページに落ちます。

利用者が自分で再設定できるよう、サインイン欄に「パスワードを忘れた」を
置いてあります。これも SMTP が要ります。

---

## 5. 請求

**管理**タブ（= `GET /api/admin/usage`）が、請求書を書くための1画面です。

- 利用者ごとの**今期の生成回数**（`used_this_period`）
- 締め日基準の期間（`period_start`）。契約日を締め日にできます（1〜28日）
- **LLM の実費**（`api_cost_this_period_usd`）— 原価が売価を超えていないかの確認用

取り下げたジョブは数えません（押し間違いで枠が減ると、使うのが怖くなります）。
失敗したジョブは数えます（API 費用は発生しているため）。

Stripe を入れるのは、顧客が増えて手作業が割に合わなくなってからで十分です。

---

## 6. 途中で止まったとき

モデルの言い間違い（存在しない出典を書く、参照 id を取り違える、長さの上限で
JSON が途中で切れる）は一定の確率で起きます。**これらは自動でやり直します**
（既定 3 回まで。`config/analysis.yaml` の `worker.max_attempts`）。画面には
「やり直し 1回」と出るだけで、人が押しに行く必要はありません。

**何度やっても直らないもの**は 1 回で止めます。

| 止まる理由 | 対処 |
|---|---|
| 残高不足 | console.anthropic.com の Plans & Billing でクレジットを追加 |
| API キーが不正 | Vercel の `ANTHROPIC_API_KEY` を直して再デプロイ |
| 安全性の判定で拒否 | 地点や条件を変える |

判定は `server/kaigyou_intel/failures.py` にあります。**判定できない失敗は
やり直しません** — 分からないものを繰り返すのは、費用だけが増えていちばん
気づかれにくい失敗の仕方だからです。

外部事実の出典が1件だけ確かめられなかった場合は、その1件を落として続けます。
落としたことはレポートの「調べたが確認できなかったこと」に残ります。全部が
確かめられなかったときだけ止めます。

---

## 7. Pro に上げるとき

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

## 8. 手元の PC で回す使い方も残っています

```powershell
python -m kaigyou_etl analyze --poll 5
```

こちらは 1 呼び出しの制限がないので、5 段を通しで回します。開発時や、
まとめて何件も流したいときはこちらが速い。両方が同じ DB を見るので、
混ぜても壊れません（`FOR UPDATE SKIP LOCKED` で取り合いません）。
