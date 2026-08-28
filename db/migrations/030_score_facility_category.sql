-- スコアの鍵に業態を入れる。
--
-- **これはラベルの修正ではなく、間違った答えを出す構造の修正です。**
--
-- これまで mesh_scores の主キーは (mesh_id, profile, radius_m) で、
-- metric_distributions の scope は 'mesh:500:r1000:pref13:with_clinics' でした。
-- どちらにも業態が入っていません。歯科しか扱っていない間は問題になりません
-- でしたが、内科でスコアを流すと**同じ鍵に別の業態の点が入り、片方が消えます。**
-- しかも compute-scores は成功と表示します。
--
-- scope の 'with_clinics' は「歯科医院が実在する商圏」という意味です。目盛りの
-- 定義そのものが業態に依存していて、内科では別の集合になります。同じ文字列で
-- 二つの意味を持たせることはできません。
--
-- ---------------------------------------------------------------------------
-- **歯科版を止めないこと。** これがこの移行のいちばん大事な条件です。
--
-- 歯科版は商談で使われています。compute-scores は東京・静岡で数十分かかるので、
-- 「移行したら再計算してください」では、その間ランキングもスコアも出ません。
--
-- なので既存行は消さずに埋めます。列は 'dental_clinic' を既定値にして足し、
-- scope は文字列をその場で書き換えます。**このマイグレーションを当てた直後から、
-- refresh-stats も compute-scores も再実行せずに今日どおり動きます。**
-- ---------------------------------------------------------------------------

-- ------------------------------------------------------------- mesh_scores
-- 既定値つきで足すので、既存行は自動的に 'dental_clinic' になります。
ALTER TABLE mesh_scores
    ADD COLUMN IF NOT EXISTS facility_category text NOT NULL DEFAULT 'dental_clinic';

-- 主キーを張り直します。**列を足しただけでは足りません。** 主キーに入って
-- いなければ、内科の行が歯科の行を ON CONFLICT で上書きします。
ALTER TABLE mesh_scores DROP CONSTRAINT IF EXISTS mesh_scores_pkey;
ALTER TABLE mesh_scores
    ADD PRIMARY KEY (mesh_id, profile, radius_m, facility_category);

-- ランキングの索引も業態で分けます。分けないと、内科を入れたあとに歯科の
-- ランキングを引くと全業態が混ざった順位が返ります。
DROP INDEX IF EXISTS mesh_scores_overall_idx;
CREATE INDEX IF NOT EXISTS mesh_scores_overall_idx
    ON mesh_scores (facility_category, profile, radius_m, overall_score DESC);

-- --------------------------------------------------------- metric_distributions
-- scope は文字列なので、列を足すのではなく中身を書き換えます。
--
--   旧  mesh:500:r1000:pref13:with_clinics
--   新  mesh:500:r1000:pref13:catdental_clinic:with_clinics
--
-- 条件は「区切りがちょうど5つ（＝まだ業態が入っていない）」です。二度当てても
-- 二重に入りません（移行後は6つになり、この条件に当たらなくなります）。
UPDATE metric_distributions
SET scope = regexp_replace(
        scope,
        '^(mesh:[0-9]+:r[0-9]+:pref[0-9]+):([a-z_]+)$',
        '\1:catdental_clinic:\2')
WHERE scope ~ '^mesh:[0-9]+:r[0-9]+:pref[0-9]+:[a-z_]+$';
