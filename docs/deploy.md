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

### 接続文字列は2種類あり、用途が違います

Supabase の **Project Settings → Database → Connection string** に出てくるものです。
**この2つを取り違えると、動くけれど遅い／たまに落ちる、という分かりにくい壊れ方をします。**

| | ポート | 使う場面 | 理由 |
|---|---|---|---|
| **Direct connection** | 5432 | 手元PCからのデータ投入（ETL） | 接続を張りっぱなしにできる。大量INSERTが速い |
| **Transaction pooler** | 6543 | Vercel の関数から | リクエストごとに接続が生まれては消えるため、プールが要る |

Vercel 側でうっかり 5432 を使うと、アクセスが増えたところで接続数を使い切ります。
逆にプーラ経由では PostgreSQL のプリペアドステートメントが使えません
（PgBouncer がトランザクションごとに別のバックエンドを割り当てるため）。
接続文字列を見て自動で切り替えるようにしてあるので、設定はURLを貼るだけで済みます
（`server/kaigyou_core/db.py` の `is_pooled`）。

---

## 2. 手元のPCから Supabase にデータを入れる

ローカルの Postgres に入れたときと同じコマンドを、`DATABASE_URL` だけ変えて実行します。

Windows (PowerShell):

```powershell
$env:DATABASE_URL = "postgresql://postgres:<パスワード>@db.<プロジェクトID>.supabase.co:5432/postgres?sslmode=require"

.\.venv\Scripts\kaigyou-etl migrate
.\.venv\Scripts\kaigyou-etl load-local download
```

macOS / Linux:

```bash
export DATABASE_URL="postgresql://postgres:<パスワード>@db.<プロジェクトID>.supabase.co:5432/postgres?sslmode=require"

.venv/bin/kaigyou-etl migrate
.venv/bin/kaigyou-etl load-local download
```

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
3. **Environment Variables** に3つ設定します（Production / Preview 両方）。

   | 変数 | 値 |
   |---|---|
   | `DATABASE_URL` | Supabase の **Transaction pooler**（ポート **6543**）の接続文字列。末尾に `?sslmode=require` |
   | `VITE_RASTER_TILES` | `https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png` |
   | `VITE_RASTER_ATTRIBUTION` | `国土地理院` |

4. **Deploy** を押します。

`VITE_` で始まる変数は**ビルド時に埋め込まれます**。
あとから値を変えたときは、再デプロイしないと反映されません。

### 背景地図について

`VITE_RASTER_*` を設定しないと、背景が灰色一色のまま
歯科医院と駅の点だけが表示されます（動作はします）。
上の値は国土地理院の淡色地図で、APIキーもアカウントも要りません。
利用規約が出典表示を求めているため、`VITE_RASTER_ATTRIBUTION` の文字列が
地図右下に常時表示されるようにしてあります。**消さないでください。**

---

## 4. 動いているか確かめる

デプロイ後のURLに対して：

| URL | 期待される結果 |
|---|---|
| `https://<your-app>.vercel.app/api/health` | `{"status":"ok"}`（DBを見ない生存確認） |
| `https://<your-app>.vercel.app/api/data-status` | 4つの情報源が `official` で並ぶ |
| `https://<your-app>.vercel.app/` | 地図が出て、赤い点が見える |

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
| `prepared statement "_pg3_0" does not exist` | プーラ経由なのにプリペアドステートメントが有効 | 通常は自動判定されます。判定が外れる接続文字列なら `KAIGYOU_DB_PREPARE=off` を設定 |
| API が全部 500、`/api/health` は OK | `DATABASE_URL` が未設定か誤り | Vercel の環境変数を確認。設定後は**再デプロイが必要** |
| `remaining connection slots are reserved` | Direct connection (5432) を Vercel で使っている | Transaction pooler (6543) に変更 |
| 地図は出るが灰色一色 | `VITE_RASTER_TILES` 未設定 | 設定して**再デプロイ** |
| 画面上部に「サンプルデータ表示中」 | 開発用の合成データが残っている | `kaigyou-etl drop-sample` を Supabase 側の `DATABASE_URL` で実行 |
| ビルドが `No module named kaigyou_api` で落ちる | `requirements.txt` の `./server` が入っていない | リポジトリ直下の `requirements.txt` を確認 |
| データ投入が異常に遅い | プーラ (6543) 経由で投入している | Direct connection (5432) に変更 |

手元の環境が原因かどうかは、まずローカルで切り分けられます。

```bash
.venv/bin/kaigyou-etl doctor
```

`DATABASE_URL` を Supabase のものにして実行すれば、
接続・PostGIS・マイグレーション・データの有無を順に見て、
最初に失敗したところと対処コマンドを表示します。
