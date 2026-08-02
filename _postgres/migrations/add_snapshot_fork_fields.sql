ALTER TABLE chat_snapshots
  ADD COLUMN IF NOT EXISTS llm_messages     JSONB,
  ADD COLUMN IF NOT EXISTS focus_ids        TEXT[] DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS initial_talk_id  TEXT;
