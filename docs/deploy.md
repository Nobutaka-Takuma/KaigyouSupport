# スマホから見られるようにする（Vercel + Supabase）

ローカルで動いているものを、そのまま公開URLに載せる手順です。
アプリの構成は変わりません。**置き場所が変わるだけ**です。

```
スマホ ──HTTPS──> Vercel ┬─ 静的ファイル      … web/dist（Viteのビルド成果物）
                         └─ /api/*           … api/index.py（FastAPI）
                                    │
                                    └──> Supabase（PostgreSQL + PostGIS）
```

データを取ってくる処理（ETL）は**この経路に入りません**。
手元のPCから Supabase に直接書き込みます。Vercel 側は読むだけです。
そのため公開したアプリは、公開先の都合でデータが壊れることがありません。

所要時間の目安：Supabase の準備 10分 / データ投入 15〜30分 / Vercel 5分。

---

## 1. Supabase を用意する

1. [supabase.com](https://supabase.com) で新規プロジェクトを作成します。
   **リージョンは Northeast Asia (Tokyo)** を選んでください。
   データベースが遠いと、地図を動かすたびに待たされます。
2. 作成時に表示される **データベースパスワードを控えます**。あとから再表示できません。
3. SQL Editor で PostGIS を有効化します。

   ```sql
   create extension if not exists postgis;
   ```

   （`kaigyou-etl migrate` も同じことをしますが、権限の問題を先に潰しておくほうが確実です。）

### 接続文字列は3種類あり、使うのは Pooler の2つです

Supabase の **Project Settings → Database → Connection string** に出てきます。

| | ホスト / ポート | 使う場面 |
|---|---|---|
| Direct connection | `db.<ref>.supabase.co` : 5432 | **使いません**（下記） |
| **Session pooler** | `aws-N-<region>.pooler.supabase.com` : **5432** | 手元PCからのデータ投入（ETL） |
| **Transaction pooler** | `aws-N-<region>.pooler.supabase.com` : **6543** | Vercel の関数から |

**Direct connection は IPv6 でしか引けません。** 2024年初頭から Supabase は直接接続を
IPv6 専用にしています。家庭用回線や社内ネットワークは IPv4 のみのことが多く、その場合
名前解決の時点で落ちます。

```
error: OperationalError: failed to resolve host 'db.<ref>.supabase.co':
       [Errno 11001] getaddrinfo failed
```

これが出たら回線の問題でもタイプミスでもありません。**Session pooler に変えてください。**
Pooler は全プランで IPv4 に対応しています。

**Pooler ではユーザ名が変わります。** `postgres` ではなく `postgres.<プロジェクトID>` です。
ダッシュボードの文字列をそのまま貼れば間違えません。

Session（5432）と Transaction（6543）の違いは接続の寿命です。Session は接続を張って
いる間ずっと同じバックエンドなので大量INSERTに向き、Transaction はトランザクション毎に
バックエンドを割り当てるのでサーバレスに向きます。後者ではプリペアドステートメントが
使えないため、接続文字列を見て自動で切り替えます
（`server/kaigyou_core/db.py` の `is_pooled`）。設定はURLを貼るだけです。

---

## 2. 手元のPCから Supabase にデータを入れる

ローカルの Postgres に入れたときと同じコマンドを、`DATABASE_URL` だけ変えて実行します。

使うのは **Session pooler**（ホストが `pooler.supabase.com`、ポート **5432**）です。

Windows (PowerShell):

```powershell
$env:DATABASE_URL = 'postgresql://postgres.<プロジェクトID>:<パスワード>@aws-N-<region>.pooler.supabase.com:5432/postgres?sslmode=require'

.\.venv\Scripts\kaigyou-etl migrate
.\.venv\Scripts\kaigyou-etl load-local download
```

> **PowerShell では引用符を `'`（シングル）にしてください。**
> `"`（ダブル）で囲むと `$` が変数展開されます。パスワードに `$` が含まれていると
> `$6XrTDQT` のような部分が空文字に置き換わり、`$$` に至っては別の値が入ります。
> 画面上は正しく見えるのに認証だけ失敗する、という追いにくい壊れ方をします。
> シングルクォートなら中身はそのまま渡ります。
> （パスワードに `'` が入っている場合は `''` と2つ重ねてください。）

macOS / Linux:

```bash
export DATABASE_URL='postgresql://postgres.<プロジェクトID>:<パスワード>@aws-N-<region>.pooler.supabase.com:5432/postgres?sslmode=require'

.venv/bin/kaigyou-etl migrate
.venv/bin/kaigyou-etl load-local download
```

`?` や `$` を含むパスワードでも、URL に直接書いて構いません（libpq が正しく読みます）。
パーセントエンコードは不要です。

`download` は5つの元データを置いてあるフォルダです（README「実データを表示するまで」参照）。
`load-local` は中身を見て種類を判別するので、ファイル名は問いません。

終わったら確認します。

```bash
.venv/bin/kaigyou-etl status
```

`公的データを取得できた情報源: 4 / 4` と出れば投入完了です。

投入されるのは約 82MB（歯科医院 51,384件・500mメッシュ 5,449件・駅 9,317件・行政区域 62件）。
Supabase 無料枠の 500MB に収まります。

> **注意**：`load-local` の最後にスコアを再計算します。ここは数分かかります。
> ネットワーク越しなので、ローカルより長くなります。途中で止めないでください。

---

## 3. Vercel にデプロイする

1. [vercel.com](https://vercel.com) で **Add New → Project**、このリポジトリを選びます。
2. Framework Preset は **Other** のままで構いません。
   ビルド設定はリポジトリの `vercel.json` に書いてあるので、画面での指定は不要です。
3. **Environment Variables** に設定します（Production / Preview 両方）。

   | 変数 | 値 |
   |---|---|
   | `DATABASE_URL` | Supabase の **Transaction pooler**（ポート **6543**）の接続文字列。末尾に `?sslmode=require` |
   | `VITE_RASTER_TILES` | `https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png` |
   | `VITE_RASTER_ATTRIBUTION` | `国土地理院` |
   | `KAIGYOU_ANALYSIS_TOKEN` | 商圏インテリジェンスを使うときのみ。次節を参照 |

4. **Deploy** を押します。

> **PC を常駐させたくない場合** は `docs/saas.md` を参照してください。
> Vercel 上で分析を走らせ、利用者アカウントと月あたりの上限を管理する構成です。
> その場合は `ANTHROPIC_API_KEY` を Vercel に置きます（下の節とは逆になります）。

### `ANTHROPIC_API_KEY` は Vercel に置きません（worker を手元で回す場合）

**Vercel 側では LLM を一度も呼びません。** API がするのは Job をキューに積むことと
進捗を見せることだけで、4 ステップの実行と Web 検索はワークステーションの worker が
行います（要件 §31。Web 検索を伴う 4 ステップは関数の実行時間に収まりません）。

そのため `ANTHROPIC_API_KEY` は **worker を動かす端末の環境変数**です。Vercel に
置いても使われず、鍵を置く場所が 1 つ増えるだけです。

```powershell
# worker を動かす端末（ここにだけ API キーを置く）
$env:ANTHROPIC_API_KEY = 'sk-ant-...'
$env:DATABASE_URL = 'postgresql://...supabase.com:5432/postgres?sslmode=require'
python -m kaigyou_etl analyze --poll 5
```

worker の `DATABASE_URL` は **Session pooler（5432）** を使ってください。長く
つなぎっぱなしにするので、Transaction pooler（6543）より向いています。

### `KAIGYOU_ANALYSIS_TOKEN` は自分で決める合言葉

`ANTHROPIC_API_KEY` とは**別のもの**です。Anthropic のキーを入れないでください。

分析 1 件ごとに LLM の課金が発生するので、公開 URL に認証なしで置くと誰でも
財布を開けられます。ホスティング環境でこの変数が未設定なら、分析の開始は
**503 で断ります**（警告を出して通すのでは、気づいたときには請求が来ています）。

値は自分で作った十分長いランダム文字列にしてください。

```powershell
# 例：64文字のランダム文字列を作る
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

これを Vercel の `KAIGYOU_ANALYSIS_TOKEN` に設定し、同じ値を画面の
「分析トークン」欄に入れます。入れた値はその端末のブラウザにだけ保存されます。

地図と統計だけを公開して分析機能は自分だけが使う、という運用がこれでできます。

`vercel.json` はこのままで動きます。**rewrites は一つも書かないでください。**
Vercel は FastAPI アプリを認識して全リクエストをそのアプリに渡します。
`/api/(.*)` の rewrite を足すと API が全部404になり、
`/((?!api/).*)` → `/index.html` のような SPA フォールバックを足すと
`/assets/*.js` まで HTML が返って**画面が真っ白**になります。
どちらの振り分けも API 側（`server/kaigyou_api/main.py` の catch-all）が行います。
`api/index.py` の `app` の import も、トップレベルから動かさないでください
（Vercel はこのファイルを実行せずに解析して `app` を探します）。

`VITE_` で始まる変数は**ビルド時に埋め込まれます**。
あとから値を変えたときは、再デプロイしないと反映されません。

### 背景地図について

`VITE_RASTER_*` を設定しないと、背景が灰色一色のまま
歯科医院と駅の点だけが表示されます（動作はします）。
上の値は国土地理院の淡色地図で、APIキーもアカウントも要りません。
利用規約が出典表示を求めているため、`VITE_RASTER_ATTRIBUTION` の文字列が
地図右下に常時表示されるようにしてあります。**消さないでください。**

---

## 3.5 データを追加・更新したとき

新しい情報源を足した回（列やテーブルが増えた回）は、**Supabase 側にも同じ変更を適用**
してからでないと、その部分の API が動きません。手元と同じ2コマンドです。

```powershell
$env:DATABASE_URL = 'postgresql://postgres.<プロジェクトID>:<パスワード>@aws-N-<region>.pooler.supabase.com:5432/postgres?sslmode=require'

.\.venv\Scripts\kaigyou-etl migrate        # スキーマの差分を適用
.\.venv\Scripts\kaigyou-etl load-local download   # 新しいファイルを含めて投入
```

`migrate` は未適用のものだけを流します。何度実行しても安全です。
`load-local` は最後に**設定済みの全プロファイル**のスコアを計算します。

スコアだけ計算し直したいとき（`config/scoring.yaml` の重みを変えた場合など）は:

```powershell
.\.venv\Scripts\kaigyou-etl refresh-stats
.\.venv\Scripts\kaigyou-etl compute-scores --all-profiles
```

`--all-profiles` を付けないと**有効なプロファイル1つ分**しか計算されず、
他のプロファイルを選んだときにランキングが空になります。

**順序について。** GitHub に push すると Vercel は数十秒でデプロイしますが、
`migrate` は手元から実行するので必ず後になります。その間コードは
「まだ存在しないテーブル」を知っている状態になります。
この時間帯でも API 全体が落ちないよう、未作成のテーブルは
「未取得のデータセット」として扱う作りにしてあります
（`server/kaigyou_core/db.py` の `table_exists`）。
とはいえ**その情報は表示されない**ので、`migrate` は早めに実行してください。

---

## 4. 動いているか確かめる

デプロイ後のURLに対して：

| URL | 期待される結果 |
|---|---|
| `https://<your-app>.vercel.app/api/health` | 下記の診断JSON（DBに接続せず答えます） |
| `https://<your-app>.vercel.app/api/data-status` | 4つの情報源が `official` で並ぶ |
| `https://<your-app>.vercel.app/` | 地図が出て、赤い点が見える |

`/api/health` は、デプロイで間違えやすい3点をそのまま返します。
**接続はしない**ので、データベースが落ちていても答えます。
認証情報は含まれないため、そのまま貼り付けて構いません。

```json
{
  "status": "ok",
  "config_found": true,       // config/*.yaml が関数に同梱されているか
  "config_dir": "/var/task/config",
  "database_url_set": true,   // DATABASE_URL が設定されているか
  "database_pooled": true     // ← false なら Direct (5432) を使っている。6543 に直す
}
```

`/api/data-status` が空、あるいは「公的データ未取得」の赤い帯が出る場合は、
Vercel の `DATABASE_URL` が**データを入れたのとは別のデータベース**を指しています。

最後にスマホの実機で開いてください。ホーム画面に追加すると全画面で開きます。

---

## 5. 公開する前に読むところ

- **URLを知っていれば誰でも見られます。** 身内だけに見せたいなら、Vercel の
  Settings → Deployment Protection で Password Protection か Vercel Authentication
  を有効にしてください（無料枠でも使えます）。
- **免責表示と出典表示は外さないでください。** 画面上の
  「暫定モデル」バッジ、データソース一覧、注意事項は、この数値が
  何であって何でないかを示すためのものです。公開先ではより重要になります。
- **Supabase の無料プロジェクトは、7日間アクセスがないと一時停止します。**
  停止すると API は 500 を返します。ダッシュボードから再開できます。
- **エラーの詳細は公開先では出しません。** ローカルでは 500 応答に例外名を
  そのまま載せています（原因が分からないと直しようがないため）。
  Vercel 上では `サーバ内部エラー` とだけ返し、詳細は Vercel のログに送ります。
  一時的に戻したいときは環境変数 `KAIGYOU_ERROR_DETAIL=1` を設定してください。

---

## うまくいかないとき

| 症状 | 原因 | 対処 |
|---|---|---|
| ビルドが `does not define a top-level "app" FastAPI instance` で失敗 | `api/index.py` の `app` が**モジュール直下に無い** | Vercel はこのファイルを**実行せず、構文解析して** `app` を探します。`try/except` や関数の中に入れると見つかりません。`from kaigyou_api.main import app` は必ずトップレベルに置いてください（`server/tests/test_deployment.py` が検査します） |
| 同上。`[tool.vercel] entrypoint` を足しても直らない | `server/pyproject.toml` に書いている／モジュールパスが違う | Vercel が読むのは**リポジトリ直下**の `pyproject.toml` です。また `server` はパッケージではないので `server.kaigyou_api.main:app` は import できません。`api/index.py` を直せばこの設定自体が不要です |
| `/api/*` が全部 404 | `vercel.json` に `/api/(.*)` の rewrite がある | Vercel の FastAPI 対応は `/api/*` をアプリに直接ルーティングします。rewrite があると**転送先のパス**（`/api/index`）でルーティングされ、全部 404 になります。この rewrite は置かないでください |
| `500: FUNCTION_INVOCATION_FAILED` | 関数が**起動前に**落ちている。多くは依存関係が入っていない | `/api/health` を開くと、起動失敗なら 503 と一緒に原因のモジュール名が出ます。ビルド設定で `installCommand` を上書きすると `pip install -r requirements.txt` が走らなくなるので注意 |
| pgRouting を後から有効化した | 有効化前に `migrate` 済みでも問題ありません | もう一度 `kaigyou-etl migrate` を実行すれば、経路探索の関数が作られます（適用済みでも修復します） |
| ローカルで「徒歩圏」を選ぶとエラー／円のまま。`load-local` に「交差点で分割しています」が出ない | ローカルの PostgreSQL に pgRouting が入っていない（Supabase で有効化しても**ローカルには効きません**） | `kaigyou-etl doctor` を実行してください。pgRouting の行が「未インストール」なら Stack Builder 等で pgRouting を追加し、`create extension pgrouting;` → `kaigyou-etl migrate` → `load-local` の順にやり直します |
| 「徒歩圏」を選ぶと 500 になる | 経路探索の SQL が失敗している | `kaigyou-etl doctor` の「徒歩圏の算出」行に PostgreSQL のエラーがそのまま出ます。画面の 500 は原因を伝えないので、まずこちらを見てください |
| 「徒歩圏」を選んでも円のまま | 街路ネットワーク未投入、または pgRouting 未有効 | Supabase の SQL Editor で `create extension if not exists pgrouting;` を実行し、OSM 道路データを `load-local` で投入 |
| ランキングが空で「メッシュスコアが未計算です」 | そのプロファイルのスコアが無い | `kaigyou-etl compute-scores --all-profiles` |
| データを追加した直後だけ API がエラー | Supabase にマイグレーションが未適用 | `DATABASE_URL` を Supabase にして `kaigyou-etl migrate` → `load-local`。3.5 節を参照 |
| 静岡県を入れたら東京都のランキングが消えた | スコアの再計算が県で区切られていなかった（e68829b 以前）| `git pull` して `python -m kaigyou_etl compute-scores --all-profiles --prefecture 13` で東京都ぶんを計算し直してください。以降は県ごとに保持されます |
| `compute-scores` が `QueryCanceled: canceling statement due to statement timeout` で落ちる | 1文が長すぎてホスト側の statement timeout に当たっている | `git pull` してください。商圏の集計もエリア名の付与も1,000メッシュずつに分割し、セッションの statement_timeout も延長するようになります。「商圏を集計中」で止まるか「エリア名を付与中」で止まるかで、どちらの段階かが分かります |
| `compute-scores` が `kg_analyze_point` のエラーで落ちる（県が大きい）| 1文が長すぎてホスト側の statement timeout に当たっている | `git pull` で 1,000メッシュずつに分割して実行するようになります。進捗も表示されます |
| 画面に「読み込んでいます…」が残る | JavaScript の読み込みに失敗している（バンドルが 404、または HTML が返っている） | ブラウザの Network タブで `/assets/index-*.js` のステータスと Content-Type を確認してください。404 ならビルド出力が配信されていません。`/api/health` の `web_client_assets` に index.html が参照しているファイル名が含まれているかも確認してください（含まれていなければ index.html と bundle が別ビルドです）|
| 画面が真っ白（APIは動いている） | `vercel.json` の rewrite が `/assets/*.js` まで `/index.html` に転送している | **rewrite を置かないでください。** ブラウザのコンソールに `Expected a JavaScript-or-Wasm module script but the server responded with a MIME type of "text/html"` が出ます。SPA のフォールバックは API 側が行います |
| トップページが `{"detail":"Not Found"}` | 画面（静的ファイル）が配信されず、全リクエストが API に届いている | 現在は API 自身が画面を返します。`/api/health` の `web_client_bundled` が `false` なら `web/dist` が同梱されていません |
| `/api/health` が `config_found: false` | `config/*.yaml` が関数に同梱されていない | `vercel.json` の `functions."api/index.py".includeFiles` が `config/**` になっているか確認 |
| `/api/health` が `database_pooled: false` | Vercel で Direct connection (5432) を使っている | Transaction pooler (6543) の接続文字列に変更して再デプロイ |
| `prepared statement "_pg3_0" does not exist` | プーラ経由なのにプリペアドステートメントが有効 | 通常は自動判定されます。判定が外れる接続文字列なら `KAIGYOU_DB_PREPARE=off` を設定 |
| API が全部 500、`/api/health` は 200。地図に点が1つも出ない | `DATABASE_URL` が古い（Supabase でパスワードを変更したのに Vercel 側を更新していない、等）| `/api/health/db` を開いてください。`{"connected":false,"reason":"authentication"}` なら接続文字列です。Vercel の Settings → Environment Variables を更新し、**再デプロイ**します（環境変数の変更だけでは反映されません）|
| API が全部 500、`/api/health` は 200 | `DATABASE_URL` が未設定か誤り | `/api/health` の `database_url_set` を確認。設定後は**再デプロイが必要** |
| `remaining connection slots are reserved` | Direct connection (5432) を Vercel で使っている | Transaction pooler (6543) に変更 |
| 地図は出るが灰色一色 | `VITE_RASTER_TILES` 未設定 | 設定して**再デプロイ** |
| 画面上部に「サンプルデータ表示中」 | 開発用の合成データが残っている | `kaigyou-etl drop-sample` を Supabase 側の `DATABASE_URL` で実行 |
| ビルドが `Package metadata name 'kaigyou-support' does not match given name 'server'` で落ちる | `requirements.txt` に `./server` のようなローカルパスを書いた | **書かないでください。** Vercel の uv はディレクトリ名からパッケージ名を推測するため必ず不一致になります。`requirements.txt` は PyPI のパッケージだけにし、自前のコードは `vercel.json` の `includeFiles` で同梱します |
| `/api/health` が `routers_loaded: false` | `server/` が関数に同梱されていない | `vercel.json` の `includeFiles` が `{config,server}/**` になっているか確認 |
| 投入時に `getaddrinfo failed` / `failed to resolve host db.*.supabase.co` | Direct connection は IPv6 専用。回線が IPv4 のみ | **Session pooler**（`pooler.supabase.com` の 5432）に変更。ユーザ名も `postgres.<プロジェクトID>` に変わります |
| `tenant/user postgres.xxx not found` / `Tenant or user not found` | プーラには届いているが、ユーザ名のプロジェクトIDが一致しない | ダッシュボードの Session pooler の文字列を**そのままコピー**。綴り違い／リージョン不一致／プロジェクト一時停止のいずれかです |
| 認証だけ失敗する（ホストは引けている） | PowerShell の `"` でパスワードの `$` が変数展開された | 引用符を `'` に変える |
| データ投入が異常に遅い | Transaction pooler (6543) 経由で投入している | Session pooler (5432) に変更 |

手元の環境が原因かどうかは、まずローカルで切り分けられます。

```bash
.venv/bin/kaigyou-etl doctor
```

`DATABASE_URL` を Supabase のものにして実行すれば、
接続・PostGIS・マイグレーション・データの有無を順に見て、
最初に失敗したところと対処コマンドを表示します。
