-- Normalise legacy party-code casing in the motions tables
--
-- The archive hands out the same party under several casings — `C` and `c`, `MP`,
-- `Mp` and `mp`. Nothing rejects it and nothing errors; the damage is that every
-- equality filter silently returns a fraction of the rows. Before this migration:
--
--     document_authors.party = 'C'    33,776 rows  |  upper(party) = 'C'   46,652  (-28%)
--     document_authors.party = 'MP'   29,570 rows  |  upper(party) = 'MP'  39,583  (-25%)
--
--     122,971 document_authors rows were lowercase
--      33,755 documents held at least one lowercase entry in parties[]
--
-- and `SELECT p FROM documents, unnest(parties) p GROUP BY p` returned one party as
-- two groups (`C`: 14,760 and `c`: 5,326). The user-visible symptom was a stats pie
-- chart: the frontend matched party codes case-sensitively, so a lowercase group fell
-- through to the "unknown party" grey.
--
-- This is a backfill, not an ingest fix. ingest/adapters/riksdagen.py has uppercased
-- author party on the way in for a long time, with a comment describing this exact
-- failure, and derives documents.parties from those already-uppercased authors. The
-- newest affected document is dated 2001-09-17, against a corpus running to 2026-07-15
-- — so nothing arriving today adds to the mess, and there is no code change to pair
-- with this. `speeches.party` and `people.party` were already clean and are not touched.
--
-- Two things worth knowing before reading the statements below.
--
-- `parties && ARRAY['C']` was never affected: those arrays carried *both* casings, so
-- the overlap operator matched either way. That is also why the array rebuild must
-- de-duplicate. A row holding {M,KD,C,FP,m,c,fp,kd} — which is the actual shape of
-- these rows, each party present twice — must fold to {C,FP,KD,M}, not to eight
-- identical-in-pairs entries. 32,044 of the 33,755 affected arrays shrink.
--
-- The ORDER BY is not cosmetic: DISTINCT alone leaves the element order unspecified.
-- Sorting matches what ingest already writes (`sorted({...})` in the adapter), so
-- backfilled rows come out in the same canonical form as new ones.
--
-- Both statements are guarded on the defect itself, so this file is a no-op on a
-- database that is already clean and safe to re-run. It is applied one statement at a
-- time (scripts/apply_migration.py commits per statement), so there is deliberately no
-- BEGIN/COMMIT here — a wrapper would be committed on its own and mislead. Each UPDATE
-- is atomic in its own right and neither depends on the other.
--
-- Applying it needs a looser cap than the app's default. The documents UPDATE rewrites
-- 33,755 rows in a 2.4 GB table with a GIN index on the column, which is well past the
-- 30 s PG_STATEMENT_TIMEOUT_MS that _postgres.py puts on every pooled connection; the
-- first attempt died with "canceling statement due to statement timeout" after the
-- document_authors statement had already committed. That is survivable precisely
-- because both statements are guarded and re-running resumes where it stopped, but
-- save yourself the round trip:
--
--     PG_STATEMENT_TIMEOUT_MS=1800000 PG_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS=0 \
--       python scripts/apply_migration.py 20260828_01_normalise_party_case
--
-- No schema.sql counterpart: this changes data, not structure. And no reindex —
-- documents.search_vector is built from title, proposals_text, subtitle and text, and
-- its trigger fires only on UPDATE OF those four columns, so writing `parties` neither
-- changes the vector nor recomputes it. The GIN index on `parties` is maintained by the
-- UPDATE as usual.

UPDATE document_authors
   SET party = upper(party)
 WHERE party <> upper(party);

-- `parties` inside the subquery is the pre-UPDATE value, which is what we want to fold.
-- The IS NOT NULL guard keeps a NULL array NULL rather than rewriting it to '{}';
-- an empty array has nothing to fold and fails the EXISTS, so it is left alone too.
UPDATE documents
   SET parties = ARRAY(SELECT DISTINCT upper(p) FROM unnest(parties) p ORDER BY 1)
 WHERE parties IS NOT NULL
   AND EXISTS (SELECT 1 FROM unnest(parties) p WHERE p <> upper(p));

-- 122,971 rows changed value in a column the planner keeps a most-common-values list
-- for; without this, party filters plan against the pre-backfill distribution.
ANALYZE document_authors;
ANALYZE documents;

-- Not included, because it cannot run inside a transaction block and this file is
-- executed inside one: the documents UPDATE leaves ~34k dead tuples in a 2.4 GB table.
-- Autovacuum will get to them. To reclaim sooner, run by hand afterwards:
--     VACUUM (ANALYZE) documents;
