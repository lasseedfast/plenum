-- Migration: deep-research boards/threads + background job infrastructure
-- Run: psql -U riksdagen -d riksdagen -f _postgres/migrations/add_research.sql

-- One board per research topic. `revision` is bumped on every merge so the
-- frontend can cheaply detect change while polling.
CREATE TABLE IF NOT EXISTS research_boards (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    title         TEXT        NOT NULL,
    topic         TEXT        NOT NULL,
    intro         TEXT,
    -- Per-browser owner (the X-Session-Id localStorage UUID). Boards are only
    -- listed/accessed for their owner — the app has no login, so this is the
    -- same identity model chat uses.
    owner_session TEXT,
    status        TEXT        NOT NULL DEFAULT 'new'
                  CHECK (status IN ('new', 'digging', 'ready', 'failed')),
    revision      INTEGER     NOT NULL DEFAULT 1,
    target_depth  INTEGER     NOT NULL DEFAULT 3,
    logic_version INTEGER     NOT NULL DEFAULT 1,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS research_boards_owner_idx ON research_boards (owner_session);

-- Threads are rows (not a board-level JSONB doc) so the background dig and
-- user-seeded threads never overwrite each other. `depth` = trips completed.
CREATE TABLE IF NOT EXISTS research_threads (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    board_id       UUID        NOT NULL REFERENCES research_boards(id) ON DELETE CASCADE,
    title          TEXT        NOT NULL,
    question       TEXT        NOT NULL,
    why            TEXT        NOT NULL DEFAULT '',
    origin         TEXT        NOT NULL DEFAULT 'auto' CHECK (origin IN ('auto', 'seed')),
    depth          INTEGER     NOT NULL DEFAULT 0,
    status         TEXT        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    pinned         BOOLEAN     NOT NULL DEFAULT FALSE,
    findings       JSONB       NOT NULL DEFAULT '[]',
    open_questions JSONB       NOT NULL DEFAULT '[]',
    leads          JSONB       NOT NULL DEFAULT '[]',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS research_threads_board_idx ON research_threads (board_id);

-- Out-of-process background jobs. Progress is written by the child process;
-- any API worker can serve a poll. No secrets are ever stored here.
CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT        PRIMARY KEY,
    kind              TEXT        NOT NULL,
    board_id          UUID        REFERENCES research_boards(id) ON DELETE CASCADE,
    status            TEXT        NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running', 'done', 'failed', 'cancelled')),
    params            JSONB       NOT NULL DEFAULT '{}',
    progress          JSONB       NOT NULL DEFAULT '{}',
    counts            JSONB       NOT NULL DEFAULT '{}',
    errors            JSONB       NOT NULL DEFAULT '[]',
    event_count       INTEGER     NOT NULL DEFAULT 0,
    cancel_requested  BOOLEAN     NOT NULL DEFAULT FALSE,
    pid               INTEGER,
    host              TEXT,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at       TIMESTAMPTZ,
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status);
CREATE INDEX IF NOT EXISTS jobs_board_idx ON jobs (board_id);

CREATE TABLE IF NOT EXISTS job_events (
    job_id TEXT        NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    seq    INTEGER     NOT NULL,
    event  JSONB       NOT NULL,
    ts     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (job_id, seq)
);
