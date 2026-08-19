# KaigyouSupport — 歯科開業候補地分析 MVP

東京都内の任意地点について、周辺の**人口・年齢構成・人口動態・歯科医院の分布・駅アクセス**を
公的データから集計し、地図上で確認・比較するツールです。

> **中心機能は「地図上の任意地点を指定 → 商圏を自動分析」です。**
> ランキングは同じ分析エンジンをメッシュ全体に適用した副次機能として実装しています。

開業成功確率・売上・患者数・家賃の予測は行いません。

---

## データ取得状況

| 情報源 | 結果 | 備考 |
|---|---|---|
| 厚労省 医療機能情報提供制度（歯科診療所） | ✅ **取り込み済 51,384件** | 手動ダウンロードしたCSVを `--input` で投入。全47都道府県（東京都 8,438件） |
| e-Stat 統計GIS（国勢調査メッシュ人口） | ❌ 未取得 | `network_blocked` — 実行環境のネットワークポリシーにより `www.e-stat.go.jp` へ到達不可（プロキシが403） |
| 国土数値情報 S12（駅別乗降客数） | ❌ 未取得 | `network_blocked` — `nlftp.mlit.go.jp` へ到達不可 |
| 国土数値情報 N03（行政区域） | ❌ 未取得 | `network_blocked` — `nlftp.mlit.go.jp` へ到達不可 |

取得できなかったデータについて、**架空の値を実データとして投入することは一切していません。**
失敗内容は `acquisition_runs` に記録され、`kaigyou-etl status` / `/about` 画面 /
`GET /api/data-status` で常に確認できます。

### 未取得分は合成データで代替している（実データではない）

人口メッシュ・駅は動作確認のため**合成（サンプル）データ**を使っています。
実データではなく、実データを装うこともしません。

- 情報源名は `【サンプル】` で始まり、`data_sources.dataset_kind = 'sample'`
- API は `provenance.contains_sample_data: true` を返す
- **全画面の上部に警告バナーが常時表示される**（非表示にできません）

### ⚠️ 実データと合成データの混在について

現在のデータベースは **実データ（歯科医院）と合成データ（人口メッシュ・駅）が混在**しています。
このため以下の数値は**意味を持ちません**:

- 人口 / 歯科医院数（実医院数 ÷ 合成人口）
- 需要スコア・成長スコア・アクセススコア・総合スコア

一方、以下は**実データのみに基づくため有効**です:

- 商圏内の歯科医院数（500m / 1km / 2km）
- 最寄り歯科医院とその距離
- 地図上の歯科医院分布

この区別は API の `warnings` と分析パネルに明示されます。
また、同一テーブルに実データと合成データが同時に存在する場合（二重計上になる状態）は、
`kaigyou-etl status` と全画面バナーで検知・警告します。

**人口メッシュを実データに差し替えれば、全指標が有効になります。**

## 構成

```
External Data → ETL（download/validate/transform/load）→ PostGIS → API → Web UI
                 ↑ ネットワークに触れるのはここだけ
```

| ディレクトリ | 役割 |
|---|---|
| `config/` | データソース定義（`sources.yaml`）、スコアリング設定（`scoring.yaml`） |
| `db/migrations/` | PostGIS スキーマと分析関数（SQL） |
| `server/kaigyou_core/` | 設定・DB・メッシュ計算・スコアリング・出典管理（ETL と API の共通部品） |
| `server/kaigyou_etl/` | データ取得。**外部通信を行う唯一のコンポーネント** |
| `server/kaigyou_api/` | 読み取り専用 HTTP API。外部データを取得しない |
| `web/` | MapLibre GL JS + React のフロントエンド。API 以外と通信しない |
| `server/tests/` | 単体テスト＋PostGIS 結合テスト |

UI・API・ETL は別プロセスであり、UI からデータ取得処理を起動する経路はありません。

---

## セットアップ

必要なもの: Python 3.11+ / Node.js 20+ / PostgreSQL 16 + PostGIS 3（Docker 可）

