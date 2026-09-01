-- 都市計画決定情報（国土数値情報 A55）。
--
-- **この表が答えるのは「そこに何人いるか」ではなく「そこに何が建てられるか」です。**
--
-- 用途地域・容積率・建蔽率はすでに地価公示（land_prices）が持っていますが、
-- **地価公示は点です。** 静岡県で 3,221 点しかなく、候補地の近くに点が無ければ
-- 「不明」になります。しかも一番近い点の用途地域が候補地の用途地域とは限りません
-- ——用途地域の境目は道1本で変わります。A55 は面なので、候補地がどの区域に
-- 入るかを ST_Contains で確定できます。
--
-- 1 つの表に全レイヤを入れます。A55 は 14 種類のポリゴンを別々のファイルで
-- 配りますが、構造はどれも「区分名・区分コード・固有名・市区町村・決定日・面」で
-- 同じです。レイヤごとに表を作ると、地域地区が 1 つ増えるたびにマイグレーションと
-- API と画面を足すことになります。zone_kind で分けます。
--
-- far / bcr は用途地域にしか無いので NULL 可。**0 と NULL は違います**——
-- 「容積率の定めが無い」と「まだ取り込んでいない」を混ぜないため、
-- 空文字は NULL にして入れます。
--
-- 将来の拡張（他県・他業態）は prefecture_code で分かれます。取り込みは
-- (source_id, prefecture_code) で置き換えるので、静岡を入れ直しても東京は
-- 消えません。walk_network で同じことを 031 で直したのと同じ形です。

CREATE TABLE IF NOT EXISTS city_planning_zones (
    id                bigserial PRIMARY KEY,
    source_id         text NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    prefecture_code   text NOT NULL,
    municipality_code text,
    municipality_name text,
    -- どのレイヤか（youto / senbiki / ritteki ...）。設定の layers の鍵と同じ。
    zone_kind         text NOT NULL,
    -- 日本語の層名（用途地域 / 区域区分 / 立地適正化計画 ...）。
    zone_kind_label   text,
    -- 区分名（第一種低層住居専用地域 / 市街化調整区域 / 居住誘導区域 ...）。
    zone_type         text,
    zone_code         integer,
    -- 固有名がある層のためのもの（公園名・地区計画名）。無い層では NULL。
    zone_name         text,
    -- 容積率・建蔽率（%）。用途地域だけが持ちます。
    far               numeric,
    bcr               numeric,
    -- 告示日。A55 では入っている市町と空の市町があります。
    decided_on        date,
    geom              geometry(MultiPolygon, 4326) NOT NULL,
    source_date       date,
    last_updated      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS city_planning_zones_geom_idx
    ON city_planning_zones USING gist (geom);
-- 「この点はどの区域か」は必ず層を指定して引くので、層を先頭に置きます。
CREATE INDEX IF NOT EXISTS city_planning_zones_kind_idx
    ON city_planning_zones (prefecture_code, zone_kind);
