-- 500m メッシュ別の将来推計人口。
--
-- なぜ要るか。総合スコアの 20% を占める「成長」が、2015→2020 の実績だけで
-- 決まっていました（scoring.yaml の growth.metric = population_growth）。
-- 歯科の開業は 20〜30 年の意思決定です。過去 5 年の増減は、これから 30 年の
-- 代理としてかなり弱い。過去は増えていても将来推計では減る地域はいくらでも
-- あります。**いちばん重い問いに、いちばん弱い指標で答えていました。**
--
-- 形の決め方が 2 つあります。
--
-- **年を列ではなく行にする。** 2025〜2050 を列にすると、推計の版が変わって
-- 年次が動くたびにマイグレーションが要ります。行にしておけば、どの年が
-- 手に入っているかはデータが答えます（取得できた年・できなかった年を
-- 明示できる、という要件にも合います）。
--
-- **ジオメトリを持たない。** 同じメッシュ格子なので、population_mesh の
-- polygon に mesh_code で結合すれば、面積按分の重み付けをそのまま使えます。
-- 8 年ぶんの polygon を複製するのは容量の無駄で、しかも両者がずれ得ます。
CREATE TABLE IF NOT EXISTS mesh_population_projection (
    id              bigserial PRIMARY KEY,
    source_id       text NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    mesh_code       text NOT NULL,
    mesh_size_m     integer NOT NULL,
    prefecture_code text NOT NULL,
    -- 推計の対象年。
    projection_year integer NOT NULL,
    -- 推計値は按分の結果なので整数とは限りません。公表値の丸めに合わせず、
    -- そのまま持ちます。丸めるのは表示するときの仕事です。
    population      double precision,
    age_0_14        double precision,
    age_15_64       double precision,
    age_65_plus     double precision,
    -- 推計の基準年（例: 2020）。「2040年の人口」だけでは、いつ時点の
    -- どの推計かが分かりません。レポートに出典として書くために持ちます。
    base_year       integer,
    -- 公表されている推計の名前をそのまま（例: 「令和2年国勢調査ベース」）。
    -- こちらで言い換えると、出典を辿れなくなります。
    estimate_label  text,
    source_date     date,
    last_updated    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, mesh_code, projection_year)
);

CREATE INDEX IF NOT EXISTS mesh_projection_lookup_idx
    ON mesh_population_projection (mesh_size_m, prefecture_code, projection_year);
CREATE INDEX IF NOT EXISTS mesh_projection_mesh_idx
    ON mesh_population_projection (mesh_code, mesh_size_m);
