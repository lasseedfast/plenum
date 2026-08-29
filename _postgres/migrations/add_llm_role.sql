-- A database role for model-authored SQL.
--
-- `database_query` runs SQL a language model wrote, in a context that includes
-- corpus text — which, in a parliament, anyone able to speak can influence. The
-- application's own role can write, and can read `users`, `auth_tokens` and the
-- chat tables; none of that should be reachable from generated SQL.
--
-- The guard in backend/services/llm_tools.py refuses queries naming anything
-- outside the corpus, but that is a parser. This is the part the database
-- enforces on its own.
--
-- Set PG_LLM_USER / PG_LLM_PASSWORD in .env afterwards, then restart the service.
-- Replace the password before running.

CREATE ROLE plenum_llm LOGIN PASSWORD 'CHANGE-ME' NOINHERIT;

GRANT CONNECT ON DATABASE plenum TO plenum_llm;
GRANT USAGE ON SCHEMA public TO plenum_llm;

-- The corpus, listed one by one on purpose. No GRANT ... ON ALL TABLES and no
-- ALTER DEFAULT PRIVILEGES: a table added later must stay unreadable until
-- someone grants it deliberately.
GRANT SELECT ON speeches            TO plenum_llm;
GRANT SELECT ON people              TO plenum_llm;
GRANT SELECT ON debates             TO plenum_llm;
GRANT SELECT ON documents           TO plenum_llm;
GRANT SELECT ON document_authors    TO plenum_llm;
GRANT SELECT ON document_proposals  TO plenum_llm;

-- Verify: the first must fail, the second must succeed.
--   psql -U plenum_llm -d plenum -c 'SELECT count(*) FROM users;'
--   psql -U plenum_llm -d plenum -c 'SELECT count(*) FROM speeches;'
