-- 007_mesh_business.sql
-- Workers and establishments per mesh (経済センサス).
--
-- A separate table rather than columns on population_mesh, for two reasons.
--
-- The mesh sets genuinely differ. Tokyo has 5,436 meshes with establishments
-- and 5,449 with residents, overlapping in 5,150: 286 meshes hold businesses
-- and nobody living there, and 299 the reverse. Office districts are exactly
-- the places the residential-population model cannot see, so they must be
-- representable, and adding them to population_mesh would mean inventing
-- population rows for meshes the census never reported.
--
-- And provenance stays honest. Every row here points at the economic census,
-- not at the population census; one source_id per row is the rule the rest of
-- the schema follows.

CREATE TABLE IF NOT EXISTS mesh_business (
    id                      bigserial PRIMARY KEY,
    source_id               text NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    mesh_code               text NOT NULL,          -- JIS X0410 (8/9/10 digits)
    mesh_size_m             integer NOT NULL,       -- never hard-coded; read from the code
    prefecture_code         text NOT NULL,
    -- Self-contained geometry: the mesh code determines the cell, so this does
    -- not depend on a matching population_mesh row existing.
    geom                    geometry(Polygon, 4326) NOT NULL,
    centroid                geometry(Point, 4326) NOT NULL,
    -- 従業者数 / 事業所数, all industries (A-S).
    workers                 integer,
    establishments          integer,
    -- Per-industry detail, keyed by the published division letter (A-S).
    -- Kept as jsonb because which divisions a release publishes varies, and
    -- nothing in the MVP scores on individual industries yet.
    industry_workers        jsonb NOT NULL DEFAULT '{}'::jsonb,
    industry_establishments jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_date             date,
    last_updated            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, mesh_code)
);

CREATE INDEX IF NOT EXISTS mesh_business_geom_idx     ON mesh_business USING gist (geom);
CREATE INDEX IF NOT EXISTS mesh_business_centroid_idx ON mesh_business USING gist (centroid);
CREATE INDEX IF NOT EXISTS mesh_business_code_idx     ON mesh_business (mesh_size_m, mesh_code);
