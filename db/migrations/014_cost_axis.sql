-- 014_cost_axis.sql
-- Give the analysis a cost axis, so the model stops answering only "where is
-- it good" and can also answer "where is it good for what it costs".
--
-- Without one, the score ranks Ginza above everywhere, which is true and
-- useless: good locations are expensive, and a practice is an business of
-- fixed costs. The land price of the trade area is the only published,
-- geocoded proxy for that available here.
--
-- Two figures, both from 地価公示:
--
--   land_price_yen_per_sqm  median of the surveyed parcels in the catchment
--   land_price_points       how many parcels that median rests on
--
-- The count matters as much as the median. 地価公示 surveys a few thousand
-- parcels in a prefecture, so a rural catchment can contain one, and the
-- median of one parcel is that parcel. The scoring layer withholds the cost
-- component below a configured minimum rather than scoring on a single point.
--
-- 商業地 first, all divisions as fallback: a clinic is a tenant in commercial
-- premises, and 住宅地 in the same block runs several times cheaper. Where the
-- catchment has no commercial parcel -- most of the suburbs -- the mixed
-- median is the honest answer, and land_price_basis says which was used so
-- the reader is not comparing the two silently.

DROP FUNCTION IF EXISTS kg_analyze_point(
    double precision, double precision, double precision, text, integer, text);

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
    land_price_yen_per_sqm      double precision,
    land_price_points           integer,
    land_price_basis            text,
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
    -- Surveyed parcels inside the same shape as everything else. Points, so a
    -- plain containment test; no apportionment applies to a point.
    land_pts AS (
        SELECT l.price_yen_per_sqm AS price,
               (l.use_category_code = '005') AS commercial
        FROM land_prices l
        WHERE l.geom && v_buf
          AND ST_Intersects(l.geom, v_buf)
          AND l.survey_year = (SELECT max(survey_year) FROM land_prices)
    ),
    land AS (
        SELECT
            COALESCE(
                percentile_cont(0.5) WITHIN GROUP (ORDER BY lp.price)
                    FILTER (WHERE lp.commercial),
                percentile_cont(0.5) WITHIN GROUP (ORDER BY lp.price)
            )                                                    AS price,
            COUNT(*)::integer                                    AS points,
            CASE WHEN COUNT(*) FILTER (WHERE lp.commercial) > 0
                 THEN 'commercial' ELSE 'all' END                AS basis
        FROM land_pts lp
    ),
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
        land.price, land.points,
        CASE WHEN land.points > 0 THEN land.basis END,
        v_kind,
        ST_Area(v_buf::geography) / 1e6,
        nearest_fac.id, nearest_fac.name, nearest_fac.dist,
        nearest_stn.id, nearest_stn.name, nearest_stn.dist, nearest_stn.daily_passengers
    FROM pop
    CROSS JOIN biz
    CROSS JOIN land
    CROSS JOIN fac
    LEFT JOIN nearest_fac ON true
    LEFT JOIN nearest_stn ON true;
END;
$$;


-- Kept on the scored mesh so the ranking can show what a place costs beside
-- what it scores, without re-running the analysis per row.
ALTER TABLE mesh_scores ADD COLUMN IF NOT EXISTS land_price_yen_per_sqm double precision;
ALTER TABLE mesh_scores ADD COLUMN IF NOT EXISTS cost_score double precision;
