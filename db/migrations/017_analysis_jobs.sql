-- 017_analysis_jobs.sql
-- 商圏インテリジェンス・エンジンの Job / Step / 出典 / レポート。
--
-- 既存の analysis_results（候補地点の分析キャッシュ）とは別物です。名前が
-- 似ているので触らないこと。あちらは1地点1行の数値キャッシュ、こちらは
-- 4ステップの推論の記録です。
--
-- 設計の要点が 3 つあります。
--
-- **ステップは独立して保存する。** Step2 が落ちても Step1 の結果は残り、
-- Step2 から再実行できます（要件 §32）。最初からやり直させると、Web検索の
-- 費用も待ち時間も丸ごともう一度かかります。
--
-- **base_data はスナップショットで持つ。** 要件 §27 は base_data_id という
-- 参照を求めていますが、参照先の表がありません。それに /api/dataset の出力は
-- スコアを再計算するたびに変わるので、参照だけ持っていると「このレポートが
-- 何を見て書かれたか」が後から再現できなくなります。§25 の根拠トレーサビリ
-- ティは、まさにそこを要求しています。だから本体を入れます。
--
-- **出典は行として持つ。** 外部事実を本文に埋め込むと、URL が本文の中の
-- 文字列になり、後から「この主張の出典は」を機械的に辿れません。

CREATE TABLE IF NOT EXISTS analysis_jobs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- 認証がまだ無いので nullable。入ったときに埋められるよう列だけ用意します。
    user_id         text,
    business_type   text NOT NULL DEFAULT 'dental_clinic',
    location_name   text,
    latitude        double precision NOT NULL,
    longitude       double precision NOT NULL,
    radius_m        integer NOT NULL DEFAULT 1000,
    profile         text,
    -- 分析の入力そのもの。これがあるので、スコアを再計算したあとでも
    -- 「このレポートは何を見て書かれたか」を再現できます。
    base_data       jsonb NOT NULL,
    -- 同一地点・同一データの再実行を見分けるため（要件 §34 Cache）。
    base_data_hash  text NOT NULL,
    status          text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','running','completed','failed','cancelled')),
    current_step    integer NOT NULL DEFAULT 0,
    error_message   text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    completed_at    timestamptz
);

CREATE INDEX IF NOT EXISTS analysis_jobs_status_idx ON analysis_jobs (status, created_at);
CREATE INDEX IF NOT EXISTS analysis_jobs_hash_idx   ON analysis_jobs (base_data_hash);


CREATE TABLE IF NOT EXISTS analysis_steps (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          uuid NOT NULL REFERENCES analysis_jobs(id) ON DELETE CASCADE,
    step_number     integer NOT NULL CHECK (step_number BETWEEN 1 AND 4),
    step_name       text NOT NULL,
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','completed','failed','skipped')),
    -- 実際に LLM へ渡したもの。プロンプトを直したときに、何を見せた結果
    -- なのかが分からないと、良くなったのか悪くなったのか判断できません。
    input_json      jsonb,
    output_json     jsonb,
    error_message   text,
    started_at      timestamptz,
    completed_at    timestamptz,
    -- 要件 §33。モデルとプロンプト版を残さないと、出力の違いが
    -- モデルのせいかプロンプトのせいか永久に分かりません。
    prompt_version  text,
    model           text,
    -- 要件 §34。1レポートいくらかかったのかを、後から数えられるように。
    input_tokens    integer,
    output_tokens   integer,
    web_searches    integer,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, step_number)
);

CREATE INDEX IF NOT EXISTS analysis_steps_job_idx ON analysis_steps (job_id, step_number);


CREATE TABLE IF NOT EXISTS analysis_sources (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          uuid NOT NULL REFERENCES analysis_jobs(id) ON DELETE CASCADE,
    step_id         uuid REFERENCES analysis_steps(id) ON DELETE CASCADE,
    -- どの PATTERN を調べていて出てきた出典か。§25 の追跡はここを通ります。
    pattern_id      text,
    url             text NOT NULL,
    title           text,
    -- 要件 §9 の優先順位。government / prefecture / municipality / ...
    source_type     text,
    published_at    date,
    retrieved_at    timestamptz NOT NULL DEFAULT now(),
    content         text,
    relevance_score double precision,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS analysis_sources_job_idx     ON analysis_sources (job_id);
CREATE INDEX IF NOT EXISTS analysis_sources_pattern_idx ON analysis_sources (job_id, pattern_id);


CREATE TABLE IF NOT EXISTS analysis_reports (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          uuid NOT NULL REFERENCES analysis_jobs(id) ON DELETE CASCADE,
    report_json     jsonb NOT NULL,
    report_markdown text,
    -- §25 の検証結果。参照が全部解決したかどうかを、レポートと一緒に保存
    -- します。壊れたときに「いつから壊れていたか」が分かるように。
    trace_ok        boolean,
    trace_problems  jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id)
);
