-- Migration: eval harness tables
-- Stores generated questions, compact tool traces, and per-paragraph judge verdicts.

CREATE TABLE IF NOT EXISTS eval_runs (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at    TIMESTAMPTZ,
    label          TEXT,
    config         JSONB,
    num_questions  INT         NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS eval_questions (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID        REFERENCES eval_runs(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    question        TEXT        NOT NULL,
    question_type   TEXT,
    answer          TEXT,
    tool_trace      JSONB,
    sources         JSONB,
    num_iterations  INT,
    duration_ms     INT,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS eval_judgments (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    question_id     UUID        REFERENCES eval_questions(id) ON DELETE CASCADE,
    paragraph_idx   INT,
    paragraph_text  TEXT,
    cited_indices   INT[],
    verdict         TEXT,
    rationale       TEXT,
    judge_model     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_eval_questions_run ON eval_questions(run_id);
CREATE INDEX IF NOT EXISTS idx_eval_judgments_q ON eval_judgments(question_id);

-- Patch: add complexity column (1 = single question, 2-3 = combined)
ALTER TABLE eval_questions ADD COLUMN IF NOT EXISTS complexity INT;

-- Patch: add metadata_mismatch column — populated by the deterministic pre-check in the
-- harness when a speaker/party in the paragraph doesn't match cited source metadata.
-- NULL means no mismatch detected (or paragraph had no extractable speaker link).
ALTER TABLE eval_judgments ADD COLUMN IF NOT EXISTS metadata_mismatch TEXT;

-- Patch: cross-encoder coverage score (0.0–1.0 sigmoid probability).
-- Measures how well all cited sources together support the paragraph claim.
-- NULL when the scorer endpoint is unavailable.
ALTER TABLE eval_judgments ADD COLUMN IF NOT EXISTS coverage_score FLOAT;
