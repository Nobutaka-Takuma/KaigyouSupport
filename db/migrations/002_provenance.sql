-- 002_provenance.sql
-- Data provenance and acquisition bookkeeping.
--
-- Every row of substantive data in this database points back at a row in
-- data_sources, and every attempt to obtain data is recorded in
-- acquisition_runs -- including the attempts that FAILED. The application is
-- required to surface both.

CREATE TABLE IF NOT EXISTS data_sources (
    id              text PRIMARY KEY,
    name            text NOT NULL,
    publisher       text NOT NULL,
    -- 'official' = obtained from the named publisher.
    -- 'sample'   = synthetic data generated locally for development.
    --              It is NOT real and must never be presented as real.
    dataset_kind    text NOT NULL CHECK (dataset_kind IN ('official', 'sample')),
    license         text,
    homepage_url    text,
    terms_url       text,
    notes           text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

COMMENT ON COLUMN data_sources.dataset_kind IS
    'official = real data from the publisher; sample = synthetic development data, not real';

CREATE TABLE IF NOT EXISTS acquisition_runs (
    id              bigserial PRIMARY KEY,
    source_id       text NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    step            text NOT NULL CHECK (step IN ('download', 'validate', 'transform', 'load')),
    status          text NOT NULL CHECK (status IN ('success', 'failed', 'skipped')),
    target_url      text,
    artifact_path   text,
    http_status     integer,
    bytes           bigint,
    sha256          text,
    record_count    integer,
    error_type      text,
    error_message   text,
    params          jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at      timestamptz NOT NULL,
    finished_at     timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS acquisition_runs_source_idx
    ON acquisition_runs (source_id, started_at DESC);
