-- 市区町村単位の昼間人口（従業地・通学地による人口）、年齢5歳階級つき。
--
-- **商圏の数字ではありません。** ここがいちばん大事な区別です。
--
-- 新宿区の昼間人口は 793,528 人ですが、そのうち何人が早稲田駅前の半径1km
-- にいるかは、この表からは分かりません。歌舞伎町にも西新宿にもいます。
-- 面積で按分するのは**完全に間違い**です。商圏の昼間人口が欲しければ、
-- 500m メッシュの従業地・通学地統計（mesh_daytime_population）が要ります。
--
-- では何の役に立つのか。**文脈です。** 新宿区の 20〜24歳は、夜間 21,906 人に
-- 対して昼間 80,136 人。3.7 倍に膨らみます。この街には昼間、若い通学者が
-- 大量に流入している、という事実は、商圏の数字が無くても意思決定に効きます。
-- そして年齢別は、メッシュ統計には無い切り口です。
--
-- 実際、この数字は外部調査（Web検索）で自治体の PDF から拾っていました。
-- 一次データを手元に持てば、検索回数が減り、値も正確になります。
--
-- 年齢階級は行にします。列にすると、階級の区切りが変わるたびにマイグレーション
-- が要ります。行なら、どの階級が取れているかはデータが答えます。
CREATE TABLE IF NOT EXISTS municipality_daytime (
    id                 bigserial PRIMARY KEY,
    source_id          text NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    -- JIS X0402（5桁）。政令市の区も1行として入ります。
    municipality_code  text NOT NULL,
    municipality_name  text,
    prefecture_code    text NOT NULL,
    -- 公表の階級コードをそのまま（"00_総数" / "03_20～24歳" など）。
    -- こちらで言い換えると、出典を辿れなくなります。
    age_band           text NOT NULL,
    -- 並べ替え用。公表コードの先頭2桁。総数は 0。
    age_order          integer,
    sex                text NOT NULL,
    night_population   integer,
    daytime_population integer,
    source_date        date,
    last_updated       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, municipality_code, age_band, sex)
);

CREATE INDEX IF NOT EXISTS municipality_daytime_code_idx
    ON municipality_daytime (municipality_code, sex);
