-- 015_specialties.sql
-- 標榜診療科目と診療時間。
--
-- これまで competition は「商圏内の歯科医院の数」だけでした。小児歯科を
-- やろうとしている人にとって、小児歯科を標榜していない医院は同じ重さの競合
-- ではありません。医療情報ネットの 032 ファイルは施設ごとの標榜科目と曜日別
-- 診療時間を公表しているので、それを取り込んで競合を科目で絞れるようにします。
--
-- 3 つのテーブル:
--
--   facility_specialties  公表された 1 行 = 1 (施設, 診療科目)
--   facility_hours        公表された 1 行 = 1 (施設, 科目, 時間帯, 曜日)
--   facility_features     上の 2 つから作る施設ごとの要約（配列と真偽値）
--
-- facilities に列を足さないのは、031 の取り込みが
-- `DELETE FROM facilities WHERE source_id = ...` で全置換するためです。
-- 施設ファイルを入れ直すたびに科目が消え、しかも成功と表示されます。
-- 別テーブルなら取り込み順に依存しません。
--
-- 外部キーを facilities に張らないのも同じ理由です。032 にあって 031 に無い
-- 施設（今回の抽出で約 1.5%）で取り込み全体が落ちるより、突き合わせ率を
-- 数えて報告するほうが正直です。

CREATE TABLE IF NOT EXISTS facility_specialties (
    id              bigserial PRIMARY KEY,
    source_id       text NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    facility_id     text NOT NULL,            -- facilities.facility_id と同じ公表 ID
    specialty_code  text NOT NULL,            -- 08001 など。08991 は自由記載
    specialty_name  text NOT NULL,            -- 公表された名称のまま
    specialty_key   text NOT NULL,            -- 正規化キー: general / pediatric / ...
    is_free_text    boolean NOT NULL DEFAULT false,
    source_date     date,
    last_updated    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, facility_id, specialty_code, specialty_name)
);

CREATE INDEX IF NOT EXISTS facility_specialties_facility_idx
    ON facility_specialties (facility_id);
CREATE INDEX IF NOT EXISTS facility_specialties_key_idx
    ON facility_specialties (specialty_key);

CREATE TABLE IF NOT EXISTS facility_hours (
    id                bigserial PRIMARY KEY,
    source_id         text NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    facility_id       text NOT NULL,
    specialty_code    text NOT NULL,
    time_band         smallint NOT NULL,      -- 1..3（午前・午後・夜間の別ではなく、単に何枠目か）
    weekday           smallint NOT NULL,      -- 1=月 .. 7=日, 8=祝
    opens             time,
    closes            time,
    reception_opens   time,
    reception_closes  time,
    source_date       date,
    UNIQUE (source_id, facility_id, specialty_code, time_band, weekday)
);

CREATE INDEX IF NOT EXISTS facility_hours_facility_idx ON facility_hours (facility_id);

