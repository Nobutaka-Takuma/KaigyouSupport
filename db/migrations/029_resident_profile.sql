-- 500m メッシュ別の「そこに住んでいる人の性格」（国勢調査 就業状態等基本集計）。
--
-- **昼間人口ではありません。** この集計は常住地基準です。配布物の表題は
-- 「人口移動、就業状態等及び従業地・通学地」ですが、これは3つの集計をまとめた
-- ひとくくりの名前で、T001108 はそのうちの就業状態等基本集計（常住地）です。
--
-- 実測（令和2年・東京都、大学・大学院在学者）：早稲田キャンパス本部のメッシュ
-- で 669人、西早稲田キャンパス（理工）で 169人。学生2万人超のキャンパスです。
-- 都全体でも未就学＋在学＋卒業が常住人口の 96.9% に収まります。通学地基準なら
-- 他県からの流入で常住人口を超えるはずで、超えていません。つまり「そこに
-- 住んでいる学生」であって「そこに通ってくる学生」ではありません。昼間人口が
-- 要るなら従業地・通学地集計（別の表 ID）が別に要ります。
--
-- では何が入っているのか。**歯科の判断を変えるものが3つ**あります。
--
-- 1. **利用交通手段。** 通勤・通学に自家用車を使う人の割合です。来院手段
--    そのものではありませんが、その地域で車が要るかどうかの強い代理です。
--    駐車場の要否は「データが無いので現地で確認」としか書けませんでした。
--    実測：早稲田駅前のメッシュは鉄道1,279人に対し自家用車53人。地方なら
--    この比率が逆転します。
--
-- 2. **居住期間。** 20年以上住んでいる人が多い街と、1年未満が多い街では、
--    かかりつけとリコール（定期管理）の回り方がまるで違います。歯科医院の
--    継続性に直結する話で、年齢構成からは分かりません。
--
-- 3. **未就学者の内訳（幼稚園・保育園・認定こども園）。** 0〜14歳という
--    括りより、小児歯科の需要にずっと近い数字です。
--
-- 列は「取れたら入る」形にします。取れなかった列は NULL のまま。0 で
-- 埋めません——「自家用車が0人」と「自家用車が分からない」は別のことです。
CREATE TABLE IF NOT EXISTS mesh_resident_profile (
    id                   bigserial PRIMARY KEY,
    source_id            text NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    mesh_code            text NOT NULL,
    mesh_size_m          integer NOT NULL,
    prefecture_code      text NOT NULL,

    -- 利用交通手段（15歳以上・自宅外就業者・通学者）。1人が複数を使うので、
    -- 合計は就業者・通学者の数と一致しません。**比率で読むものです。**
    commute_walk         integer,
    commute_rail         integer,
    commute_bus          integer,
    commute_car          integer,
    commute_motorcycle   integer,
    commute_bicycle      integer,

    -- 居住期間（総数）。
    resident_under_1y    integer,
    resident_1_to_5y     integer,
    resident_20y_plus    integer,

    -- 未就学者と在学者（常住）。
    preschool_total      integer,
    preschool_nursery    integer,
    students_high_school integer,
    students_university  integer,

    -- 当地に常住する15歳以上の就業者・通学者。
    workers_living_here  integer,
    students_living_here integer,

    -- 雇用形態（15歳以上）。パートが厚い商圏は、平日昼に動ける人が多い。
    employees_regular    integer,
    employees_part_time  integer,
    self_employed        integer,

    source_date          date,
    last_updated         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, mesh_code)
);

CREATE INDEX IF NOT EXISTS mesh_resident_profile_code_idx
    ON mesh_resident_profile (mesh_size_m, mesh_code);
CREATE INDEX IF NOT EXISTS mesh_resident_profile_pref_idx
    ON mesh_resident_profile (prefecture_code);
