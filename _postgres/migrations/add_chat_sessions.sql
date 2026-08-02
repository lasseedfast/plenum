-- Migration: add chat_sessions table
-- Run: psql -U riksdagen -d riksdagen -f _postgres/migrations/add_chat_sessions.sql

CREATE TABLE IF NOT EXISTS chat_sessions (
    id              UUID        PRIMARY KEY,
    session_type    TEXT        NOT NULL CHECK (session_type IN ('general', 'mp')),
    intressent_id   TEXT        REFERENCES people(intressent_id),
    initial_talk_id TEXT,
    llm_messages    JSONB       NOT NULL DEFAULT '[]',
    turns           JSONB       NOT NULL DEFAULT '[]',
    focus_ids       TEXT[]      NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_activity   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS chat_sessions_last_activity_idx ON chat_sessions (last_activity);
