CREATE TABLE IF NOT EXISTS error_log (
    id            BIGSERIAL    PRIMARY KEY,
    fingerprint   TEXT         NOT NULL UNIQUE,
    error_type    TEXT         NOT NULL,
    first_seen_at TIMESTAMPTZ  DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ  DEFAULT NOW(),
    count         INT          NOT NULL DEFAULT 1,
    model         TEXT,
    traceback     TEXT,
    detail        JSONB
);

CREATE INDEX IF NOT EXISTS error_log_error_type_idx ON error_log (error_type);
CREATE INDEX IF NOT EXISTS error_log_last_seen_idx  ON error_log (last_seen_at DESC);

-- Enable filtering llm_events by model
CREATE INDEX IF NOT EXISTS llm_events_model_idx ON llm_events ((detail->>'model'));
