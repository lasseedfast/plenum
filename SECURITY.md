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

Two layers apply, and the second is the one that counts:

1. Statements are refused unless they begin with `SELECT` or `WITH`, and multiple
   statements are rejected outright — that is how a write gets smuggled in behind a
   leading `SELECT`.
2. The query runs inside a `SET TRANSACTION READ ONLY` transaction, so PostgreSQL
   itself rejects `INSERT`, `UPDATE`, `DELETE` and DDL regardless of what the first
   check concluded.

The same applies to `share_insight`, which re-executes SQL stored in saved
conversations — replayed SQL is no more trustworthy than freshly generated SQL.

**Still run plenum against a database role that only has `SELECT`.** Defence in depth
means not relying on any single layer, and the ingest pipeline needs write access
that the web application never should have:

```sql
CREATE ROLE plenum_ro LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE plenum TO plenum_ro;
GRANT USAGE ON SCHEMA public TO plenum_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO plenum_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO plenum_ro;
```

The ingest pipeline needs write access and should use a separate role.

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
