-- 徒歩圏のポリゴンを、実際の OSM の密度でも作れるようにする。
--
-- 032 は合成の格子（40m 間隔・2 点/辺）で測って 12 倍速くなりました。**その
-- 格子が軽すぎました。** 実際の OSM は歩道・区画道路・建物間の通路が入り、
-- 道は曲がっていて 1 辺が何点も持ちます。20m 間隔・7 点/辺（分割後 135,720 辺）
-- で測り直すと、032 を当てた状態でもこうなります。
--
--   半径  500m    30.7 秒
--   半径 1000m   135.8 秒
--   半径 2000m   サーバが落ちる（バッファ中に out of memory）
--
-- 内訳を採ると、半径 1000m で pgr_drivingDistance 0.24 秒、到達辺の取り出し
-- 0.02 秒、**バッファ 146 秒**。効いているのは辺の数ではなく**点の数**です。
-- 到達辺 9,105 本が持つ点は 63,735。そして出来上がるポリゴンの頂点は 988 しか
-- ありません。**入れた細かさは捨てられています。**
--
-- 3 つ足します。どれも出力を変えないか、変えても 0.2% 未満です。
--
-- 1. **中心線を 10m で単純化してから膨らませる。** 40m 幅の縁を作るのに
--    1m の曲がりは要りません。63,735 点 -> 18,210 点、146 秒 -> 5.3 秒、
--    面積の差 0.14%。**ここだけが近似です。**
--
-- 2. **ST_LineMerge で本数を減らす。** 交差点で分割された辺は端点を共有して
--    いるので、結合すると長い 1 本になります。7.6 秒 -> 4.8 秒。出力は同じ。
--
-- 3. **約 140m の枡ごとに膨らませてから合わせる。** GEOS のバッファは点数に
--    対して superlinear なので、小さく割って足すほうが速くなります。合併は
--    結合則が成り立つので**結果は 1 回でやったときと完全に同じ**です
--    （頂点数・面積とも一致を確認）。半径 2000m で 36.5 秒 -> 4.5 秒。
--
-- 合わせて、同じ 135,720 辺の網で:
--
--   半径  500m    30.7 秒 ->  0.29 秒
--   半径 1000m   135.8 秒 ->  1.18 秒
--   半径 2000m   落ちる    ->  4.45 秒
--
-- 枡の大きさは 140m 前後が最良でした（半径 500m/1000m/2000m のいずれでも
-- 280m・560m より速い）。細かくしすぎると最後に合わせる多角形の数が増えます。

DO $outer$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgrouting') THEN
        RAISE NOTICE 'pgrouting absent; walking catchments stay unavailable';
        RETURN;
    END IF;

    EXECUTE $fn$
    CREATE OR REPLACE FUNCTION kg_walk_catchment(
        p_lat        double precision,
        p_lng        double precision,
        p_distance_m double precision,
        p_buffer_m   double precision DEFAULT 40,
        p_snap_m     double precision DEFAULT 300
    )
    RETURNS geometry
    LANGUAGE plpgsql STABLE AS $body$
    DECLARE
        v_point geometry(Point, 4326) := ST_SetSRID(ST_MakePoint(p_lng, p_lat), 4326);
        v_start bigint;
        v_box   geometry;
        v_area  geometry;
        -- メルカトルは緯度が上がるほど引き伸ばされます。地上で p_buffer_m に
        -- するには、その分だけ大きい距離で膨らませます。
        v_scale double precision := 1.0 / GREATEST(cos(radians(p_lat)), 0.01);
        -- 中心線をどこまで粗くしてよいか。40m の縁に対して 10m。
        v_simplify constant double precision := 10.0 / 111320.0;
        -- 膨らませる作業を区切る枡の大きさ（度）。約 140m。
        v_cell     constant double precision := 0.00125;
    BEGIN
        IF to_regclass('public.walk_network_noded') IS NULL THEN
            RETURN NULL;          -- network not loaded; caller uses a circle
        END IF;

        -- Only the streets that could possibly be in range. The network is
        -- city-wide; the search must not be. The margin allows for a path that
        -- leaves the box and comes back -- a river detour does exactly that.
        v_box := ST_Buffer(v_point::geography, p_distance_m * 1.5 + 500)::geometry;

        SELECT v.id INTO v_start
        FROM walk_network_noded_vertices_pgr v
        WHERE v.the_geom && v_box
          AND ST_DWithin(v.the_geom::geography, v_point::geography, p_snap_m)
        ORDER BY v.the_geom::geography <-> v_point::geography
        LIMIT 1;

        IF v_start IS NULL THEN
            RETURN NULL;          -- nothing walkable within p_snap_m
        END IF;

        WITH reachable AS (
            SELECT node
            FROM pgr_drivingDistance(
                'SELECT n.id, n.source, n.target, n.cost_m AS cost, n.cost_m AS reverse_cost
                   FROM walk_network_noded n
                  WHERE n.source IS NOT NULL AND n.target IS NOT NULL
                    AND n.geom && ' || quote_literal(v_box::text) || '::geometry',
                v_start, p_distance_m, directed := false)
        ),
        edges AS (
            SELECT ST_Simplify(n.geom, v_simplify) AS geom,
                   ST_SnapToGrid(ST_StartPoint(n.geom), v_cell) AS cell
            FROM walk_network_noded n
            WHERE n.source IN (SELECT node FROM reachable)
              AND n.target IN (SELECT node FROM reachable)
        ),
        blocks AS (
            SELECT ST_Buffer(ST_Transform(ST_LineMerge(ST_Collect(geom)), 3857),
                             p_buffer_m * v_scale, 'quad_segs=2') AS g
            FROM edges
            GROUP BY cell
        )
        SELECT ST_Transform(ST_Union(g), 4326) INTO v_area FROM blocks;

        RETURN v_area;
    END;
    $body$;
    $fn$;
END
$outer$;
