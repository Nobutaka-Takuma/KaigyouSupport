# 医科（病院・診療所・助産所）への拡張：リファクタリング計画

歯科版は商談で使われています。**この計画のどの段階でも、歯科版が動かなくなる
瞬間があってはいけません。** それが他のすべての判断より優先します。

具体的には次の4つを守ります。

1. **マイグレーションは既存行を埋める。** 列を足したら `dental_clinic` で
   backfill します。「入れ直してください」で済ませない
2. **再計算を必須にしない。** `compute-scores` は東京・静岡で数十分かかります。
   マイグレーション直後にスコアが消える設計にしないこと
3. **既存の URL・API・レポートの形を壊さない。** 既定値を省略したときの
   ふるまいは、今日と同じであること
4. **新しいコードは、古いスキーマでも動く。** ← 下の「移行の作法」

---

## 移行の作法（一度これで壊しました）

**コードは push で即デプロイされますが、マイグレーションは手で当てます。**
順序は選べません。だから「マイグレーションを当ててからデプロイする」という
運用は成り立たず、**新しいコードが古いスキーマでも動く**必要があります。

実際に静岡で壊しました。030 をデプロイし、Supabase に `migrate` を当てる前の
状態で:

- 需要と競合が「データ不足」 … 新しい鍵（`…:catdental_clinic:…`）しか探さず、
  DB にある古い鍵を見なかった
- ランキングとヒートマップが 500 … まだ無い列（`ms.facility_category`）を
  SELECT した
- 成長とアクセスだけ点が出ていた … この 2 つは目盛りを使わない絶対値だから

**歯科版が商談で使われている最中に、医科の準備で歯科が落ちた**わけです。
まさに避けたかったことでした。

### 規則

| | 読む側（API・レポート） | 書く側（ETL） |
|---|---|---|
| 新しい列が無い | **絞らずに読む。** 当時はどれも歯科なので、絞らないのが正しい答え | **止める。** 業態を記録しない行を書くと、あとから区別できない |
| 新しい鍵が無い | **古い鍵も探す**（既定の業態のときだけ） | — |

読む側で正しいことと、書く側で正しいことは**逆です。** 読む側が止まると
画面が落ちます。書く側が黙って続けると、区別のつかない行がデータに残ります。

### 手順（expand → migrate → contract）

1. **expand** … 新旧どちらの形でも読めるコードを出す。この段階でデプロイして
   よい。スキーマは古いままでよい
2. **migrate** … `kaigyou-etl migrate` を当てる。**ローカルと Supabase の
   両方。** 片方だけだと、その片方が上の窓に入り続けます
3. **contract** … 旧形式を読む道を消す。**当てたことを確認してから**

いまは 1 と 2 の間です。`legacy_scope_key` と `column_exists` の分岐は
**3 で消すもの**で、恒久的な仕組みではありません。

### 見張り

- `test_the_reader_still_finds_scales_written_before_the_migration`
- `test_no_reader_selects_a_column_that_a_migration_has_not_added_yet`
- `test_the_writer_refuses_instead_of_writing_rows_without_a_business_type`

---

## いまの構造：どこまで業態非依存にできているか

### A. 完全に共通（約6割）

土台はすでに業態を引数で受けています。ここは手を入れません。

| 領域 | ファイル |
|---|---|
| 空間・メッシュ基盤 | `kaigyou_core/mesh.py` `db.py` `config.py` `provenance.py` `status.py` |
| 人口・経済・交通・地価の ETL | `adapters/estat_*` `mlit_*` `osm_*`（15本中10本） |
| DB スキーマ | 001-002, 006-014, 016, 024-029 |
| LLM パイプライン機構 | `kaigyou_intel/client.py` `jobs.py` `worker.py` `failures.py` `pricing.py` `steps/*.py` |
| 射影・検算 | `projection.py`（`_citable` の一部を除く） |
| 画面基盤 | `lib/api.ts`、地図描画、`ReportsPage` `AdminPage` |

`facilities.facility_category` も `kg_analyze_point(p_facility_category)` も
最初から引数です。設計が効いています。

### B. 引数は通っているが、既定値が歯科

```
kaigyou_core/analysis.py:25          DEFAULT_CATEGORY = "dental_clinic"
db/migrations/005,011,014_*.sql      p_facility_category text DEFAULT 'dental_clinic'
db/migrations/017_analysis_jobs.sql  business_type text NOT NULL DEFAULT 'dental_clinic'
kaigyou_api/routers/*.py             Query(DEFAULT_CATEGORY)
kaigyou_etl/doctor.py:281,421        'dental_clinic' 直書き
```

既定値なので、歯科の呼び出しは何も指定せずに今日どおり動きます。

### C. スコアリングの鍵に業態が入っていない ← **性質が違う**

