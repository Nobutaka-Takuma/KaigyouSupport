-- 利用者アカウントと、月あたりのレポート生成回数の上限。
--
-- 分析 1 本で LLM の課金が発生します（実測 $1.2 前後）。払うのは運営者で、
-- 利用者には月額で請求します。だから **誰が何回使ったか**を数えられないと、
-- 請求も原価管理も成り立ちません。
--
-- 認証そのものは Supabase Auth に任せます（auth.users）。ここに持つのは、
-- 認証では分からないこと ―― 契約しているプランと、上限と、止めるかどうか
-- ―― だけです。認証情報を二重に持つと、片方だけ消えたときに直せません。
CREATE TABLE IF NOT EXISTS accounts (
    -- Supabase Auth のユーザー ID（auth.users.id）。外部キーは張りません。
    -- auth スキーマは Supabase の管理下で、ローカルの Postgres には
    -- 存在しないためです。同じスキーマがどちらでも動くことを優先します。
    user_id         text PRIMARY KEY,
    email           text,
    display_name    text,
    organisation    text,
    -- 月あたりのレポート生成回数。0 なら停止（作成させない）。
    monthly_quota   integer NOT NULL DEFAULT 0,
    -- 請求の締め日。「毎月1日」ではなく契約日を基準にしたいことがあるので、
    -- 日付だけ持ちます（1〜28。29〜31 は月によって存在しません）。
    billing_day     integer NOT NULL DEFAULT 1
                    CHECK (billing_day BETWEEN 1 AND 28),
    status          text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'suspended')),
    -- 運営者用。管理画面を開けるのはこれが true のアカウントだけ。
    is_admin        boolean NOT NULL DEFAULT false,
    note            text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- 利用実績は analysis_jobs から数えます。別に集計表を持つと、二重計上と
-- 数え漏れの両方が起きます。取り下げたジョブを数えないなど、数え方を
-- 変えたくなったときも 1 か所で済みます。
CREATE INDEX IF NOT EXISTS analysis_jobs_user_created_idx
    ON analysis_jobs (user_id, created_at DESC);
