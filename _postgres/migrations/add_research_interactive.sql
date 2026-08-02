-- Interactive deep research: scout → propose → approve → dig → answer → report.
--
-- Boards gain three transient statuses (scouting/awaiting/reporting) and a
-- regeneratable markdown report. Threads gain a 'proposed' status (waiting for
-- user approval), per-thread user guidance, a synthesized markdown answer, and
-- persisted discovery hints (hints used to ride along in-process inside one
-- build job; with proposals dug in a later job they must survive on the row).
--
-- guidance/answer/hints/report are content fields: encrypted ("v1:..." blobs)
-- on enc boards, plaintext otherwise. answer_depth is numeric bookkeeping and
-- stays plaintext (same policy as depth).

ALTER TABLE research_boards DROP CONSTRAINT IF EXISTS research_boards_status_check;
ALTER TABLE research_boards ADD CONSTRAINT research_boards_status_check
    CHECK (status IN ('new', 'scouting', 'awaiting', 'digging', 'reporting', 'ready', 'failed'));
ALTER TABLE research_boards ADD COLUMN IF NOT EXISTS report TEXT;
ALTER TABLE research_boards ADD COLUMN IF NOT EXISTS report_generated_at TIMESTAMPTZ;

ALTER TABLE research_threads DROP CONSTRAINT IF EXISTS research_threads_status_check;
ALTER TABLE research_threads ADD CONSTRAINT research_threads_status_check
    CHECK (status IN ('proposed', 'active', 'archived'));
ALTER TABLE research_threads ADD COLUMN IF NOT EXISTS guidance TEXT;
ALTER TABLE research_threads ADD COLUMN IF NOT EXISTS answer TEXT;
ALTER TABLE research_threads ADD COLUMN IF NOT EXISTS answer_depth INTEGER NOT NULL DEFAULT 0;
ALTER TABLE research_threads ADD COLUMN IF NOT EXISTS hints JSONB NOT NULL DEFAULT '[]';
