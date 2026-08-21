-- 009_walk_network.sql
-- A walkable street network, and trade areas measured along it.
--
-- Every catchment so far has been a circle. A circle crosses the Sumida river,
-- the Yamanote embankment and the Kanpachi in one step, and counts everyone on
-- the far side as if they were customers. The people who actually walk in are
-- the ones within so many metres *of street*, which is a different shape --
-- often dramatically so near a barrier.
--
-- Two halves, deliberately separable:
--
--   walk_network         plain PostGIS. Loads and draws with no extension
--                        beyond what the rest of the schema needs.
--   kg_walk_catchment    needs pgRouting. Created only where the extension is
--                        available, so `migrate` still succeeds on a database
--                        without it and the feature reports itself missing
--                        rather than breaking the deployment.

CREATE TABLE IF NOT EXISTS walk_network (
    id            bigserial PRIMARY KEY,
    source_id     text NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    osm_id        text,
    name          text,
    -- OSM highway class, kept as published (footway, residential, ...). Which
    -- classes count as walkable is a configuration question, not a schema one.
    road_class    text,
    geom          geometry(LineString, 4326) NOT NULL,
    -- Metres along the segment. Walking is bidirectional, so there is one cost
    -- and no reverse_cost: oneway restrictions apply to vehicles.
    cost_m        double precision NOT NULL,
    -- Filled by pgr_createTopology after load.
    source        bigint,
    target        bigint,
    source_date   date,
    last_updated  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS walk_network_geom_idx   ON walk_network USING gist (geom);
CREATE INDEX IF NOT EXISTS walk_network_source_idx ON walk_network (source);
CREATE INDEX IF NOT EXISTS walk_network_target_idx ON walk_network (target);


DO $outer$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pgrouting') THEN
        RAISE NOTICE 'pgrouting is not available here; walking catchments will be '
                     'unavailable and analysis falls back to circles';
        RETURN;
    END IF;

    CREATE EXTENSION IF NOT EXISTS pgrouting;

    -- Reachable area on foot: every street within p_distance_m *along the
    -- network* of the nearest junction, widened by p_buffer_m so that the
    -- catchment is an area rather than a bundle of lines. Front doors are set
    -- back from the kerb; the buffer is what stands in for that.
    --
    -- Returns NULL rather than an empty polygon when there is no network near
    -- the point, so callers can fall back to a circle and say that they did.
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
        -- Only ever consider the streets that could possibly be in range. The
        -- network is city-wide; the search must not be. The margin covers a
        -- path that leaves the box and comes back.
        v_box := ST_Buffer(v_point::geography, p_distance_m * 1.5 + 500)::geometry;

        SELECT v.id INTO v_start
        FROM walk_network_vertices_pgr v
        WHERE v.the_geom && v_box
          AND ST_DWithin(v.the_geom::geography, v_point::geography, p_snap_m)
        ORDER BY v.the_geom::geography <-> v_point::geography
        LIMIT 1;

        IF v_start IS NULL THEN
            RETURN NULL;   -- nothing walkable nearby; the caller uses a circle
        END IF;

        WITH reachable AS (
            SELECT node
            FROM pgr_drivingDistance(
                'SELECT w.id, w.source, w.target, w.cost_m AS cost, w.cost_m AS reverse_cost
                   FROM walk_network w
                  WHERE w.source IS NOT NULL AND w.target IS NOT NULL
                    AND w.geom && ' || quote_literal(v_box::text) || '::geometry',
                v_start, p_distance_m, directed := false)
        )
        SELECT ST_Buffer(
                 ST_Collect(w.geom)::geography, p_buffer_m
               )::geometry
        INTO v_area
        FROM walk_network w
        WHERE w.source IN (SELECT node FROM reachable)
          AND w.target IN (SELECT node FROM reachable);

        RETURN v_area;   -- NULL when the start node is isolated
    END;
    $body$;
    $fn$;
END
$outer$;
