-- 016_benchmark_columns.sql
-- ベンチマークに必要な列を、採点済みメッシュにも持たせる。
--
-- データセットは各統計に「周辺と比べてどうか」を付けます。percentile と順位を
-- 出すには、その指標を全メッシュぶん同じ条件で並べたものが要ります。mesh_scores
-- はスコアの材料しか持っていなかったので、従業者数・事業所数・15〜64歳人口には
-- 比較相手がありませんでした。
--
-- metric_distributions の p05/p50/p95 で代用することもできますが、それでは
-- 「上位6%」も「5,448メッシュ中327位」も出せません（3点しか分からないため）。
-- 順位を出せない指標だけ黙って percentile を欠かすと、読み手はその指標が
-- 平凡なのだと受け取ります。列を足すほうが安いし、正直です。
ALTER TABLE mesh_scores ADD COLUMN IF NOT EXISTS age_15_64 double precision;
ALTER TABLE mesh_scores ADD COLUMN IF NOT EXISTS workers double precision;
ALTER TABLE mesh_scores ADD COLUMN IF NOT EXISTS establishments double precision;

-- 「同規模の商圏と比べて」を出すための索引。人口が近いメッシュだけを集めます。
CREATE INDEX IF NOT EXISTS mesh_scores_population_idx
    ON mesh_scores (profile, radius_m, population);
