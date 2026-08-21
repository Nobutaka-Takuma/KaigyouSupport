-- 010_walk_network_noding.sql
-- Route over a *noded* network, not the raw one.
--
-- pgr_createTopology only joins lines that share an endpoint. Streets that
-- simply cross are left unconnected, because a crossing is not an endpoint --
-- and in a Geofabrik extract a residential street is one long way that passes
-- through many junctions without being split at any of them. The result is a
-- graph in fragments: on a 25-edge test grid, the largest connected component
-- was four edges, and catchments came out at 2% of the area they should be.
--
-- pgr_nodeNetwork splits every edge wherever another crosses it and writes
-- walk_network_noded, which keeps old_id back to the street it came from. That
-- is the table worth routing over.

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
        SELECT ST_Buffer(ST_Collect(n.geom)::geography, p_buffer_m)::geometry
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
