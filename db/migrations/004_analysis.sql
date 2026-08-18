-- 004_analysis.sql
-- Candidate locations, cached analysis, score-normalisation statistics and
-- precomputed mesh scores (which feed both the ranking and the heat map).

CREATE TABLE IF NOT EXISTS candidate_locations (
    id          bigserial PRIMARY KEY,
    label       text,
    geom        geometry(Point, 4326) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS candidate_locations_geom_idx ON candidate_locations USING GIST (geom);

CREATE TABLE IF NOT EXISTS analysis_results (
    id                          bigserial PRIMARY KEY,
    candidate_location_id       bigint REFERENCES candidate_locations(id) ON DELETE CASCADE,
    radius_m                    integer NOT NULL,
    profile                     text NOT NULL,
    population                  double precision,
    age_0_14                    double precision,
    age_15_64                   double precision,
    age_65_plus                 double precision,
    households                  double precision,
    population_growth           double precision,
    facility_count              integer,
    population_per_facility     double precision,
    nearest_facility_distance_m double precision,
    nearest_station             text,
    station_distance_m          double precision,
    daily_passengers            integer,
    demand_score                double precision,
    competition_score           double precision,
    growth_score                double precision,
    accessibility_score         double precision,
    overall_score               double precision,
    calculated_at               timestamptz NOT NULL DEFAULT now(),
    UNIQUE (candidate_location_id, radius_m, profile)
);

-- Distribution statistics used to normalise raw metrics onto a 0-100 scale.
-- Recomputed by `kaigyou-etl refresh-stats` after every load, so the scale
-- always reflects the data actually in the database.
CREATE TABLE IF NOT EXISTS metric_distributions (
    metric          text NOT NULL,
    scope           text NOT NULL,   -- e.g. 'mesh:1000:r1000:pref13'
    min_value       double precision,
    p05             double precision,
    p50             double precision,
    p95             double precision,
    max_value       double precision,
    mean_value      double precision,
    stddev_value    double precision,
    sample_count    integer NOT NULL,
    computed_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (metric, scope)
);

-- One row per mesh per (profile, radius). Drives /api/rankings and the
-- score heat map layer.
CREATE TABLE IF NOT EXISTS mesh_scores (
    mesh_id                     bigint NOT NULL REFERENCES population_mesh(id) ON DELETE CASCADE,
    profile                     text NOT NULL,
    radius_m                    integer NOT NULL,
    population                  double precision,
    age_0_14                    double precision,
    age_65_plus                 double precision,
    households                  double precision,
    population_growth           double precision,
    facility_count              integer,
    population_per_facility     double precision,
    nearest_facility_distance_m double precision,
    nearest_station             text,
    station_distance_m          double precision,
    daily_passengers            integer,
    demand_score                double precision,
    competition_score           double precision,
    growth_score                double precision,
    accessibility_score         double precision,
    overall_score               double precision,
    area_label                  text,
    computed_at                 timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (mesh_id, profile, radius_m)
);

CREATE INDEX IF NOT EXISTS mesh_scores_overall_idx
    ON mesh_scores (profile, radius_m, overall_score DESC);
