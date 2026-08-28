-- 成長スコアの根拠を、実績から将来推計に切り替える。
--
-- これまで mesh_scores に残していたのは population_growth（2015→2020 の実績）
-- だけでした。スコアの根拠は後から辿れる必要があるので、実際に採点に使った
-- 値を残します。実績のほうも消しません。「これまで」と「これから」は
-- 別の事実で、レポートには両方載ります。
ALTER TABLE mesh_scores
    ADD COLUMN IF NOT EXISTS population_change_projected double precision;

COMMENT ON COLUMN mesh_scores.population_change_projected IS
    '将来推計人口による変化率（config/scoring.yaml の growth.from_year → to_year）。'
    'population_growth は 2015→2020 の実績で、別物。';
