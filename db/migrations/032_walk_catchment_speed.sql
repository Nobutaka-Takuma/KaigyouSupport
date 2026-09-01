-- 徒歩圏のポリゴンを、同じ形のまま速く作る。
--
-- **徒歩圏で分析が始められないのは、経路探索のせいではありません。**
-- 4km 四方・40m 間隔の格子（分割後 20,200 辺）で測った内訳です。
--
--   pgr_drivingDistance          0.07 秒
--   到達した辺を取り出す          0.01 秒
--   ST_Buffer(...::geography)   42.78 秒   ← ここが全部
--
-- 半径 2km では到達辺が 9,902 本になり、geography の ST_Buffer はその 1 本ずつ
-- を投影して丸め、9,902 個の角丸長方形を結合します。辺の数に対して superlinear
-- なので、辺が 4 倍になると時間は 7 倍になります。
--
-- build_dataset は 500m・1000m・2000m と、そのあと 1000m をもう一度、さらに
-- 地図用にもう一度呼びます。1 回 40 秒なら、ジョブを作る HTTP 応答の中で
-- 数分かかります。**API がタイムアウトして「分析を開始できない」ようになるのは
-- これです。**
--
-- 直し方は、geography をやめて投影座標で膨らませることです。同じ格子で測定:
--
--   現行 geography quad_segs=8   42.78 秒   頂点 6,320   面積 8.316 km²
--   geography quad_segs=2        20.57 秒   頂点 1,665   面積 8.292 km²
--   **3857 quad_segs=2            3.46 秒   頂点   633   面積 8.291 km²**
--   平面4326 flat/mitre           1.06 秒   頂点   499   面積 8.065 km²
--
-- 3857（Web メルカトル）を選びます。**面積の差は 0.3%** で、頂点は 1/10 です。
-- 頂点が減ることは速さと別に効きます——このポリゴンはこのあとメッシュ 1 枚ずつ
-- との ST_Intersection に渡され、GeoJSON として画面へ送られるからです。
--
-- 4326 のまま度で膨らませるのがいちばん速いのですが、東経方向だけ cos(緯度)
-- 倍に潰れます（緯度 35.7 度で 40m のはずが 32.5m）。3 秒のために形を歪める
-- 理由がありません。メルカトルは局所的に等角なので、緯度に応じて
-- 1/cos(緯度) 倍した距離で膨らませれば地上で 40m になります。
--
-- quad_segs=2 は「厳密さを少し捨てる」ところです。40m の縁を四分円あたり
-- 2 辺で近似します。500m メッシュの面積按分にも、地図の表示にも影響しません。

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
        -- するには、その分だけ大きい距離で膨らませます。cos は下限を切って
        -- おきます（日本では効きませんが、0 除算で落ちるよりよい）。
        v_scale double precision := 1.0 / GREATEST(cos(radians(p_lat)), 0.01);
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
        )
        SELECT ST_Transform(
                   ST_Buffer(ST_Transform(ST_Collect(n.geom), 3857),
                             p_buffer_m * v_scale,
                             'quad_segs=2'),
                   4326)
        INTO v_area
        FROM walk_network_noded n
        WHERE n.source IN (SELECT node FROM reachable)
          AND n.target IN (SELECT node FROM reachable);

        RETURN v_area;
    END;
    $body$;
    $fn$;
END
$outer$;
