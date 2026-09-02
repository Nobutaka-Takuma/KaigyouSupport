-- 母集団の分布を、あらかじめ計算して置いておく。
--
-- ---------------------------------------------------------------------------
-- なぜこの表が要るのか
--
-- 実測（銀座1km・手元の PostgreSQL）：
--
--   build_dataset 全体 127ms / SQL 59 往復
--     measures.measure_scope_shape   8 往復  18.0ms
--     measures._scope_statistics     6 往復  31.3ms
--     ...
--     analysis.analyze_point         3 往復  14.7ms   ← 商圏の集計そのもの
--
-- **母集団の形を測るのに 14 往復・49ms、商圏の集計そのものは 15ms。**
-- 「この地点が何人か」より「周りがどうなっているか」のほうが 3 倍高い。
--
-- そして周りは、**クリックした地点によって変わりません。** 静岡県内の市街地の
-- 人口分布は、どこをクリックしても同じです。地点ごとに測り直していました。
--
-- ---------------------------------------------------------------------------
-- 何を保存して、何を保存しないか
--
-- 保存するのは**分布**であって、地点ごとの評価ではありません。
--
-- 利用者は任意の座標をクリックします。その商圏はどのメッシュの商圏とも
-- 一致しません。メッシュごとに評価を貯めても、**答えられるのは「いちばん近い
-- メッシュの評価」**で、それは境界で間違い、しかも間違いを説明できません。
--
-- 分布さえあれば、任意の値の位置は二分探索で出ます。SQL の往復はゼロです。
--
--   count(*) FILTER (WHERE column <= 12430)   ← これだけが地点に依存する
--   percentile_cont(...)                      ← これは地点に依存しない
--
-- 依存しないほうを保存します。
--
-- ---------------------------------------------------------------------------
-- 入らない母集団が 2 つあります
--
--   nearby             この地点から10km以内   ← 地点で母集団が変わる
--   similar_population 商圏人口が同規模        ← 地点で母集団が変わる
--
-- **この 2 つは事前計算できません。** その場で測ります。入れないことを明示
-- するために、ここに書いておきます（「なぜ無いのか」を後から探させない）。

CREATE TABLE IF NOT EXISTS benchmark_distributions (
    -- 母集団の種類。measures.BenchmarkScope.type と同じ語。
    scope_kind        text NOT NULL,
    -- その種類の中で母集団を一意に決める鍵。
    --   都道府県単位（prefecture / urban / with_clinics / station_front）… '22'
    --   市区町村単位（municipality / neighbourhood）                      … '22:沼津市'
    scope_key         text NOT NULL,
    -- 人に見せる名前。**これを書かずに「上位8%」と言えば、それはもう統計では
    -- ありません。** 読む側が毎回組み立て直さずに済むよう、一緒に持ちます。
    scope_label       text NOT NULL,

    metric            text NOT NULL,   -- measures.MEASURE_SPECS のキー
    profile           text NOT NULL,
    radius_m          integer NOT NULL,
    facility_category text NOT NULL,

    -- 母集団のメッシュ数と、その指標が NULL でなかった数。**2 つは違います。**
    -- 地価のように大半が NULL の指標を、母集団の大きさで割ると percentile が
    -- 狂います。
    sample_count      integer NOT NULL,
    value_count       integer NOT NULL,

    -- **この表の本体。** 昇順に並んだ値。
    --
    -- value_count が小さいときは**全部**入れます（is_exact = true）。順位も
    -- percentile も厳密に出ます。大きいときは分位点の格子に落とします。
    -- 「5,448 件中 1 位」を「上位0.1%」と言えるかどうかは、この違いです。
    boundaries        double precision[] NOT NULL,
    is_exact          boolean NOT NULL,

    median            double precision,
    p25               double precision,
    p75               double precision,

    -- この母集団で「高い・低い」を語れるか。**語れない母集団があります。**
    -- 県内全メッシュのように大半が無人なら、町の中心はどこでも上位に来ます。
    -- 順位は事実なので出しますが、評価は付けません。
    --
    -- これは母集団の性質で、指標ごとには変わりません。行に持たせているのは、
    -- 読むときに表を 2 つ引かせないためです（1 リクエスト 1 往復で済ませたい）。
    discriminating    boolean NOT NULL DEFAULT true,
    not_discriminating_reason text,
    share_below_viable_floor  double precision,

    -- 再現性（指示書 §19）。データが更新されても、この版で計算した結果が
    -- どういうものだったかを後から言えるように。
    benchmark_version text NOT NULL,
    data_version      text,
    computed_at       timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (scope_kind, scope_key, metric, profile, radius_m, facility_category)
);

-- 1 リクエストで引くのは「この profile・この radius・この地点に関係する
-- 母集団すべて」です。**1 往復で取り切れる形にします。**
CREATE INDEX IF NOT EXISTS benchmark_distributions_lookup
    ON benchmark_distributions (profile, radius_m, facility_category, scope_key);
