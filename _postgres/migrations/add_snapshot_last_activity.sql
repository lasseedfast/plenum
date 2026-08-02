ALTER TABLE chat_snapshots
  ADD COLUMN IF NOT EXISTS last_activity TIMESTAMPTZ DEFAULT NOW();

UPDATE chat_snapshots SET last_activity = created_at WHERE last_activity IS NULL;

CREATE INDEX IF NOT EXISTS chat_snapshots_last_activity_idx
  ON chat_snapshots (last_activity);
