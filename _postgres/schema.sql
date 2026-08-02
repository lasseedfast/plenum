-- Riksdagen database schema for PostgreSQL + pgvector
-- Run once: psql -U riksdagen -d riksdagen -f schema.sql

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- for ILIKE index support on prefix searches

-- ─────────────────────────────────────────
-- people
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS people (
    intressent_id   TEXT PRIMARY KEY,
    hangar_id       TEXT,
    hangar_guid     TEXT,
    sourceid        TEXT,
    fodd_ar         TEXT,
    kon             TEXT,
    efternamn       TEXT,
    tilltalsnamn    TEXT,
    sorteringsnamn  TEXT,
    iort            TEXT,
    parti           TEXT,
    valkrets        TEXT,
    status          TEXT,
    person_url_xml  TEXT,
    bild_url_80     TEXT,
    bild_url_192    TEXT,
    bild_url_max    TEXT,
    personuppdrag   JSONB,   -- nested assignment array, kept as-is
    personuppgift   JSONB,   -- nested contact info array, kept as-is
    namn            TEXT,
    aktiv           BOOLEAN
);

CREATE INDEX IF NOT EXISTS people_parti_idx ON people (parti);
CREATE INDEX IF NOT EXISTS people_namn_idx  ON people (namn);

-- ─────────────────────────────────────────
-- talks
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS talks (
    -- Primary key: dok_id (e.g. "H40911"), called "id" in the source data
    id              TEXT PRIMARY KEY,

    -- Original UUID from riksdagen (preserved for reference after key migration)
    anforande_id    TEXT,

    -- Core content
    anforandetext   TEXT,
    avsnittsrubrik  TEXT,

    -- Metadata
    anforande_nummer INTEGER,
    kammaraktivitet TEXT,
    talare          TEXT,
    parti           TEXT,
    intressent_id   TEXT,      -- references people(intressent_id), nullable

    -- Date fields
    datum           DATE,      -- e.g. 2016-09-29
    dok_datum       TEXT,      -- original datetime string from riksdagen API
    year            INTEGER,
    period          INTEGER,   -- parliamentary session year (start year)

    -- Document references
    rel_dok_id      TEXT,
    dok_nummer      TEXT,
    hangar_id       TEXT,
    titel           TEXT,      -- debate/session title

    -- Debate grouping
    debate          TEXT,      -- e.g. "2016-09-29:56"
    replik          BOOLEAN,

    -- LLM-generated fields
    summary         TEXT,
    tags            TEXT[],

    -- URL fields (populated from riksdagen API, may be null)
    debateurl       TEXT,
    url_session     TEXT,
    url_audio       TEXT,
    audiofileurl    TEXT,
    startpos        INTEGER,

    -- Full-text search vector (auto-maintained by trigger)
    search_vector   TSVECTOR
);

-- Full-text search index (Swedish)
CREATE INDEX IF NOT EXISTS talks_search_idx     ON talks USING GIN (search_vector);

-- Filtering indexes
CREATE INDEX IF NOT EXISTS talks_debate_idx     ON talks (debate, anforande_nummer);
CREATE INDEX IF NOT EXISTS talks_parti_idx      ON talks (parti);
CREATE INDEX IF NOT EXISTS talks_datum_idx      ON talks (datum);
CREATE INDEX IF NOT EXISTS talks_year_idx       ON talks (year);
CREATE INDEX IF NOT EXISTS talks_intressent_idx ON talks (intressent_id);
CREATE INDEX IF NOT EXISTS talks_talare_idx     ON talks USING GIN (to_tsvector('simple', coalesce(talare, '')));

