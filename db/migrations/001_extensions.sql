-- 001_extensions.sql
-- PostGIS is the primary GIS engine for this project. All spatial work
-- (buffers, area-weighted intersection, nearest-neighbour) happens in SQL.

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
