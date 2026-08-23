-- 013_land_prices.sql
-- 地価公示（国土数値情報 L01）の標準地。
--
-- What this is: the price the Land Ministry publishes for a specific surveyed
-- parcel, per square metre, as of 1 January. It is an official figure for that
-- parcel, not a valuation of the neighbourhood and not a rent.
--
-- What it is NOT, and must not be turned into: an estimate of what a practice
-- would pay. Rent depends on the building, the floor, the frontage and the
-- contract, none of which are here, and the requirements rule out predicting
-- it. So this is stored and displayed as what it is -- the published price of
-- the nearest surveyed points -- and it feeds no score.
--
-- One row per (point, year). 地価公示 is published annually and the parcels are
-- stable, so keeping the year in the key lets a later year arrive alongside
-- this one instead of replacing it.

CREATE TABLE IF NOT EXISTS land_prices (
    id                   bigserial PRIMARY KEY,
    source_id            text NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    -- 標準地番号: municipality + use division + serial, e.g. 13101-000-001.
    point_code           text NOT NULL,
    survey_year          integer NOT NULL,
    prefecture_code      text NOT NULL,
    municipality_code    text,
    municipality_name    text,
    address              text,
    geom                 geometry(Point, 4326) NOT NULL,
    -- 円/m²。The published figure, unrounded and unconverted.
    price_yen_per_sqm    bigint NOT NULL,
    -- 対前年変動率（%）。NULL where the point is newly surveyed.
    change_rate_pct      double precision,
    -- 用途区分。The label and the published code, because the code is the
    -- stable thing and the label is what a reader wants to see.
    use_category         text,
    use_category_code    text,
    current_use          text,          -- 利用の現況
    zoning               text,          -- 用途地域
    building_coverage_pct integer,      -- 建蔽率
    floor_area_ratio_pct integer,       -- 容積率
    nearest_station      text,
    station_distance_m   integer,
    source_date          date,
    last_updated         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, point_code, survey_year)
);

CREATE INDEX IF NOT EXISTS land_prices_geom_idx ON land_prices USING gist (geom);
CREATE INDEX IF NOT EXISTS land_prices_pref_idx ON land_prices (prefecture_code, survey_year);
CREATE INDEX IF NOT EXISTS land_prices_use_idx  ON land_prices (use_category_code);
