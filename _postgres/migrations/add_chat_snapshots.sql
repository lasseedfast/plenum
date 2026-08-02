-- Migration: add chat_snapshots table (read-only shareable chat snapshots)
-- Run: docker exec -i riksdagen-pg psql -U riksdagen -d riksdagen < _postgres/migrations/add_chat_snapshots.sql

CREATE TABLE IF NOT EXISTS chat_snapshots (
    id              UUID        PRIMARY KEY,
    session_type    TEXT        NOT NULL CHECK (session_type IN ('general', 'mp')),
    intressent_id   TEXT,       -- for MP chats: used to show name/party in the snapshot view
    turns           JSONB       NOT NULL DEFAULT '[]',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