-- 施設ごとの要約。分析のたびに時間表を畳み直さないために持ちます。
-- 中身はすべて上の 2 テーブルから決まる導出値で、取り込みのたびに作り直します。
CREATE TABLE IF NOT EXISTS facility_features (
    facility_id           text PRIMARY KEY,
    source_id             text NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    specialty_keys        text[] NOT NULL DEFAULT '{}',   -- 重複を除いた正規化キー
    declared_specialties  text[] NOT NULL DEFAULT '{}',   -- 公表された名称のまま
    open_days             smallint,        -- 診療時間の記載がある曜日数（祝日を含む）
    weekly_open_hours     double precision,-- 週あたりの診療時間の合計
    latest_close          time,            -- いちばん遅い終了時刻
    opens_saturday        boolean NOT NULL DEFAULT false,
    opens_sunday          boolean NOT NULL DEFAULT false,
    opens_holiday         boolean NOT NULL DEFAULT false,
    opens_evening         boolean NOT NULL DEFAULT false,  -- 設定の evening_from 以降まで
    source_date           date,
    last_updated          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS facility_features_keys_idx
    ON facility_features USING GIN (specialty_keys);


-- ---------------------------------------------------------------------------
-- 商圏分析に科目の内訳を足す。
--
-- 返す値が 5 つ増えます:
--
--   facility_specialty_counts    商圏内の医院を科目キーごとに数えたもの
--   facility_hours_counts        土日祝・夜間に開いている医院の数
--   facility_specialty_count     p_specialty を標榜する医院の数（指定時のみ）
--   facilities_with_specialty_data  科目データがある医院の数（＝上の分母）
--   facility_weekly_hours_median 商圏内の医院の週間診療時間の中央値
--
-- 分母を返すのが肝心です。今の抽出は東京都の歯科診療所の一部しか含まないので、
-- 「小児歯科 3 件」が「3 件しかない」なのか「3 件しか分かっていない」なのかは、
-- 分母が無いと区別できません。
-- 既知の署名をすべて落としてから作り直します。マイグレーションの修復は
-- 009 以降をファイル順に流し直すので、あとの版が作った 7 引数版が残っていると
-- CREATE が `already exists with same argument types` で落ちるか、引数の数だけ
-- 違う多重定義ができて呼び出しが曖昧になります。
DROP FUNCTION IF EXISTS kg_analyze_point(
    double precision, double precision, double precision, text, integer, text);
DROP FUNCTION IF EXISTS kg_analyze_point(
    double precision, double precision, double precision, text, integer,
    text, text);

CREATE FUNCTION kg_analyze_point(
    p_lat               double precision,
    p_lng               double precision,
    p_radius_m          double precision,
    p_facility_category text    DEFAULT 'dental_clinic',
    p_mesh_size_m       integer DEFAULT 1000,
    p_catchment         text    DEFAULT 'circle',
    p_specialty         text    DEFAULT NULL
)
RETURNS TABLE (
    population                     double precision,
    age_0_14                       double precision,
    age_15_64                      double precision,
    age_65_plus                    double precision,
    households                     double precision,
    population_growth              double precision,
    mesh_count                     integer,
    workers                        double precision,
    establishments                 double precision,
    worker_mesh_count              integer,
    facility_count                 integer,
    population_per_facility        double precision,
    workers_per_facility           double precision,
    facility_specialty_counts      jsonb,
    facility_hours_counts          jsonb,
    facility_specialty_count       integer,
    facilities_with_specialty_data integer,
    facility_weekly_hours_median   double precision,
    land_price_yen_per_sqm         double precision,
    land_price_points              integer,
    land_price_basis               text,
    catchment_kind                 text,
    catchment_area_km2             double precision,
    nearest_facility_id            bigint,
    nearest_facility_name          text,
    nearest_facility_distance_m    double precision,
    nearest_station_id             bigint,
    nearest_station_name           text,
    nearest_station_distance_m     double precision,
    nearest_station_passengers     integer
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
    -- 商圏内の医院を 1 度だけ拾い、件数・科目内訳・診療時間をここから作ります。
    -- 以前は件数だけを数えていました。同じ集合を 3 回引き直すと、メッシュ全件の
    -- 掃引で 3 倍の時間がかかります。
    fac_rows AS (
        SELECT ff.facility_id IS NOT NULL          AS has_features,
               COALESCE(ff.specialty_keys, '{}')   AS keys,
               ff.weekly_open_hours,
               COALESCE(ff.opens_saturday, false)  AS sat,
               COALESCE(ff.opens_sunday, false)    AS sun,
               COALESCE(ff.opens_holiday, false)   AS hol,
               COALESCE(ff.opens_evening, false)   AS eve
        FROM facilities f
        LEFT JOIN facility_features ff ON ff.facility_id = f.facility_id
        WHERE f.facility_category = p_facility_category
          AND ((v_kind = 'walk' AND f.geom && v_buf AND ST_Intersects(f.geom, v_buf))
            OR (v_kind <> 'walk'
                AND ST_DWithin(f.geom::geography, v_point::geography, p_radius_m)))
    ),
    fac AS (
        SELECT
            COUNT(*)::integer                                        AS facility_count,
            COUNT(*) FILTER (WHERE fr.has_features)::integer         AS with_specialty_data,
            CASE WHEN p_specialty IS NOT NULL
                 THEN COUNT(*) FILTER (WHERE p_specialty = ANY(fr.keys))::integer END
                                                                     AS specialty_count,
            jsonb_build_object(
                'declared', COUNT(*) FILTER (WHERE fr.weekly_open_hours IS NOT NULL),
                'saturday', COUNT(*) FILTER (WHERE fr.sat),
                'sunday',   COUNT(*) FILTER (WHERE fr.sun),
                'holiday',  COUNT(*) FILTER (WHERE fr.hol),
                'evening',  COUNT(*) FILTER (WHERE fr.eve)
            )                                                        AS hours_counts,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY fr.weekly_open_hours)
                                                                     AS weekly_hours_median
        FROM fac_rows fr
    ),
    spec AS (
        SELECT COALESCE(jsonb_object_agg(t.key, t.n), '{}'::jsonb) AS counts
        FROM (
            SELECT k AS key, COUNT(*)::integer AS n
            FROM fac_rows fr, unnest(fr.keys) AS k
            GROUP BY k
        ) t
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
        spec.counts, fac.hours_counts, fac.specialty_count,
        fac.with_specialty_data, fac.weekly_hours_median,
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
    CROSS JOIN spec
    LEFT JOIN nearest_fac ON true
    LEFT JOIN nearest_stn ON true;
END;
$$;


-- 半径ごとの件数を標榜科目で絞った版。データセットの「500m に n 件、1km に m 件」
-- を科目別にも出すためのものです。
--
-- kg_facility_counts に引数を足す形にはしません。引数を足すと CREATE OR REPLACE
-- が置き換えではなく多重定義になり、既定値つきの 5 引数版と元の 4 引数版が並んで、
-- 4 引数の呼び出しがどちらとも解釈できなくなります
-- （`function ... is not unique`）。しかも 005_functions.sql は 4 引数版を
-- 作り直すので、その状態はマイグレーションを流し直すたびに戻ってきます。
-- 名前を分ければ、どの順で流し直しても曖昧になりません。
CREATE OR REPLACE FUNCTION kg_facility_counts_by_specialty(
    p_lat               double precision,
    p_lng               double precision,
    p_radii             double precision[],
    p_facility_category text,
    p_specialty         text
)
RETURNS TABLE (radius_m double precision, facility_count integer)
LANGUAGE sql STABLE AS $$
    SELECT r AS radius_m,
           (SELECT COUNT(*)::integer
              FROM facilities f
              JOIN facility_features ff ON ff.facility_id = f.facility_id
             WHERE f.facility_category = p_facility_category
               AND p_specialty = ANY(ff.specialty_keys)
               AND ST_DWithin(
                     f.geom::geography,
                     ST_SetSRID(ST_MakePoint(p_lng, p_lat), 4326)::geography,
                     r))
    FROM unnest(p_radii) AS r;
$$;


-- ランキングにも科目の内訳を持たせます。分析をやり直さずに一覧へ出すため。
ALTER TABLE mesh_scores ADD COLUMN IF NOT EXISTS facility_specialty_counts jsonb;
ALTER TABLE mesh_scores ADD COLUMN IF NOT EXISTS facility_specialty_count integer;
