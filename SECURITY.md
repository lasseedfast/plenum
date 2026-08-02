# Security

## Reporting a vulnerability

Open a private issue on the repository, or contact the maintainer directly rather
than filing a public issue. Please allow reasonable time for a fix before disclosing.

## Known issue: `database_query` executes model-authored SQL

The `database_query` tool lets the model write and run SQL so it can answer
aggregate questions ("how many speeches per party mentioned X?"). The SQL is passed
to the database as written. There is currently **no `SELECT`-only enforcement in the
application**.

This matters more than it might appear, because the corpus itself reaches the model's
context. Speech and document text is fetched and shown to the model, so anyone who
can get text into the parliament's record — which, in a parliament, is the point —
can attempt a prompt injection.

**Run plenum against a database role that only has `SELECT`.** The application does
not do this for you:

```sql
CREATE ROLE plenum_ro LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE plenum TO plenum_ro;
GRANT USAGE ON SCHEMA public TO plenum_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO plenum_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO plenum_ro;
```

The ingest pipeline needs write access and should use a separate role.

Enforcing this in the application — a read-only transaction plus a statement-type
check on the tool path — is tracked and intended.

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
