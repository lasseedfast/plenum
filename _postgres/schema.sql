-- Riksdagen database schema for PostgreSQL + pgvector
-- Run once: psql -U riksdagen -d riksdagen -f schema.sql

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- for ILIKE index support on prefix searches

-- ─────────────────────────────────────────
-- people
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS people (
    person_id   TEXT PRIMARY KEY,
    source_record_id       TEXT,
    source_record_guid     TEXT,
    source_id        TEXT,
    birth_year         TEXT,
    gender             TEXT,
    last_name       TEXT,
    first_name    TEXT,
    sort_name  TEXT,
    home_town            TEXT,
    party           TEXT,
    constituency        TEXT,
    status          TEXT,
    source_url  TEXT,
    image_url_small     TEXT,
    image_url_medium    TEXT,
    image_url_large    TEXT,
    assignments   JSONB,   -- nested assignment array, kept as-is
    contact_details   JSONB,   -- nested contact info array, kept as-is
    name            TEXT,
    active           BOOLEAN
);

CREATE INDEX IF NOT EXISTS people_parti_idx ON people (party);
CREATE INDEX IF NOT EXISTS people_namn_idx  ON people (name);

-- ─────────────────────────────────────────
-- speeches
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS speeches (
    -- Primary key: source_doc_id (e.g. "H40911"), called "id" in the source data
    id              TEXT PRIMARY KEY,

    -- Original UUID from riksdagen (preserved for reference after key migration)
    source_speech_id    TEXT,

    -- Core content
    text   TEXT,
    section_title  TEXT,

    -- Metadata
    sequence INTEGER,
    activity_type TEXT,
    speaker_name          TEXT,
    party           TEXT,
    person_id   TEXT,      -- references people(person_id), nullable

    -- Date fields
    date           DATE,      -- e.g. 2016-09-29
    source_datetime       TEXT,      -- original datetime string from riksdagen API
    year            INTEGER,
    session_year          INTEGER,   -- parliamentary session year (start year)

    -- Document references
    -- `source_doc_id` is the id of the protocol document this speech appeared in. It is
    -- written by the ingest step and is NOT the same as `id` above, despite `id`
    -- historically being described as "the source_doc_id" — that comment refers to the
    -- source field named "id", which happens to look like a document id.
    source_doc_id          TEXT,
    related_doc_id      TEXT,
    source_doc_number      TEXT,
    source_record_id       TEXT,
    title           TEXT,      -- debate_id/session title

    -- Debate grouping
    debate_id          TEXT,      -- e.g. "2016-09-29:56"
    is_reply          BOOLEAN,

    -- LLM-generated fields
    summary         TEXT,
    tags            TEXT[],

    -- URL fields (populated from riksdagen API, may be null)
    url_video       TEXT,
    url_session     TEXT,
    url_audio       TEXT,
    url_audio_file    TEXT,
    audio_start_seconds        INTEGER,

    -- LLM-derived. `arguments` holds extracted argument sentences; the two
    -- booleans are pipeline bookkeeping so a re-run can skip finished rows.
    arguments             TEXT[],
    arguments_corrected   BOOLEAN DEFAULT FALSE,
    tagging_failed        BOOLEAN DEFAULT FALSE,

    -- Embedding of `summary`, for debate_id-level semantic search. Distinct from
    -- `speech_chunks.embedding`, which covers the full text passage by passage.
    summary_embedding vector(384),

    -- Full-text search vector (auto-maintained by trigger)
    search_vector   TSVECTOR
);

-- Full-text search index (Swedish)
CREATE INDEX IF NOT EXISTS speeches_search_idx     ON speeches USING GIN (search_vector);

-- Filtering indexes
CREATE INDEX IF NOT EXISTS speeches_debate_idx     ON speeches (debate, sequence);
CREATE INDEX IF NOT EXISTS speeches_party_idx      ON speeches (party);
CREATE INDEX IF NOT EXISTS speeches_date_idx      ON speeches (date);
CREATE INDEX IF NOT EXISTS speeches_year_idx       ON speeches (year);
CREATE INDEX IF NOT EXISTS speeches_person_idx ON speeches (person_id);
CREATE INDEX IF NOT EXISTS speeches_speaker_idx     ON speeches USING GIN (to_tsvector('simple', coalesce(speaker_name, '')));
CREATE INDEX IF NOT EXISTS speeches_source_doc_idx      ON speeches (dok_id);

