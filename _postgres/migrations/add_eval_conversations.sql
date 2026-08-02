CREATE TABLE IF NOT EXISTS eval_conversations (
    id          BIGSERIAL    PRIMARY KEY,
    session_id  TEXT         NOT NULL,
    turn_index  INT          NOT NULL,
    stream      BOOLEAN      NOT NULL DEFAULT FALSE,
    started_at  TIMESTAMPTZ  NOT NULL,
    finished_at TIMESTAMPTZ  NOT NULL,
    duration_s  DOUBLE PRECISION,
    iterations  INT,
    has_error   BOOLEAN      NOT NULL DEFAULT FALSE,
    doc         JSONB        NOT NULL
);

CREATE INDEX IF NOT EXISTS eval_conversations_session_idx ON eval_conversations (session_id, started_at);