```bash
# 1. 依存関係
make setup

# 2. データベース（Docker を使う場合）
make db
export DATABASE_URL=postgresql://kaigyou:kaigyou@127.0.0.1:5432/kaigyou

# 3. スキーマ適用
make migrate

# 4. 公的データの取得を試行
make fetch          # 取得できない情報源があると exit code 2
make status         # 取得できたもの／できなかったものと理由を表示

# 5. 取得できなかった場合、動作確認用の合成データを投入（任意）
make sample

# 6. スコア基準の算出とメッシュスコアの計算
make stats
make scores

# 7. 起動
make api            # http://127.0.0.1:8000  （API ドキュメント /docs）
make web            # http://127.0.0.1:5173
```

### 背景地図タイル

地図タイルは同梱していません。`web/.env` に以下のいずれかを設定してください
（未設定でも、自前のデータレイヤーのみで地図は動作します）。

```
VITE_BASEMAP_STYLE=https://example.com/style.json
# または
VITE_RASTER_TILES=https://tile.openstreetmap.org/{z}/{x}/{y}.png
VITE_RASTER_ATTRIBUTION=© OpenStreetMap contributors
```

タイル提供元の利用規約を必ず確認してください。

---

## 実データの取り込み

### 手動ダウンロードしたファイルを使う（推奨）

いくつかの情報源はフォーム経由の配布で、安定した直リンクがありません。
その場合は手元にダウンロードしたファイルを渡してください。

```bash
# 歯科診療所（実績あり）。全47都道府県を含むファイルをそのまま投入できます
kaigyou-etl run mhlw_dental_clinics --input 031_dental_facility_info_20260601.csv

# 東京都だけに絞る場合
kaigyou-etl run mhlw_dental_clinics --input <file>.csv --prefecture 13

kaigyou-etl run estat_population_mesh --input ~/Downloads/tblT001102C13.zip
kaigyou-etl run mlit_stations --input ~/Downloads/S12-23_GML.zip
```

`--offline` を付けると、ネットワークに一切アクセスしません。

### 自動ダウンロード

`config/sources.yaml` に `url` が設定されている情報源は自動取得できます。

```bash
kaigyou-etl run mlit_stations
kaigyou-etl run-all
```

歯科診療所は配布ページがフォーム経由のため `url` を空にしてあります
（誤ったURLを設定すると、設定漏れが「サーバ障害」に見えてしまうため）。

### どのファイルが必要か

**歯科医院（取り込み実績あり）**
厚労省「医療機能情報提供制度」の歯科施設情報 CSV
（`0xx_dental_facility_info_YYYYMMDD.csv`、UTF-8 BOM付き、57列）。
ID / 正式名称 / 所在地 / 都道府県コード / 市区町村コード / 緯度 / 経度 を使用します。

> 実ファイルでは約6%（54,637件中3,253件）の座標が `0,0` です。
> これらは投入せず破棄し、件数を `acquisition_runs` に記録します
> （住所からのジオコーディングは行いません）。東京都の欠損は0.4%のみです。

**診療科目（未取得・任意）**
上記ファイルには標榜診療科が含まれません。同じ制度の別ファイル（診療科目情報）を
取り込めば `facilities.clinic_types` が埋まり、小児歯科・矯正歯科などでの
絞り込みが有効になります。`config/sources.yaml` の `columns.clinic_types` に
実際の列名を追加してください。

**人口メッシュ（最重要・未取得）**
e-Stat 統計GIS の国勢調査メッシュ統計（東京都、1kmメッシュ）。
これを入れると需要・成長・総合スコアが実データに基づくようになります。
人口増減率には2時点（例: 2020年と2015年）が必要です。

**駅（未取得）**
国土数値情報 S12（駅別乗降客数）の zip。アクセススコアに使用します。

### 取り込み後

```bash
kaigyou-etl drop-sample     # 合成データを削除
kaigyou-etl refresh-stats   # スコア基準を実データの分布で再計算
kaigyou-etl compute-scores  # ランキング・ヒートマップ用スコアを再計算
kaigyou-etl status
```

### 配布形式が変わったら

列名の変更は `config/sources.yaml` の `columns` に候補を足すだけで対応できます
（コード変更不要）。URL の変更も同ファイルの `url` を書き換えるだけです。
配布形式そのものが変わった場合は、`server/kaigyou_etl/adapters/` に
アダプタを1つ追加し、`adapters/__init__.py` の `ADAPTERS` に登録します。

