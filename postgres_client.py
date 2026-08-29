"""
Root-level PostgreSQL client singletons.

Import this in scripts and services:
    from postgres_client import pg

`pg` is the application's connection and can write. `pg_llm` is for SQL a language
model wrote: it connects as a role granted SELECT on the corpus tables and nothing
else, so a prompt injection that gets past the guard in `llm_tools` still cannot
read `users`, `auth_tokens` or anyone's chats. See SECURITY.md.

The application itself cannot run read-only — it writes chat sessions, auth records
and event logs — which is why this is a second connection rather than a change to
the first one.
"""

import logging
import os

from _postgres._postgres import Postgres

log = logging.getLogger("riksdagen.postgres")

pg = Postgres()


def _llm_client() -> Postgres:
    """The restricted connection, or the main one with a warning if unconfigured.

    Falling back keeps a fresh checkout working — the table allowlist in
    `llm_tools._reject_unsafe_sql` still applies — but it is a downgrade, so say so
    once at startup rather than failing silently.
    """
    user = os.environ.get("PG_LLM_USER")
    if not user:
        log.warning(
            "PG_LLM_USER is unset — model-authored SQL will run on the main database "
            "connection. The table allowlist still applies, but the database itself is "
            "not enforcing it. See SECURITY.md."
        )
        return pg
    # The pool is opened on first use, so an unused client costs nothing.
    return Postgres(user=user, password=os.environ.get("PG_LLM_PASSWORD", ""))


pg_llm = _llm_client()
