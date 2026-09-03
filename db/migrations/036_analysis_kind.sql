-- 分析の種類。**同じジョブの表に、別の段構成を持つ分析が入ります。**
--
-- これまでは 1 種類しかありませんでした（周辺一般の分析：FACT → 外部調査 →
-- 需要形成 → レポート）。競合分析は段の数も中身も違います
-- （競合ごとの Web 調査 → 集計 → 競争環境の要約）。
--
-- 表を分けない理由：ジョブの状態管理・再実行・使用量の記録・アカウントの
-- 枠は、種類が違っても同じものが要ります。**そこを 2 つに増やすと、片方
-- だけ直したときに黙って挙動が変わります。**
--
-- 既存の行は 'area'（周辺一般）になります。読む側は kind を見て段の構成を
-- 選びます。

ALTER TABLE analysis_jobs
    ADD COLUMN IF NOT EXISTS analysis_kind text NOT NULL DEFAULT 'area';

-- 待ち行列は種類を問わず古い順に拾います。種類で絞って一覧を出す画面の
-- ためだけの索引です。
CREATE INDEX IF NOT EXISTS analysis_jobs_kind
    ON analysis_jobs (analysis_kind, created_at DESC);
