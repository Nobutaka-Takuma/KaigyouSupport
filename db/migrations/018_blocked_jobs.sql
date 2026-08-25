-- 「未実装のステップで止まった Job」を、失敗とも待機とも別の状態にする。
--
-- それまでは queued に戻していました。失敗ではないので待ち行列に戻すのは
-- 筋なのですが、worker は queued を古い順に拾うので、同じ Job を拾っては
-- 同じところで止まる、を繰り返します。`--poll` で常駐させると 5 秒ごとに
-- それが起きますし、後から作った Job はいつまでも順番が回ってきません。
--
-- blocked は「材料は揃っているが、続きを実装していない」状態です。worker は
-- 拾いません。起動時に、止まっているステップが実装済みになっていれば
-- 自動で queued へ戻します（kaigyou_intel.jobs.requeue_unblocked）。
ALTER TABLE analysis_jobs DROP CONSTRAINT IF EXISTS analysis_jobs_status_check;
ALTER TABLE analysis_jobs ADD CONSTRAINT analysis_jobs_status_check
    CHECK (status IN ('queued','running','blocked','completed','failed','cancelled'));
