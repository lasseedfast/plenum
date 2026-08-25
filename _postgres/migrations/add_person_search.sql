-- Name lookup for the member autocomplete and the people cards in search results.
--
-- Two things were missing. Matching a surname needs a substring ILIKE, which is a
-- sequential scan over people on every keystroke without a trigram index. And
-- ranking the matches sensibly needs each person's speech volume and recency,
-- which cost seconds when aggregated per candidate on the fly.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS people_name_trgm_idx
    ON people USING gin (lower(name) gin_trgm_ops);

CREATE MATERIALIZED VIEW IF NOT EXISTS person_speech_stats AS
    SELECT person_id,
           count(*)::int AS speech_count,
           max(date)     AS last_speech
    FROM speeches
    WHERE person_id IS NOT NULL
    GROUP BY person_id;

-- A unique index is what lets the daily sync use REFRESH ... CONCURRENTLY, so
-- rebuilding the aggregates does not lock out readers.
CREATE UNIQUE INDEX IF NOT EXISTS person_speech_stats_pkey
    ON person_speech_stats (person_id);
