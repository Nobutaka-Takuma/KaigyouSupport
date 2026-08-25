-- 5 段目（顧客提出用レポート）を許す。
--
-- STEP4 までは根拠を辿れる形（タグと id）で作ります。それは検算のための形で
-- あって、人が読むための形ではありません。[FACT] が20個並んだ文書は、読み手に
-- 「自分で要約してください」と言っているのと同じです。顧客に渡す文書は、
-- そこから散文に起こし直します。
--
-- 段数を DDL に固定していたので、増やすとここで落ちます。上限を外して
-- 「1 以上」だけにしておけば、次に段を足すときにマイグレーションが要りません。
ALTER TABLE analysis_steps DROP CONSTRAINT IF EXISTS analysis_steps_step_number_check;
ALTER TABLE analysis_steps ADD CONSTRAINT analysis_steps_step_number_check
    CHECK (step_number >= 1);
