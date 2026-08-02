CREATE TABLE IF NOT EXISTS llm_events (
    id         BIGSERIAL    PRIMARY KEY,
    created_at TIMESTAMPTZ  DEFAULT NOW(),
    event_type TEXT         NOT NULL,
    detail     JSONB
);

CREATE INDEX IF NOT EXISTS llm_events_type_idx    ON llm_events (event_type);
CREATE INDEX IF NOT EXISTS llm_events_created_idx ON llm_events (created_at DESC);