-- Trigger to keep search_vector up to date on insert/update
CREATE OR REPLACE FUNCTION talks_search_vector_update() RETURNS TRIGGER AS $$
BEGIN
    NEW.search_vector := to_tsvector('swedish', coalesce(NEW.anforandetext, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS talks_search_vector_trigger ON talks;
CREATE TRIGGER talks_search_vector_trigger
    BEFORE INSERT OR UPDATE OF anforandetext
    ON talks
    FOR EACH ROW
    EXECUTE FUNCTION talks_search_vector_update();

-- ─────────────────────────────────────────
-- chunks  (text chunks + vector embeddings)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chunks (
    -- Key: "{talk_id}:{chunk_index}", e.g. "H40911:0"
    id          TEXT PRIMARY KEY,

    talk_id     TEXT NOT NULL REFERENCES talks(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text        TEXT NOT NULL,
    embedding   vector(384)
);

CREATE INDEX IF NOT EXISTS chunks_talk_idx      ON chunks (talk_id);
-- HNSW index for approximate nearest-neighbour cosine search
CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ─────────────────────────────────────────
-- debates  (aggregated debate summaries)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS debates (
    -- Key: "{date}:{debate_index}", e.g. "2016-09-29:56"
    debate          TEXT PRIMARY KEY,

    datum           DATE,
    summary         TEXT,
    num_talks       INTEGER,
    talk_summaries  TEXT[],   -- array of individual talk summary strings
    talk_ids        TEXT[]    -- array of talk ids (without "talks/" prefix)
);

CREATE INDEX IF NOT EXISTS debates_datum_idx ON debates (datum);

-- ─────────────────────────────────────────
-- motions  (motioner from riksdagens öppna data)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS motions (
    -- Primary key: dok_id (e.g. "HD02846")
    dok_id          TEXT PRIMARY KEY,
    hangar_id       TEXT,

    -- Identity / classification
    rm              TEXT,       -- riksmöte, e.g. "2022/23"
    beteckning      TEXT,       -- motion number within rm, e.g. "846"
    subtyp          TEXT,       -- e.g. "Enskild motion", "Kommittémotion"
    organ           TEXT,       -- committee the motion was referred to, e.g. "AU"
    status          TEXT,       -- e.g. "Klar", "Inkommen"

    -- Dates
    datum           DATE,
    systemdatum     TEXT,       -- raw string, used for change detection
    publicerad      TEXT,
    year            INTEGER,    -- int(rm[:4]), mirrors talks.period

    -- Content
    titel           TEXT,
    undertitel      TEXT,
    text            TEXT,       -- plain text extracted from the html field
    forslag_text    TEXT,       -- concat of yrkande lydelser (high-signal, weighted B in FTS)
    has_text        BOOLEAN NOT NULL DEFAULT FALSE,  -- false for scanned-PDF-only documents

    -- Source URLs
    dokument_url_text TEXT,
    dokument_url_html TEXT,
    pdf_url         TEXT,

    -- Authors (denormalized; relational detail in motion_authors)
    parties         TEXT[] NOT NULL DEFAULT '{}',
    author_names    TEXT[] NOT NULL DEFAULT '{}',    -- in signing order

    -- Proposals and attachments, raw from dokumentstatus
    forslag         JSONB,      -- dokforslag list: yrkanden + utskottsförslag + kammarens beslut
    bilagor         JSONB,      -- dokbilaga list
    num_yrkanden    INTEGER NOT NULL DEFAULT 0,

    -- LLM-generated fields (future parity with talks)
    summary         TEXT,

    -- Full-text search vector (auto-maintained by trigger)
    search_vector   TSVECTOR
);

CREATE INDEX IF NOT EXISTS motions_search_idx  ON motions USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS motions_datum_idx   ON motions (datum);
CREATE INDEX IF NOT EXISTS motions_year_idx    ON motions (year);
CREATE INDEX IF NOT EXISTS motions_organ_idx   ON motions (organ);
CREATE INDEX IF NOT EXISTS motions_parties_idx ON motions USING GIN (parties);

CREATE OR REPLACE FUNCTION motions_search_vector_update() RETURNS TRIGGER AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('swedish', coalesce(NEW.titel, '')), 'A') ||
        setweight(to_tsvector('swedish', coalesce(NEW.forslag_text, '')), 'B') ||
        setweight(to_tsvector('swedish', coalesce(NEW.undertitel, '')), 'C') ||
        setweight(to_tsvector('swedish', coalesce(NEW.text, '')), 'D');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS motions_search_vector_trigger ON motions;
CREATE TRIGGER motions_search_vector_trigger
    BEFORE INSERT OR UPDATE OF titel, undertitel, text, forslag_text
    ON motions
    FOR EACH ROW
    EXECUTE FUNCTION motions_search_vector_update();

-- ─────────────────────────────────────────
-- motion_authors  (undertecknare, in signing order)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS motion_authors (
    dok_id        TEXT NOT NULL REFERENCES motions(dok_id) ON DELETE CASCADE,
    ordinal       INTEGER NOT NULL,   -- position in dokintressent list
    -- Soft link to people(intressent_id); no FK — 1990s ids may be missing from people
    intressent_id TEXT,
    namn          TEXT,
    partibet      TEXT,
    roll          TEXT,               -- e.g. "undertecknare"
    PRIMARY KEY (dok_id, ordinal)
);

CREATE INDEX IF NOT EXISTS motion_authors_intressent_idx ON motion_authors (intressent_id);

-- ─────────────────────────────────────────
-- motion_chunks  (text chunks + vector embeddings)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS motion_chunks (
    -- Key: "{dok_id}:{chunk_index}", e.g. "HD02846:0"
    id          TEXT PRIMARY KEY,

    motion_id   TEXT NOT NULL REFERENCES motions(dok_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text        TEXT NOT NULL,
    embedding   vector(384)
);

CREATE INDEX IF NOT EXISTS motion_chunks_motion_idx ON motion_chunks (motion_id);
CREATE INDEX IF NOT EXISTS motion_chunks_embedding_idx ON motion_chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- ─────────────────────────────────────────
-- motion_yrkanden  (one condensed proposal per row + its own embedding)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS motion_yrkanden (
    -- Key: "{dok_id}:{ordinal}", ordinal = 0-based position in the forslag array
    id           TEXT PRIMARY KEY,
    dok_id       TEXT NOT NULL REFERENCES motions(dok_id) ON DELETE CASCADE,
    ordinal      INTEGER NOT NULL,
    nummer       TEXT,
    lydelse      TEXT NOT NULL,
    utskottet    TEXT,        -- committee proposal (e.g. "Avslag")
    kammaren     TEXT,        -- chamber decision (e.g. "Avslag"/"Bifall")
    behandlas_i  TEXT,        -- committee report where handled
    embedding    vector(384)
);

CREATE INDEX IF NOT EXISTS motion_yrkanden_dok_idx ON motion_yrkanden (dok_id);
CREATE INDEX IF NOT EXISTS motion_yrkanden_embedding_idx ON motion_yrkanden
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
    intressent_id   TEXT        REFERENCES people(intressent_id),
    initial_talk_id TEXT,
    llm_messages    JSONB       NOT NULL DEFAULT '[]',
    turns           JSONB       NOT NULL DEFAULT '[]',
    focus_ids       TEXT[]      NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_activity   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Owned sessions (user_id set): all content lives encrypted in enc_payload
    -- (llm_messages/turns/focus_ids AND intressent_id/initial_talk_id — which MP
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
    intressent_id   TEXT,       -- for MP chats: used to show name/party in the snapshot view
    turns           JSONB       NOT NULL DEFAULT '[]',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

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
