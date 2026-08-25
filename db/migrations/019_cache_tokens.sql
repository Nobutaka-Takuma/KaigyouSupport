-- キャッシュの読み書きを別に数える。
--
-- 実測：STEP2（Web検索あり）の入力が 307,754 トークンでした。1 回の呼び出しの
-- 中でサーバ側の検索ループが回り、そのたびに増えていく文脈を読み直すためです。
-- 入力トークンをひとつの数字にまとめてしまうと、その内訳（毎回読み直している
-- ぶんがどれだけか）が分からず、キャッシュが効いているのかも判定できません。
--
-- 単価が違うので、費用の計算にも要ります（読み出しは入力の約0.1倍、
-- 書き込みは約1.25倍）。
ALTER TABLE analysis_steps ADD COLUMN IF NOT EXISTS cache_read_tokens integer;
ALTER TABLE analysis_steps ADD COLUMN IF NOT EXISTS cache_write_tokens integer;