```sql
mesh_scores           PRIMARY KEY (mesh_id, profile, radius_m)      -- category なし
metric_distributions  scope = 'mesh:500:r1000:pref13:with_clinics'  -- category なし
```

`with_clinics`（＝「歯科医院が実在する商圏」）という**目盛りの定義そのものが
歯科依存**です。内科でスコアを流すと、同じ主キーに別業態の点が入って片方が
消えます。**しかも成功と表示されます。**

ラベルの間違いではなく答えの間違いなので、最初に潰します。

### D. 歯科の語彙が Python に固定されている

- **`kaigyou_core/specialties.py`（149行）** — `SOURCE_ID =
  "mhlw_dental_specialties"` が固定。コード表は YAML にあるのに `LABELS` /
  `ORDER` だけ Python の dict という**割れた状態**
- **`measures.py`** — キー名 `dental_clinics` / `population_per_clinic` /
  `clinics_per_10k` と label・definition。キー名は DB・API・UI まで流れる
- **`report.py`** 3箇所、**`web/types.ts` `ScorePanel.tsx`** のフィールド名と行ラベル

### E. 歯科の業態知識（設定・プロンプト、約1,800行）

```
config/hypotheses.yaml     259行  KSF・要件チェックリスト（ユニット台数、衛生士、リコール）
config/insights.yaml       185行  複合指標の問い
config/scoring.yaml        367行  5プロファイル（pediatric / orthodontics / office / cost_aware）
config/prompts/*.md      1,208行  5本すべてに歯科の判断が埋まっている
config/sources.yaml    2ブロック  mhlw_dental_clinics / mhlw_dental_specialties
```

**ここは共通化しません。** 「内科の開業で何を答えるべきか」は歯科とは別の
知識で、無理に抽象化すると両方に効かない枠になります。やるのは**置き場所を
分けること**だけです（`config/dental/` `config/medical/`）。

---

## F. 医科にあって歯科に無い概念

分類の外側ですが、設計に効くので先に挙げます。**6 と 7 を決めないと、あとで
全部やり直しになります。**

1. **病床** — 病院は20床以上。病床数・病床機能（高度急性期/急性期/回復期/
   慢性期）を持つ列が `facilities` にない
2. **基準病床数・病床過剰地域** — 二次医療圏単位の行政上の制約。**商圏が
   どれだけ良くても病床を増やせない**ことがある。スコアの外側にある拒否条件で、
   歯科に対応物が無い
3. **二次医療圏** — 市区町村でも都道府県でもない地理単位。境界データの
   取り込みが別途要る
4. **標榜科目の階層と数** — 歯科は5科目＋自由記載。医科は数十科目あり、
   内科系/外科系の階層と重複標榜が常態。平坦な `specialty_key` では持たない
5. **科目ごとに商圏の広さが違う** — 内科は徒歩圏、産婦人科・小児科は二次
   医療圏規模、眼科・皮膚科はその中間。**半径固定の商圏モデルそのものが科目で
   変わる。** scoring profile より深い層
6. **性別が要る** — 産婦人科の需要側は15〜49歳女性。`population_mesh` は
   `age_0_14 / 15_64 / 65_plus` の男女計だけで、**性別が入っていない。**
   国勢調査メッシュには男女別があるので取り込みの拡張で足せる
7. **病院は競合ではなく連携先** — 診療所にとって近隣の総合病院は紹介・逆紹介の
   相手。同じ `facilities` に入るが `competition` に数えてはいけない。
   **役割の区別**が要る
8. **助産所** — 全国で件数が少なく「商圏内0件」が常態。`zero_facility_score:
   95`（競合ゼロ＝好条件）という歯科の前提が、そのままでは成り立たない

---

## 進める順序

進捗: **1 済** / **2 済** / **3 済** / **4 済** / 5 未

### 1. スコア鍵に業態を入れる（C）— 唯一、間違った答えが出る箇所 ✔ 済（030）

`scope_key()` と `mesh_scores` の主キーに `facility_category` を足します。

**既存データを消しません。** マイグレーションで:

- `mesh_scores` に列を足し、既存行を `'dental_clinic'` で埋めてから主キーを張り直す
- `metric_distributions.scope` の既存文字列を **その場で書き換える**
  （`mesh:500:r1000:pref13:with_clinics` →
  `mesh:500:r1000:pref13:catdental_clinic:with_clinics`）

こうすれば `compute-scores` も `refresh-stats` も**再実行不要**で、
マイグレーション直後から歯科版は今日どおり動きます。書き換えた鍵が今の
コードの作る鍵と一致することは、`test_the_migrated_scope_matches_what_the_code_now_builds`
が SQL を読まずに確かめます。ここが 1 文字でもずれると、移行直後に目盛りが
見つからなくなり、数十分の再計算が終わるまでスコアもランキングも出ません。

ついでに `drop-prefecture` の目盛り削除を直しました。`LIKE '%pref13'` に
なっていて、鍵は `pref13` で終わらないので、**これまで 1 件も消していません
でした**（残った目盛りが、入れ直した別の県のデータに使われます）。

