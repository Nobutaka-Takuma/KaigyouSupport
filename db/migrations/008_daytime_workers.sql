-- 008_daytime_workers.sql
-- Add workers and establishments to the point analysis.
--
-- The trade area already sums residents by area-weighted intersection; this
-- does the same for mesh_business, from its own geometry. That matters: 286
-- Tokyo meshes hold establishments and no residents, so joining through
-- population_mesh would silently drop them -- and they are precisely the
-- office blocks the residential figure cannot see.
--
-- DROP then CREATE, not CREATE OR REPLACE: the return type gains columns, and
-- PostgreSQL will not replace a function whose signature changed.

DROP FUNCTION IF EXISTS kg_analyze_point(
    double precision, double precision, double precision, text, integer);

CREATE FUNCTION kg_analyze_point(
    p_lat               double precision,
    p_lng               double precision,
    p_radius_m          double precision,
    p_facility_category text    DEFAULT 'dental_clinic',
    p_mesh_size_m       integer DEFAULT 1000
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
BEGIN
    -- Circle of exactly p_radius_m metres, built in geography then handed back
    -- to planar 4326 for cheap intersection maths.
    v_buf := ST_Buffer(v_point::geography, p_radius_m)::geometry;

    RETURN QUERY
    WITH mesh_share AS (
        SELECT
            m.population, m.age_0_14, m.age_15_64, m.age_65_plus,
            m.households, m.population_growth,
            -- share of the mesh polygon covered by the trade area, 0..1
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
            -- population-weighted mean growth rate
            SUM(ms.population_growth * COALESCE(ms.population, 0) * ms.share)
                / NULLIF(SUM(CASE WHEN ms.population_growth IS NOT NULL
                                  THEN COALESCE(ms.population, 0) * ms.share END), 0)
                                                                           AS population_growth,
            COUNT(*)::integer                                              AS mesh_count
        FROM mesh_share ms
    ),
    -- Same apportionment, its own geometry. NULL rather than 0 when the
    -- dataset has not been loaded, so the scoring layer can tell "no workers
    -- here" apart from "we do not know", and withhold the component instead of
    -- scoring the area as empty.
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
    fac AS (
        SELECT COUNT(*)::integer AS facility_count
        FROM facilities f
        WHERE f.facility_category = p_facility_category
          AND ST_DWithin(f.geom::geography, v_point::geography, p_radius_m)
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
        -- Zero facilities is a legitimate answer, not a division: leave it NULL
        -- and let the scoring layer apply its configured cap.
        CASE WHEN fac.facility_count > 0
             THEN pop.population / fac.facility_count END,
        CASE WHEN fac.facility_count > 0
             THEN biz.workers / fac.facility_count END,
        nearest_fac.id, nearest_fac.name, nearest_fac.dist,
        nearest_stn.id, nearest_stn.name, nearest_stn.dist, nearest_stn.daily_passengers
    FROM pop
    CROSS JOIN biz
    CROSS JOIN fac
    LEFT JOIN nearest_fac ON true
    LEFT JOIN nearest_stn ON true;
END;
$$;