-- Semantic search over per-speech summaries (separate from speech_chunks.embedding,
-- which indexes the full text passage by passage).
CREATE INDEX IF NOT EXISTS speeches_summary_embedding_idx ON speeches
    USING hnsw (summary_embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- Trigger to keep search_vector up to date on insert/update
CREATE OR REPLACE FUNCTION talks_search_vector_update() RETURNS TRIGGER AS $$
BEGIN
    NEW.search_vector := to_tsvector('swedish', coalesce(NEW.text, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS talks_search_vector_trigger ON speeches;
CREATE TRIGGER talks_search_vector_trigger
    BEFORE INSERT OR UPDATE OF text
    ON speeches
    FOR EACH ROW
    EXECUTE FUNCTION talks_search_vector_update();

-- ─────────────────────────────────────────
-- speech_chunks  (text speech_chunks + vector embeddings)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS speech_chunks (
    -- Key: "{speech_id}:{chunk_index}", e.g. "H40911:0"
    id          TEXT PRIMARY KEY,

    speech_id     TEXT NOT NULL REFERENCES speeches(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text        TEXT NOT NULL,
    embedding   vector(384)
);

CREATE INDEX IF NOT EXISTS speech_chunks_speech_idx      ON speech_chunks (speech_id);
-- HNSW index for approximate nearest-neighbour cosine search
CREATE INDEX IF NOT EXISTS speech_chunks_embedding_idx ON speech_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ─────────────────────────────────────────
-- debates  (aggregated debate summaries)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS debates (
    -- Key: "{date}:{debate_index}", e.g. "2016-09-29:56"
    id          TEXT PRIMARY KEY,

    date           DATE,
    summary         TEXT,
    num_talks       INTEGER,
    talk_summaries  TEXT[],   -- array of individual talk summary strings
    talk_ids        TEXT[],   -- array of talk ids (without "speeches/" prefix)

    summary_embedding vector(384)
);

CREATE INDEX IF NOT EXISTS debates_date_idx ON debates (date);
CREATE INDEX IF NOT EXISTS debates_summary_embedding_idx ON debates
    USING hnsw (summary_embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- ─────────────────────────────────────────
-- documents  (motioner from riksdagens öppna data)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS documents (
    -- Primary key: doc_id (e.g. "HD02846")
    doc_id          TEXT PRIMARY KEY,

    -- Which kind of member-submitted document this is. Everything is 'motion'
    -- today; the column exists so bills, written questions and committee reports
    -- can share this table rather than each needing their own.
    doc_type        TEXT NOT NULL DEFAULT 'motion',

    source_record_id       TEXT,

    -- Identity / classification
    session_label              TEXT,       -- riksmöte, e.g. "2022/23"
    designation      TEXT,       -- motion number within session_label, e.g. "846"
    subtype          TEXT,       -- e.g. "Enskild motion", "Kommittémotion"
    committee           TEXT,       -- committee the motion was referred to, e.g. "AU"
    status          TEXT,       -- e.g. "Klar", "Inkommen"

    -- Dates
    date           DATE,
    source_updated_at     TEXT,       -- raw string, used for change detection
    published_at      TEXT,
    session_year            INTEGER,    -- int(session_label[:4]), mirrors speeches.session_year

    -- Content
    title           TEXT,
    subtitle      TEXT,
    text            TEXT,       -- plain text extracted from the html field
    proposals_text    TEXT,       -- concat of yrkande lydelser (high-signal, weighted B in FTS)
    has_text        BOOLEAN NOT NULL DEFAULT FALSE,  -- false for scanned-PDF-only documents

    -- Source URLs
    url_text TEXT,
    url_html TEXT,
    url_pdf         TEXT,

    -- Authors (denormalized; relational detail in document_authors)
    parties         TEXT[] NOT NULL DEFAULT '{}',
    author_names    TEXT[] NOT NULL DEFAULT '{}',    -- in signing order

    -- Proposals and attachments, raw from dokumentstatus
    proposals_raw         JSONB,      -- dokforslag list: yrkanden + utskottsförslag + kammarens beslut
    attachments         JSONB,      -- dokbilaga list
    num_proposals    INTEGER NOT NULL DEFAULT 0,

    -- LLM-generated fields (future parity with speeches)
    summary         TEXT,

    -- Full-text search vector (auto-maintained by trigger)
    search_vector   TSVECTOR
);

CREATE INDEX IF NOT EXISTS documents_search_idx  ON documents USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS documents_date_idx   ON documents (date);
CREATE INDEX IF NOT EXISTS documents_session_year_idx    ON documents (year);
CREATE INDEX IF NOT EXISTS documents_committee_idx   ON documents (committee);
CREATE INDEX IF NOT EXISTS documents_parties_idx ON documents USING GIN (parties);

CREATE OR REPLACE FUNCTION motions_search_vector_update() RETURNS TRIGGER AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('swedish', coalesce(NEW.title, '')), 'A') ||
        setweight(to_tsvector('swedish', coalesce(NEW.proposals_text, '')), 'B') ||
        setweight(to_tsvector('swedish', coalesce(NEW.subtitle, '')), 'C') ||
        setweight(to_tsvector('swedish', coalesce(NEW.text, '')), 'D');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS motions_search_vector_trigger ON documents;
CREATE TRIGGER motions_search_vector_trigger
    BEFORE INSERT OR UPDATE OF title, subtitle, text, proposals_text
    ON documents
    FOR EACH ROW
    EXECUTE FUNCTION motions_search_vector_update();

-- ─────────────────────────────────────────
-- document_authors  (undertecknare, in signing order)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS document_authors (
    doc_id        TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    ordinal       INTEGER NOT NULL,   -- position in dokintressent list
    -- Soft link to people(person_id); no FK — 1990s ids may be missing from people
    person_id TEXT,
    name          TEXT,
    party      TEXT,
    role          TEXT,               -- e.g. "undertecknare"
    PRIMARY KEY (doc_id, ordinal)
);

CREATE INDEX IF NOT EXISTS document_authors_person_idx ON document_authors (person_id);

-- ─────────────────────────────────────────
-- document_chunks  (text speech_chunks + vector embeddings)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS document_chunks (
    -- Key: "{dok_id}:{chunk_index}", e.g. "HD02846:0"
    id          TEXT PRIMARY KEY,

    doc_id   TEXT NOT NULL REFERENCES documents(dok_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text        TEXT NOT NULL,
    embedding   vector(384)
);

CREATE INDEX IF NOT EXISTS document_chunks_doc_idx ON document_chunks (doc_id);
CREATE INDEX IF NOT EXISTS document_chunks_embedding_idx ON document_chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- ─────────────────────────────────────────
-- document_proposals  (one condensed proposal per row + its own embedding)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS document_proposals (
    -- Key: "{doc_id}:{ordinal}", ordinal = 0-based position in the proposals_raw array
    id           TEXT PRIMARY KEY,
    doc_id       TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    ordinal      INTEGER NOT NULL,
    number       TEXT,
    text      TEXT NOT NULL,
    committee_recommendation    TEXT,        -- committee proposal (e.g. "Avslag")
    chamber_decision     TEXT,        -- chamber decision (e.g. "Avslag"/"Bifall")
    handled_in  TEXT,        -- committee report where handled
    embedding    vector(384)
);

CREATE INDEX IF NOT EXISTS document_proposals_doc_idx ON document_proposals (dok_id);
CREATE INDEX IF NOT EXISTS document_proposals_embedding_idx ON document_proposals
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- ─────────────────────────────────────────
-- users / auth_tokens  (optional zero-knowledge accounts)
-- ─────────────────────────────────────────
-- The server NEVER sees the password: the client stretches it with PBKDF2 and
-- sends only a derived auth key (stored bcrypt-hashed here) plus the DEK
-- wrapped by a client-side key. All owned content is ciphertext at rest.
CREATE TABLE IF NOT EXISTS users (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    username        TEXT        UNIQUE NOT NULL,   -- stored lowercased
    auth_hash       TEXT        NOT NULL,          -- bcrypt(client-derived auth key)
    kdf_salt        TEXT        NOT NULL,          -- base64; client PBKDF2 salt
    kdf_iterations  INTEGER     NOT NULL DEFAULT 600000,
    wrapped_dek     TEXT        NOT NULL,          -- "v1:..." DEK wrapped by client KEK
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    enc_settings    TEXT,                          -- "v1:..." AI settings (incl. API key) under the DEK
    settings_updated_at TIMESTAMPTZ
);

-- Added after the fact; kept here so an existing database picks them up.
ALTER TABLE users ADD COLUMN IF NOT EXISTS enc_settings TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS settings_updated_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash    TEXT        PRIMARY KEY,         -- sha256(token); raw token lives only in the client
    user_id       UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS auth_tokens_user_idx ON auth_tokens (user_id);

-- ─────────────────────────────────────────
-- chat_sessions  (persistent chat history)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chat_sessions (
    id              UUID        PRIMARY KEY,
    session_type    TEXT        NOT NULL CHECK (session_type IN ('general', 'mp')),
    person_id   TEXT        REFERENCES people(person_id),
    initial_speech_id TEXT,
    llm_messages    JSONB       NOT NULL DEFAULT '[]',
    turns           JSONB       NOT NULL DEFAULT '[]',
    focus_ids       TEXT[]      NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_activity   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Owned sessions (user_id set): all content lives encrypted in enc_payload
    -- (llm_messages/turns/focus_ids AND person_id/initial_speech_id — which MP
    -- you talked to is sensitive metadata); plaintext columns stay empty/NULL.
    -- No 7-day expiry for owned rows.
    user_id         UUID        REFERENCES users(id) ON DELETE CASCADE,
    enc_payload     TEXT,
    enc_title       TEXT
);

CREATE INDEX IF NOT EXISTS chat_sessions_last_activity_idx ON chat_sessions (last_activity);
CREATE INDEX IF NOT EXISTS chat_sessions_user_idx ON chat_sessions (user_id, last_activity DESC);

ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS enc_payload TEXT;
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS enc_title TEXT;

-- ─────────────────────────────────────────
-- chat_snapshots  (frozen, shareable chat views)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chat_snapshots (
    id              UUID        PRIMARY KEY,
    session_type    TEXT        NOT NULL CHECK (session_type IN ('general', 'mp')),
    person_id   TEXT,       -- for MP chats: used to show name/party in the snapshot view
    turns           JSONB       NOT NULL DEFAULT '[]',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Full tool-call history, so a forked snapshot resumes with the model's
    -- context rather than only the rendered turns. Note that stat cards replay
    -- the SQL stored in here, which is why retired column names need a
    -- compatibility shim rather than a clean break.
    llm_messages    JSONB,
    focus_ids       TEXT[]      DEFAULT '{}',
    initial_speech_id TEXT,
    last_activity   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS chat_snapshots_last_activity_idx ON chat_snapshots (last_activity);

-- ─────────────────────────────────────────
-- research_boards / research_threads  (deep research)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS research_boards (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    title         TEXT        NOT NULL,
    topic         TEXT        NOT NULL,
    intro         TEXT,
    owner_session TEXT,       -- per-browser owner (X-Session-Id); scopes the list
    status        TEXT        NOT NULL DEFAULT 'new'
                  CHECK (status IN ('new', 'scouting', 'awaiting', 'digging', 'reporting', 'ready', 'failed')),
    revision      INTEGER     NOT NULL DEFAULT 1,
    target_depth  INTEGER     NOT NULL DEFAULT 3,
    logic_version INTEGER     NOT NULL DEFAULT 1,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Encrypted boards (enc = TRUE): title/topic/intro and the threads'
    -- title/question/why/findings/open_questions/leads hold "v1:..." ciphertext
    -- under a per-board key. The board key is stored only wrapped by the
    -- owner's DEK; background jobs receive the raw key via stdin (never the DB).
    user_id       UUID        REFERENCES users(id) ON DELETE CASCADE,
    enc           BOOLEAN     NOT NULL DEFAULT FALSE,
    wrapped_board_key TEXT,
    -- Single regeneratable markdown report woven from the thread answers
    -- (encrypted on enc boards).
    report        TEXT,
    report_generated_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS research_boards_owner_idx ON research_boards (owner_session);
CREATE INDEX IF NOT EXISTS research_boards_user_idx ON research_boards (user_id);

ALTER TABLE research_boards ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE research_boards ADD COLUMN IF NOT EXISTS enc BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE research_boards ADD COLUMN IF NOT EXISTS wrapped_board_key TEXT;
ALTER TABLE research_boards ADD COLUMN IF NOT EXISTS report TEXT;
ALTER TABLE research_boards ADD COLUMN IF NOT EXISTS report_generated_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS research_threads (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    board_id       UUID        NOT NULL REFERENCES research_boards(id) ON DELETE CASCADE,
    title          TEXT        NOT NULL,
    question       TEXT        NOT NULL,
    why            TEXT        NOT NULL DEFAULT '',
    origin         TEXT        NOT NULL DEFAULT 'auto' CHECK (origin IN ('auto', 'seed')),
    depth          INTEGER     NOT NULL DEFAULT 0,
    status         TEXT        NOT NULL DEFAULT 'active' CHECK (status IN ('proposed', 'active', 'archived')),
    pinned         BOOLEAN     NOT NULL DEFAULT FALSE,
    findings       JSONB       NOT NULL DEFAULT '[]',
    open_questions JSONB       NOT NULL DEFAULT '[]',
    leads          JSONB       NOT NULL DEFAULT '[]',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- User's free-text steer for the thread (goes into trip prompts) and the
    -- synthesized markdown answer; hints persist the discovery pass's search
    -- suggestions until the thread's first trip. All three encrypted on enc
    -- boards. answer_depth = depth when the answer was last synthesized
    -- (depth > answer_depth means the answer is stale).
    guidance       TEXT,
    answer         TEXT,
    answer_depth   INTEGER     NOT NULL DEFAULT 0,
    hints          JSONB       NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS research_threads_board_idx ON research_threads (board_id);

ALTER TABLE research_threads ADD COLUMN IF NOT EXISTS guidance TEXT;
ALTER TABLE research_threads ADD COLUMN IF NOT EXISTS answer TEXT;
ALTER TABLE research_threads ADD COLUMN IF NOT EXISTS answer_depth INTEGER NOT NULL DEFAULT 0;
ALTER TABLE research_threads ADD COLUMN IF NOT EXISTS hints JSONB NOT NULL DEFAULT '[]';

-- ─────────────────────────────────────────
-- jobs / job_events  (out-of-process background jobs)
-- ─────────────────────────────────────────
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

-- ─────────────────────────────────────────
-- observability
-- ─────────────────────────────────────────

-- Deduplicated error store. Repeated failures bump `count` and `last_seen_at`
-- rather than inserting a new row, so a crash loop stays one line instead of
-- thousands. `fingerprint` is the dedup key (type + normalised traceback).
CREATE TABLE IF NOT EXISTS error_log (
    id            BIGSERIAL   PRIMARY KEY,
    fingerprint   TEXT        NOT NULL UNIQUE,
    error_type    TEXT        NOT NULL,
    first_seen_at TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ DEFAULT NOW(),
    count         INTEGER     NOT NULL DEFAULT 1,
    model         TEXT,
    traceback     TEXT,
    detail        JSONB
);

CREATE INDEX IF NOT EXISTS error_log_last_seen_idx  ON error_log (last_seen_at DESC);
CREATE INDEX IF NOT EXISTS error_log_error_type_idx ON error_log (error_type);

-- Behavioural events (tool chosen, model used, latency). Append-only.
CREATE TABLE IF NOT EXISTS llm_events (
    id         BIGSERIAL   PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    event_type TEXT        NOT NULL,
    detail     JSONB
);

CREATE INDEX IF NOT EXISTS llm_events_created_idx ON llm_events (created_at DESC);
CREATE INDEX IF NOT EXISTS llm_events_type_idx    ON llm_events (event_type);
-- Expression index: model lives inside the JSON payload but is grouped on constantly.
CREATE INDEX IF NOT EXISTS llm_events_model_idx   ON llm_events ((detail ->> 'model'));

-- ─────────────────────────────────────────
-- evaluation harness
--
-- Populated by scripts/eval_harness.py. A "run" asks a fixed question set; each
-- answer is split into paragraphs and judged for whether its citations support it.
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS eval_runs (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at   TIMESTAMPTZ,
    label         TEXT,
    config        JSONB,
    num_questions INTEGER     NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS eval_questions (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id         UUID        REFERENCES eval_runs(id) ON DELETE CASCADE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    question       TEXT        NOT NULL,
    question_type  TEXT,
    answer         TEXT,
    tool_trace     JSONB,
    sources        JSONB,
    num_iterations INTEGER,
    duration_ms    INTEGER,
    error          TEXT,
    complexity     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_eval_questions_run ON eval_questions (run_id);

CREATE TABLE IF NOT EXISTS eval_judgments (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    question_id       UUID        REFERENCES eval_questions(id) ON DELETE CASCADE,
    paragraph_idx     INTEGER,
    paragraph_text    TEXT,
    cited_indices     INTEGER[],
    verdict           TEXT,
    rationale         TEXT,
    judge_model       TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata_mismatch TEXT,
    coverage_score    DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_eval_judgments_q ON eval_judgments (question_id);

-- Whole conversations captured for later analysis. Only sessions whose first
-- message is prefixed "TEST " are recorded, so ordinary user chats are never stored here.
CREATE TABLE IF NOT EXISTS eval_conversations (
    id          BIGSERIAL   PRIMARY KEY,
    session_id  TEXT        NOT NULL,
    turn_index  INTEGER     NOT NULL,
    stream      BOOLEAN     NOT NULL DEFAULT FALSE,
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    duration_s  DOUBLE PRECISION,
    iterations  INTEGER,
    has_error   BOOLEAN     NOT NULL DEFAULT FALSE,
    doc         JSONB       NOT NULL
);

CREATE INDEX IF NOT EXISTS eval_conversations_session_idx ON eval_conversations (session_id, started_at);
