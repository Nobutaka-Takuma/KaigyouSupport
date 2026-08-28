# 医科（病院・診療所・助産所）への拡張：リファクタリング計画

歯科版は商談で使われています。**この計画のどの段階でも、歯科版が動かなくなる
瞬間があってはいけません。** それが他のすべての判断より優先します。

具体的には次の3つを守ります。

1. **マイグレーションは既存行を埋める。** 列を足したら `dental_clinic` で
   backfill します。「入れ直してください」で済ませない
2. **再計算を必須にしない。** `compute-scores` は東京・静岡で数十分かかります。
   マイグレーション直後にスコアが消える設計にしないこと
3. **既存の URL・API・レポートの形を壊さない。** 既定値を省略したときの
   ふるまいは、今日と同じであること

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

### 1. スコア鍵に業態を入れる（C）— 唯一、間違った答えが出る箇所

`scope_key()` と `mesh_scores` の主キーに `facility_category` を足します。

**既存データを消しません。** マイグレーションで:

- `mesh_scores` に列を足し、既存行を `'dental_clinic'` で埋めてから主キーを張り直す
- `metric_distributions.scope` の既存文字列を **その場で書き換える**
  （`mesh:500:r1000:pref13:with_clinics` →
  `mesh:500:r1000:pref13:dental_clinic:with_clinics`）

こうすれば `compute-scores` も `refresh-stats` も**再実行不要**で、
マイグレーション直後から歯科版は今日どおり動きます。

### 2. 既定値を直す（B）

10箇所程度。既定値は `dental_clinic` のままにし、**引数で上書きできる**
ことだけを保証します。歯科の呼び出しは何も変わりません。

### 3. 語彙を設定へ出す（D）

`specialties.py` の `LABELS` / `ORDER` / `SOURCE_ID` を業態ごとの設定に移します。
`config/sources.yaml` の `specialty_codes` の隣が自然な置き場所です。
**歯科の語彙は同じ内容のまま移すだけ**で、出力は1文字も変わらないこと。

### 4. 設定の置き場所を分ける（E）

`config/dental/` に今のファイルを移し、業態で読み分けます。
**移動だけで内容は変えません。** 読み込み側に業態が無いときは `dental` に
落ちるようにして、既存の呼び出しを壊さないこと。

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
