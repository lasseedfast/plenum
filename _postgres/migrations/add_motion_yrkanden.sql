-- Make the formal proposals (yrkanden, <forslag> tags) first-class search targets.
-- They are condensed and to-the-point, so they get a high FTS weight and their
-- own embedding table for semantic search.
-- Apply once: psql -U riksdagen -d riksdagen -f _postgres/migrations/add_motion_yrkanden.sql

-- ── 1. Keyword: fold yrkanden text into motions.search_vector ──────────────
ALTER TABLE motions ADD COLUMN IF NOT EXISTS forslag_text TEXT;  -- concat of yrkande lydelser

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

-- ── 2. Semantic: one embeddable row per yrkande ────────────────────────────
CREATE TABLE IF NOT EXISTS motion_yrkanden (
    -- Key: "{dok_id}:{ordinal}", ordinal = 0-based position in the forslag array
    id           TEXT PRIMARY KEY,
    dok_id       TEXT NOT NULL REFERENCES motions(dok_id) ON DELETE CASCADE,
    ordinal      INTEGER NOT NULL,
    nummer       TEXT,        -- yrkande number as stated in the motion
    lydelse      TEXT NOT NULL,
    utskottet    TEXT,        -- committee proposal (e.g. "Avslag")
    kammaren     TEXT,        -- chamber decision (e.g. "Avslag"/"Bifall")
    behandlas_i  TEXT,        -- committee report where handled
    embedding    vector(384)
);

CREATE INDEX IF NOT EXISTS motion_yrkanden_dok_idx ON motion_yrkanden (dok_id);
CREATE INDEX IF NOT EXISTS motion_yrkanden_embedding_idx ON motion_yrkanden
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- ── 3. Backfill both from the existing forslag JSONB (no re-parse needed) ───
INSERT INTO motion_yrkanden (id, dok_id, ordinal, nummer, lydelse, utskottet, kammaren, behandlas_i)
SELECT m.dok_id || ':' || (f.ord - 1),
       m.dok_id,
       (f.ord - 1)::int,
       f.val->>'nummer',
       f.val->>'lydelse',
       f.val->>'utskottet',
       f.val->>'kammaren',
       f.val->>'behandlas_i'
FROM motions m,
     LATERAL jsonb_array_elements(m.forslag) WITH ORDINALITY AS f(val, ord)
WHERE m.forslag IS NOT NULL
  AND jsonb_typeof(m.forslag) = 'array'
  AND coalesce(f.val->>'lydelse', '') <> ''
ON CONFLICT (id) DO NOTHING;

-- forslag_text last: this UPDATE fires the trigger and rebuilds search_vector
-- (now including the yrkanden at weight B) for every existing motion.
UPDATE motions m
SET forslag_text = sub.txt
FROM (
    SELECT dok_id, string_agg(val->>'lydelse', ' ') AS txt
    FROM motions, LATERAL jsonb_array_elements(forslag) AS val
    WHERE forslag IS NOT NULL
      AND jsonb_typeof(forslag) = 'array'
      AND coalesce(val->>'lydelse', '') <> ''
    GROUP BY dok_id
) sub
WHERE m.dok_id = sub.dok_id
  AND m.forslag_text IS DISTINCT FROM sub.txt;
