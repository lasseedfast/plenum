# Security

## Reporting a vulnerability

Open a private issue on the repository, or contact the maintainer directly rather
than filing a public issue. Please allow reasonable time for a fix before disclosing.

## Model-authored SQL

The `database_query` tool lets the model write and run SQL so it can answer aggregate
questions ("how many speeches per party mentioned X?"). This matters more than it
might appear, because the corpus reaches the model's context: speech and document
text is fetched and shown to it, so anyone who can get text into the parliament's
record — which, in a parliament, is the point — can attempt a prompt injection.

Four layers apply:

1. Statements are refused unless they begin with `SELECT` or `WITH`.
2. Multiple statements are rejected — that is how a write gets smuggled in behind a
   leading `SELECT`.
3. The query may only name the corpus tables (`speeches`, `people`, `debates`,
   `documents`, `document_authors`, `document_proposals`). Anything else is refused,
   including through a join, a subquery, a CTE or a `UNION`.
4. The query runs inside a `SET TRANSACTION READ ONLY` transaction, so PostgreSQL
   itself rejects `INSERT`, `UPDATE`, `DELETE` and DDL regardless of what the earlier
   checks concluded.

Layer 3 is the one that keeps generated SQL away from the application's own tables.
**`READ ONLY` does not do this** — it stops writes, not reads, and the same database
holds `users`, `auth_tokens` and the chat sessions. That gap was real: before layer 3
existed, asking the live site to list the rows in `users` worked.

The same applies to `share_insight`, which re-executes SQL stored in saved
conversations — replayed SQL is no more trustworthy than freshly generated SQL.

### Run model SQL as a role that cannot read anything else

Layer 3 is a parser, and a parser can be fooled. The guarantee is a database role that
was never granted `SELECT` on the other tables:

```sql
CREATE ROLE plenum_llm LOGIN PASSWORD '...' NOINHERIT;
GRANT CONNECT ON DATABASE plenum TO plenum_llm;
GRANT USAGE ON SCHEMA public TO plenum_llm;
GRANT SELECT ON speeches, people, debates,
                documents, document_authors, document_proposals TO plenum_llm;
```

Set `PG_LLM_USER` and `PG_LLM_PASSWORD` and the tool connects as this role; leave them
unset and it falls back to the main connection with a warning.

Deliberately no `GRANT ... ON ALL TABLES` and no `ALTER DEFAULT PRIVILEGES`: a table
added later should be unreadable until someone grants it explicitly.

Note that **the application as a whole cannot run read-only** — it writes chat sessions,
auth records and event logs. Two roles are needed, not one: the application's own role
with write access to its tables, and this one for model-authored SQL. The ingest
pipeline needs write access to the corpus and should use a third.

## API keys

Users may supply their own model provider key in the browser. That key is held only
for the duration of a request, is passed to background research jobs over the child
process's stdin, and is never written to the database or to logs. The only persisted
copy is the one the browser encrypts under the user's own password.

Server-managed keys come from environment variables and must never be committed.
`.gitignore` excludes `.env`, `.env.*`, `providers.yaml`, key material and archives.

## Chat privacy

Signed-in users' chats and research boards are stored encrypted with a key derived
from their password. The server cannot read them. This means a forgotten password is
unrecoverable by design.

Conversations are logged for evaluation **only** when the first message begins with
`TEST `. Ordinary conversations are never written to the evaluation tables.