### 2. 既定値を直す（B）✔ 済

既定値は `dental_clinic` のままにし、**引数で上書きできる**ことだけを
保証します。歯科の呼び出しは何も変わりません。

やってみると、直書きの置き換えより**口が無いこと**のほうが本体でした。

- `refresh-stats` `compute-scores` `new-analysis` に `--category` がありません
  でした。医科を入れても「歯科として」採点され、**しかも成功と表示します**
- `load-local` は既定の業態だけを採点していました。医科のファイルを入れたのに
  ランキングが空、という状態になります。取り込まれている業態すべてを回すように
  しました（歯科しか入っていない環境では今までどおり 1 業態です）
- 施設が 1 件も無い県では、空を返して採点を飛ばすのではなく既定を 1 つ返します。
  飛ばすと、原因の分からない「空のランキング」になります

直書きは `scoring.DEFAULT_FACILITY_CATEGORY` の 1 か所に集めました。増えて
いないことは `test_the_business_type_is_defined_in_one_place` が見張ります
（SQL の既定値と合成データの生成だけ除外）。

### 3. 語彙を設定へ出す（D）✔ 済

`LABELS` / `ORDER` / `HOURS_LABELS` を `config/sources.yaml` の
`specialty_codes` の隣へ移しました。**内容は 1 文字も変えていません**
（移す前の Python の dict と一致することをテストが持っています）。

**どの語彙かは業態が決めます。** `SOURCE_ID` 固定をやめ、`specialty_codes` を
持つソースの `facility_category` で引きます。見つからないときは**空を返し、
他業態の語彙で代用しません。** 歯科の科目名で内科を分類したものは、
間違っていてもそれらしく見えます。

取り込み側（`mhlw_specialties`）も、そのファイル自身の業態で語彙を引くように
しました。既定（歯科）で固定したままだと、医科のファイルを歯科の科目表で
分類してほぼ全部が「その他の標榜科」に落ち、**しかも取り込みは成功と
表示します。**

`measures.py` のキー名（`dental_clinics` など）と `web/types.ts` のフィールド名は
**まだ歯科の語です。** ここは DB・API・UI を横断して名前が流れているので、
5 段目（医科のデータモデル）で医科の指標を足すときに、両方の名前を持てる形に
します。今それだけを改名しても、歯科版の API の形が変わるだけで得るものが
ありません。

### 4. 設定の置き場所を分ける（E）✔ 済

**移動だけで、中身は 1 行も変えていません。**

```
config/sources.yaml       どの業態でも同じ（人口・事業所・駅・地価の出どころ）
config/analysis.yaml      どの業態でも同じ（モデル・上限・段の構成）
config/dental_clinic/     この業態の知識（scoring / insights / hypotheses / prompts）
```

フォルダ名は **`facility_category` そのもの**にしました（計画では
`config/dental/` と書いていましたが、対応表を持つと業態を足すたびにそこを
直すことになり、忘れると設定が黙って読まれません）。

`sources.yaml` は**分けません。** 人口メッシュも駅も地価も業態で変わらず、
分けると同じ国勢調査の定義を業態の数だけ複製することになります。片方だけ
直したときに「同じ商圏なのに業態で人口が違う」が起きます。業態ごとの施設
ファイルは、ソースごとの `facility_category` で区別できています。

業態フォルダに無いファイルは `config/` 直下に落ちます。設定を移していない
環境（手元のコピー、`KAIGYOU_CONFIG_DIR` を差し替えた検証環境）を、この
変更でその場で壊さないためです。**移行のためのもの**で、恒久的な仕組みでは
ありません。

STEP1〜4 の実行関数が**業態を受け取る**ようになりました。ジョブは
`business_type` を持っているので、worker がそれを渡します。渡さないと、
医科のジョブが歯科のプロンプトと KSF で書かれます——**しかも成功と
表示されます。** 引数の名前をテストで見張っています。

`analysis.yaml` の `prompt_version` は業態をまたいで共通の文字列です。
同じ版番号で中身の違うプロンプトが 2 つ存在しうるので、医科の
プロンプトを書くときに版の付け方を決めてください。

### 5. 医科のデータモデル（F）

ここで初めて医科固有のものを足します。着手前に 6（性別）と 7（連携先/競合の
区別）を決めること。

---

## 各段階の完了条件

**どの段階も、次を満たすまで完了としません。**

- `pytest server/tests` が既知の失敗（サンドボックスのサンプルデータ由来の
  11件）以外で緑
- 歯科の地点で `analyze` を1本通し、**前と同じレポートが出る**
- `/api/candidate-analysis` を業態指定なしで叩き、**前と同じ JSON が返る**
- マイグレーション適用後、`compute-scores` を再実行せずにランキング画面が出る
