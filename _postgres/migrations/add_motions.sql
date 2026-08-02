-- Motioner (parliamentary motions) from Riksdagens öppna data.
-- Apply once: psql -U riksdagen -d riksdagen -f _postgres/migrations/add_motions.sql

-- ─────────────────────────────────────────
-- motions
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
        setweight(to_tsvector('swedish', coalesce(NEW.undertitel, '')), 'B') ||
        setweight(to_tsvector('swedish', coalesce(NEW.text, '')), 'D');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS motions_search_vector_trigger ON motions;
CREATE TRIGGER motions_search_vector_trigger
    BEFORE INSERT OR UPDATE OF titel, undertitel, text
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
-- NOTE: for the initial bulk backfill it is much faster to DROP this index,
-- load all embeddings, then recreate it.
CREATE INDEX IF NOT EXISTS motion_chunks_embedding_idx ON motion_chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
