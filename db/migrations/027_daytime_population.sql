-- 500m メッシュ別の昼間人口（従業地・通学地による人口）。
--
-- なぜ要るか。「昼間の人」を経済センサスの**従業者数**だけで測っていました。
-- 従業者は昼間そこにいる人の一部でしかありません。**通学者が丸ごと落ちます。**
--
-- 実測：早稲田駅前（半径1km）のレポートは、従業者数 52,688 人を昼間人口の
-- 代理として使い、大学生に一言も触れませんでした。早稲田大学の学生は
-- 従業者ではないので、経済センサスには 1 人も現れません。歯科医院にとって
-- 20代前半の数万人がいるかどうかは、診療内容も診療時間も変える情報です。
--
-- 出典は令和2年国勢調査の地域メッシュ統計「人口移動、就業状態等及び
-- 従業地・通学地」（2022年12月13日公表、e-Stat 統計GIS）。500m メッシュで
-- 提供されます。
--
-- 形の決め方は mesh_population_projection と同じ考え方です。
--
-- **ジオメトリを持たない。** 同じメッシュ格子なので、population_mesh の
-- polygon に mesh_code で結合すれば、面積按分の重み付けをそのまま使えます。
-- 複製すると容量の無駄なうえ、両者がずれ得ます。
--
-- **列は「取れたら入る」形にする。** 公表表がどの内訳まで持つかは版によって
-- 変わります。取れなかった列は NULL のままにし、0 で埋めません。
-- 「通学者が 0 人」と「通学者が分からない」はまったく別のことです。
CREATE TABLE IF NOT EXISTS mesh_daytime_population (
    id                bigserial PRIMARY KEY,
    source_id         text NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    mesh_code         text NOT NULL,
    mesh_size_m       integer NOT NULL,
    prefecture_code   text NOT NULL,
    -- 従業地・通学地による人口（いわゆる昼間人口）。
    daytime_population integer,
    -- そのうち、この場所で働いている人（従業地による就業者）。経済センサスの
    -- 「従業者数」とは調査も定義も違うので、別の列に持ちます。**足しません。**
    workers_here      integer,
    -- そのうち、この場所に通学している人。**ここが経済センサスに無い部分です。**
    students_here     integer,
    -- 参考として、同じ表が持つ常住人口（夜間人口）。population_mesh の値とは
    -- 集計が違うことがあるので、上書きせず別に持ちます。昼夜間人口比率を
    -- 出すときは、必ず同じ表の中で割ります。
    night_population  integer,
    source_date       date,
    last_updated      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, mesh_code)
);

CREATE INDEX IF NOT EXISTS mesh_daytime_population_code_idx
    ON mesh_daytime_population (mesh_size_m, mesh_code);
CREATE INDEX IF NOT EXISTS mesh_daytime_population_pref_idx
    ON mesh_daytime_population (prefecture_code);
