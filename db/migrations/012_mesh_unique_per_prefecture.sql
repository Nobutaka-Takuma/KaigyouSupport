-- 012_mesh_unique_per_prefecture.sql
-- A mesh belongs to a prefecture, and one mesh can belong to two.
--
-- e-Stat publishes mesh statistics one prefecture at a time, and a mesh that
-- straddles a boundary appears in both files -- each carrying the part that
-- falls inside that prefecture. With UNIQUE (source_id, mesh_code) the second
-- file's row collides with the first's, and the upsert takes the figures while
-- leaving prefecture_code as it was: the row then reports Kanagawa's residents
-- under a Tokyo label, and the other side's residents are gone. Nothing about
-- that is visible from the outside.
--
-- Adding prefecture_code to the key gives a straddling mesh one row per side.
-- The point analysis sums by geometric intersection and so still counts the
-- whole mesh; the ranking, which filters by prefecture, sees each prefecture's
-- own share, which is what it should rank.
--
-- Harmless where only one prefecture is loaded, which is why it can be applied
-- to an existing database without touching a row.

ALTER TABLE population_mesh
    DROP CONSTRAINT IF EXISTS population_mesh_source_id_mesh_code_key;
ALTER TABLE mesh_business
    DROP CONSTRAINT IF EXISTS mesh_business_source_id_mesh_code_key;

-- Not constraints, because a partial or expression index is not what is wanted
-- here and a plain unique index is what ON CONFLICT needs to infer.
CREATE UNIQUE INDEX IF NOT EXISTS population_mesh_source_mesh_pref_key
    ON population_mesh (source_id, mesh_code, prefecture_code);
CREATE UNIQUE INDEX IF NOT EXISTS mesh_business_source_mesh_pref_key
    ON mesh_business (source_id, mesh_code, prefecture_code);
