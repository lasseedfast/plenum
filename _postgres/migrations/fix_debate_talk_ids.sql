-- Fix talk_ids in debates rows migrated from ArangoDB.
-- The migration stripped the "-{anforande_nummer}" suffix, leaving bare dok_ids
-- that don't match talks.id. This update reconstructs the correct IDs from talks.
UPDATE debates d
SET
    talk_ids = sub.ids,
    num_talks = sub.cnt
FROM (
    SELECT
        debate,
        array_agg(id ORDER BY anforande_nummer) AS ids,
        COUNT(*) AS cnt
    FROM talks
    WHERE debate IS NOT NULL
    GROUP BY debate
) sub
WHERE d.debate = sub.debate;
