-- 006_station_attributes.sql
--
-- Two corrections that only became apparent once the real S12 file was in
-- hand.
--
-- 1. S12 publishes no prefecture. Every other source carries one, but forcing
--    a value here would mean inventing it, so the column becomes nullable and
--    is filled by spatial lookup where mesh data allows.
-- 2. A station combines several published rows (one per operator). The
--    breakdown is worth keeping, so stations gain the same jsonb attribute
--    bag that facilities already has.

ALTER TABLE stations ALTER COLUMN prefecture_code DROP NOT NULL;

ALTER TABLE stations
    ADD COLUMN IF NOT EXISTS attributes jsonb NOT NULL DEFAULT '{}'::jsonb;
