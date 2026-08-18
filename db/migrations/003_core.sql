-- 003_core.sql
-- Core spatial tables.
--
-- Extensibility notes (MVP scope is Tokyo + dental clinics, but nothing here
-- is hard-wired to either):
--   * prefecture_code is a column, not an assumption. Tokyo is '13'.
--   * facilities.facility_category lets non-dental facility types share the
--     table; clinic_types[] carries the advertised specialities.
--   * population_mesh.mesh_size_m is stored per row, so 500m/250m meshes can
--     coexist with the 1km MVP default.

-- ---------------------------------------------------------------- boundaries
CREATE TABLE IF NOT EXISTS municipalities (
    id                  bigserial PRIMARY KEY,
    source_id           text NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    municipality_code   text NOT NULL,              -- JIS X0402, 5 digits
    name                text NOT NULL,
    prefecture_code     text NOT NULL,              -- JIS X0401, 2 digits
    prefecture_name     text,
    geom                geometry(MultiPolygon, 4326) NOT NULL,
    source_date         date,
    last_updated        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, municipality_code)
);

CREATE INDEX IF NOT EXISTS municipalities_geom_idx ON municipalities USING GIST (geom);
CREATE INDEX IF NOT EXISTS municipalities_pref_idx ON municipalities (prefecture_code);

-- ---------------------------------------------------------------- facilities
CREATE TABLE IF NOT EXISTS facilities (
    id                  bigserial PRIMARY KEY,
    source_id           text NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    facility_id         text NOT NULL,              -- identifier as published by the source
    facility_category   text NOT NULL DEFAULT 'dental_clinic',
    name                text NOT NULL,
    address             text,
    prefecture_code     text NOT NULL,
    municipality_code   text,
    geom                geometry(Point, 4326) NOT NULL,
    opening_date        date,
    clinic_types        text[] NOT NULL DEFAULT '{}',   -- 一般歯科 / 小児歯科 / 矯正歯科 ...
    founder_type        text,                            -- 開設者区分
    attributes          jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_date         date,
    last_updated        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, facility_id)
);

CREATE INDEX IF NOT EXISTS facilities_geom_idx  ON facilities USING GIST (geom);
CREATE INDEX IF NOT EXISTS facilities_geog_idx  ON facilities USING GIST ((geom::geography));
CREATE INDEX IF NOT EXISTS facilities_cat_idx   ON facilities (facility_category, prefecture_code);
CREATE INDEX IF NOT EXISTS facilities_types_idx ON facilities USING GIN (clinic_types);

-- Convenience view matching the naming in the requirements document.
CREATE OR REPLACE VIEW dental_clinics AS
    SELECT * FROM facilities WHERE facility_category = 'dental_clinic';

-- ----------------------------------------------------------- population mesh
CREATE TABLE IF NOT EXISTS population_mesh (
    id                      bigserial PRIMARY KEY,
    source_id               text NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    mesh_code               text NOT NULL,          -- JIS X0410 (8/9/10 digits)
    mesh_size_m             integer NOT NULL,       -- 1000 / 500 / 250 -- never hard-coded
    prefecture_code         text NOT NULL,
    geom                    geometry(Polygon, 4326) NOT NULL,
    centroid                geometry(Point, 4326) NOT NULL,
    population              integer,
    age_0_14                integer,
    age_15_64               integer,
    age_65_plus             integer,
    households              integer,
    population_growth       double precision,       -- fraction, e.g. 0.034 = +3.4%
    age_breakdown           jsonb NOT NULL DEFAULT '{}'::jsonb,  -- optional 5-year bands
    source_date             date,
    last_updated            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, mesh_code)
);

CREATE INDEX IF NOT EXISTS population_mesh_geom_idx     ON population_mesh USING GIST (geom);
CREATE INDEX IF NOT EXISTS population_mesh_geog_idx     ON population_mesh USING GIST ((geom::geography));
CREATE INDEX IF NOT EXISTS population_mesh_centroid_idx ON population_mesh USING GIST (centroid);
CREATE INDEX IF NOT EXISTS population_mesh_size_idx     ON population_mesh (mesh_size_m, prefecture_code);

-- ------------------------------------------------------------------ stations
CREATE TABLE IF NOT EXISTS stations (
    id                  bigserial PRIMARY KEY,
    source_id           text NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    station_id          text NOT NULL,
    name                text NOT NULL,
    operator            text,
    railway_line        text,
    prefecture_code     text NOT NULL,
    geom                geometry(Point, 4326) NOT NULL,
    daily_passengers    integer,
    passengers_year     integer,                    -- year the passenger figure refers to
    source_date         date,
    last_updated        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, station_id)
);

CREATE INDEX IF NOT EXISTS stations_geom_idx ON stations USING GIST (geom);
CREATE INDEX IF NOT EXISTS stations_geog_idx ON stations USING GIST ((geom::geography));
CREATE INDEX IF NOT EXISTS stations_name_idx ON stations (name);
