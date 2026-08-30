-- 道路網に都道府県を持たせる。
--
-- **2 つ目の県を入れる作業が、1 つ目を消す作業になっていました。**
--
-- 取り込みは `DELETE FROM walk_network WHERE source_id = ...` で全置換します。
-- source_id は都道府県を含まない 1 つの値（osm_walk_network）なので、静岡の
-- ファイルを入れた時点で東京の道路網が消えます。**しかも取り込みは成功と
-- 表示します。** メッシュ統計で同じことが起きて 012 で直したのと同じ形です。
--
-- そのうえ config の bbox は東京23区（139.56,35.53〜139.92,35.82）だったので、
-- 静岡の道路は 1 本も残りません。消えて、入らない。徒歩圏がどちらの県でも
-- 使えなくなります。
--
-- 既存行の都道府県は、辺の始点がどの市区町村ポリゴンに入るかで埋めます。
-- 決め打ちで '13' と書かないのは、bbox を書き換えて別の県を入れている人が
-- いるかもしれないからです。市区町村が未取得なら NULL のままにします
-- （「東京だと決めつけた」より「分からない」のほうが直しやすい）。

ALTER TABLE walk_network
    ADD COLUMN IF NOT EXISTS prefecture_code text;

UPDATE walk_network w
SET prefecture_code = m.prefecture_code
FROM municipalities m
WHERE w.prefecture_code IS NULL
  AND ST_Intersects(m.geom, ST_StartPoint(w.geom));

CREATE INDEX IF NOT EXISTS walk_network_pref_idx
    ON walk_network (prefecture_code);
