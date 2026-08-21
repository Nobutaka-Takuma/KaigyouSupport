-- 011_catchment_mode.sql
-- Let the trade area be a walk, not only a circle.
--
-- The shape is now a choice, and which one was used is returned with the
-- numbers. That matters more than it sounds: a circle and a walking catchment
-- around the same point can differ by a factor of three, so a population
-- figure without its shape is not interpretable. Measured on the test network,
-- with a river and one bridge, at 500m: circle 0.78 km² crossing the river,
-- walk 0.29 km² stopping at the bank.
--
-- 'circle' stays the default. It needs no extra dataset, it is instant, and
-- every score computed so far used it -- switching silently would change every
-- number on the site without anything saying so.

DROP FUNCTION IF EXISTS kg_analyze_point(
    double precision, double precision, double precision, text, integer);

-- The trade area itself, so the map can draw the same shape the numbers used.
-- Falls back to a circle whenever a walk is asked for and cannot be produced --
-- no network loaded, no pgRouting, or no street within reach of the point.
CREATE OR REPLACE FUNCTION kg_catchment(
    p_lat        double precision,
    p_lng        double precision,
    p_radius_m   double precision,
    p_mode       text DEFAULT 'circle'
)
RETURNS TABLE (geom geometry, kind text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_point geometry(Point, 4326) := ST_SetSRID(ST_MakePoint(p_lng, p_lat), 4326);
    v_walk  geometry;
BEGIN
    IF p_mode = 'walk' AND to_regproc('kg_walk_catchment') IS NOT NULL THEN
        v_walk := kg_walk_catchment(p_lat, p_lng, p_radius_m);
        IF v_walk IS NOT NULL AND NOT ST_IsEmpty(v_walk) THEN
            RETURN QUERY SELECT v_walk, 'walk'::text;
            RETURN;
        END IF;
    END IF;
    RETURN QUERY SELECT ST_Buffer(v_point::geography, p_radius_m)::geometry,
                        'circle'::text;
END;
$$;

CREATE FUNCTION kg_analyze_point(
    p_lat               double precision,
    p_lng               double precision,
    p_radius_m          double precision,
    p_facility_category text    DEFAULT 'dental_clinic',
    p_mesh_size_m       integer DEFAULT 1000,
    p_catchment         text    DEFAULT 'circle'
)
RETURNS TABLE (
    population                  double precision,
    age_0_14                    double precision,
    age_15_64                   double precision,
    age_65_plus                 double precision,
    households                  double precision,
    population_growth           double precision,
    mesh_count                  integer,
    workers                     double precision,
    establishments              double precision,
    worker_mesh_count           integer,
    facility_count              integer,
    population_per_facility     double precision,
    workers_per_facility        double precision,
    catchment_kind              text,
    catchment_area_km2          double precision,
    nearest_facility_id         bigint,
    nearest_facility_name       text,
    nearest_facility_distance_m double precision,
    nearest_station_id          bigint,
    nearest_station_name        text,
    nearest_station_distance_m  double precision,
    nearest_station_passengers  integer
)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_point geometry(Point, 4326) := ST_SetSRID(ST_MakePoint(p_lng, p_lat), 4326);
    v_buf   geometry;
    v_kind  text;
BEGIN
    SELECT c.geom, c.kind INTO v_buf, v_kind
    FROM kg_catchment(p_lat, p_lng, p_radius_m, p_catchment) c;

    RETURN QUERY
    WITH mesh_share AS (
        SELECT
            m.population, m.age_0_14, m.age_15_64, m.age_65_plus,
            m.households, m.population_growth,
            LEAST(1.0, GREATEST(0.0,
                ST_Area(ST_Intersection(m.geom, v_buf)::geography)
                / NULLIF(ST_Area(m.geom::geography), 0)
            )) AS share
        FROM population_mesh m
        WHERE m.mesh_size_m = p_mesh_size_m
          AND m.geom && v_buf
          AND ST_Intersects(m.geom, v_buf)
    ),
    pop AS (
        SELECT
            SUM(COALESCE(ms.population,  0) * ms.share)                    AS population,
            SUM(COALESCE(ms.age_0_14,    0) * ms.share)                    AS age_0_14,
            SUM(COALESCE(ms.age_15_64,   0) * ms.share)                    AS age_15_64,
            SUM(COALESCE(ms.age_65_plus, 0) * ms.share)                    AS age_65_plus,
            SUM(COALESCE(ms.households,  0) * ms.share)                    AS households,
            SUM(ms.population_growth * COALESCE(ms.population, 0) * ms.share)
                / NULLIF(SUM(CASE WHEN ms.population_growth IS NOT NULL
                                  THEN COALESCE(ms.population, 0) * ms.share END), 0)
                                                                           AS population_growth,
            COUNT(*)::integer                                              AS mesh_count
        FROM mesh_share ms
    ),
    biz AS (
        SELECT
            CASE WHEN COUNT(*) > 0 THEN
                SUM(COALESCE(b.workers, 0) * LEAST(1.0, GREATEST(0.0,
                    ST_Area(ST_Intersection(b.geom, v_buf)::geography)
                    / NULLIF(ST_Area(b.geom::geography), 0)))) END          AS workers,
            CASE WHEN COUNT(*) > 0 THEN
                SUM(COALESCE(b.establishments, 0) * LEAST(1.0, GREATEST(0.0,
                    ST_Area(ST_Intersection(b.geom, v_buf)::geography)
                    / NULLIF(ST_Area(b.geom::geography), 0)))) END          AS establishments,
            COUNT(*)::integer                                               AS worker_mesh_count
        FROM mesh_business b
        WHERE b.mesh_size_m = p_mesh_size_m
          AND b.geom && v_buf
          AND ST_Intersects(b.geom, v_buf)
    ),
    -- Competitors inside the same shape as the demand. Counting clinics in a
    -- circle while measuring population along the streets would put a rival
    -- across the river into a catchment its customers cannot reach.
    --
    -- Circles keep the exact geodesic test they always used. ST_Buffer on
    -- geography returns a 32-gon inscribed in the true circle -- about 99.6% of
    -- its area -- so switching them to an intersection would drop the occasional
    -- clinic near the edge and move numbers that nothing else here changed.
    fac AS (
        SELECT COUNT(*)::integer AS facility_count
        FROM facilities f
        WHERE f.facility_category = p_facility_category
          AND ((v_kind = 'walk' AND f.geom && v_buf AND ST_Intersects(f.geom, v_buf))
            OR (v_kind <> 'walk'
                AND ST_DWithin(f.geom::geography, v_point::geography, p_radius_m)))
    ),
    nearest_fac AS (
        SELECT f.id, f.name,
               ST_Distance(f.geom::geography, v_point::geography) AS dist
        FROM facilities f
        WHERE f.facility_category = p_facility_category
        ORDER BY f.geom::geography <-> v_point::geography
        LIMIT 1
    ),
    nearest_stn AS (
        SELECT s.id, s.name, s.daily_passengers,
               ST_Distance(s.geom::geography, v_point::geography) AS dist
        FROM stations s
        ORDER BY s.geom::geography <-> v_point::geography
        LIMIT 1
    )
    SELECT
        pop.population, pop.age_0_14, pop.age_15_64, pop.age_65_plus,
        pop.households, pop.population_growth, pop.mesh_count,
        biz.workers, biz.establishments, biz.worker_mesh_count,
        fac.facility_count,
        CASE WHEN fac.facility_count > 0
             THEN pop.population / fac.facility_count END,
        CASE WHEN fac.facility_count > 0
             THEN biz.workers / fac.facility_count END,
        v_kind,
        ST_Area(v_buf::geography) / 1e6,
        nearest_fac.id, nearest_fac.name, nearest_fac.dist,
        nearest_stn.id, nearest_stn.name, nearest_stn.dist, nearest_stn.daily_passengers
    FROM pop
    CROSS JOIN biz
    CROSS JOIN fac
    LEFT JOIN nearest_fac ON true
    LEFT JOIN nearest_stn ON true;
END;
$$;