> **注記:** 各アダプタの変換処理は、公開されているスキーマ定義に基づいて実装し、
> 実際の配布形式を模したフィクスチャで単体テストしていますが、
> **本番の配布ファイルに対する検証は未実施です**（この環境では取得できないため）。
> 最初の実データ投入時は `kaigyou-etl run ... ` の validate ステップの出力
> （検出された列名・件数）を必ず確認してください。

---

## スコアリング

4指標（Demand / Competition / Growth / Accessibility）と総合スコアを算出します。

**重みはコードに埋め込まれていません。** すべて `config/scoring.yaml` にあり、
API はファイルの更新時刻を見て自動で再読み込みします。再起動は不要です。

```yaml
profiles:
  default:
    overall_weights:
      demand: 0.35
      competition: 0.30
      growth: 0.20
      accessibility: 0.15
```

複数のプロファイルを定義でき、リクエスト単位で切り替えられます
（`?profile=pediatric`）。UI のプルダウンにも自動で現れます。

**重みは仮説値です。** 開業実績による較正は行っていないため、
UI では常に「暫定モデル」と表示されます。

### 欠損データの扱い

算出に必要な指標が欠けている場合、**0 とはみなさず「算出不可」として扱います**。

- 部分的に欠けている場合は、残った指標で重みを再正規化
- ただし `min_weight_coverage`（既定 0.5）を下回る場合はスコア自体を出しません
  — 15% の重みしかない1指標だけで「需要スコア」を名乗らせないためです
- 算出できなかった指標は API レスポンスの `unavailable_components` と
  `breakdown[].missing` で明示され、UI にも表示されます

---

## GIS 処理

空間処理はすべて PostGIS 側にあります（`db/migrations/005_functions.sql`）。

- **商圏**: `ST_Buffer(point::geography, radius)` — 度ではなくメートルの真円
- **商圏人口**: メッシュポリゴンと商圏円の交差面積で按分
  （`ST_Area(ST_Intersection(...)::geography) / ST_Area(mesh::geography)`）
- **競合数**: `ST_DWithin(geography)` — GIST インデックス使用
- **最寄り施設／駅**: geography の KNN 演算子 `<->`
- **人口増減率**: 商圏内メッシュの人口加重平均

メッシュサイズは固定値ではありません。`population_mesh.mesh_size_m` に行ごとに保持し、
メッシュコードの桁数（8桁=1km / 9桁=500m / 10桁=250m）から自動判定します。

---

## 将来拡張のためのデータモデル

| 拡張 | 対応 |
|---|---|
| 東京都以外の都道府県 | 全空間テーブルに `prefecture_code`。API・ETL もパラメータ化済み |
| 歯科以外の医療施設 | `facilities.facility_category`（`dental_clinic` は既定値の一つ）。`dental_clinics` は互換用ビュー |
| 診療タイプ別分析 | `facilities.clinic_types text[]` に標榜診療科。GIN インデックス付き。API は `?clinic_type=` で絞り込み可 |
| 500m / 250m メッシュ | `population_mesh.mesh_size_m` |
| 施設固有の属性 | `facilities.attributes jsonb` |

---

## API

| エンドポイント | 内容 |
|---|---|
| `GET /api/candidate-analysis?lat=&lng=&radius=` | 任意地点の商圏分析（中心機能） |
| `GET /api/rankings` | メッシュ単位ランキング |
| `GET /api/compare?points=lat,lng;lat,lng` | 最大3地点の比較 |
| `GET /api/clinics` / `stations` / `meshes` / `municipalities` | GeoJSON レイヤー |
| `GET /api/data-status` | **取得できたデータ／できなかったデータと原因** |
| `GET /api/meta` | スコアリングモデル定義・免責事項 |

すべての分析レスポンスに `provenance`（出典・データ時点・取込日時・サンプル判定）、
`disclaimer`、`score_disclaimer` が含まれます。

---

## テスト

```bash
make test
```

`DATABASE_URL` が到達可能なら PostGIS 結合テスト（面積按分・距離・最寄り探索の
数値検証）も実行され、到達できない場合はスキップされます。

---

## 免責事項

> 本サービスの分析結果は、公開統計およびオープンデータを基にした参考情報です。
>
> 特定地域における歯科医院の開業成功、収益性、患者数等を保証するものではありません。
>
> 各データには取得時点があります。

データの利用にあたっては、各提供元の利用規約に従ってください。
出典・ライセンス・取得日は `/about` 画面で確認できます。
