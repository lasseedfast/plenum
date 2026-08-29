# Database schema

The definitive version is [`_postgres/schema.sql`](../_postgres/schema.sql), verified
against the production database. This page explains it.

Names are country-neutral English. Where a parliamentary concept has no stable
English equivalent — the Swedish *yrkande*, *riksmöte* or *replik* — the column takes
a neutral name and the country's own word lives in `parliament.yaml` under
`vocabulary:`, where prompts pick it up. Data values are never translated.

## What the model is told

This page is for people. The description the *model* reads is a different, shorter
thing — [`prompts/en/_shared/schema_tables.md`](../prompts/en/_shared/schema_tables.md)
— and it is **generated from the database**, never edited by hand:

```bash
python scripts/generate_schema_prompt.py
```

A column reaches that file only if it carries a `COMMENT`, and the comment is its
description. `-` means "shown, the name says it"; `[hide] reason` keeps it out; any
other text becomes the note the model reads; no comment at all fails the build, so a
newly added column cannot be silently forgotten. The statements live in
[`_postgres/migrations/add_column_comments.sql`](../_postgres/migrations/add_column_comments.sql).

Roughly half the columns are hidden — ingest bookkeeping, URLs, embeddings, pipeline
flags. A model answering questions about politics has no use for them, and every
column it is shown costs context on every turn.

This exists because the hand-maintained version drifted: it named three columns that
did not exist (`debates.debate`, `document_proposals.nummer`, `behandlas_i`), warned
about NULLs in a column that has none, and called a `text` column an integer. A
generated description cannot do any of that. `COMMENT` also survives
`ALTER TABLE ... RENAME` and vanishes on `DROP`, so the prompt follows a rename by
itself.

**Adding a column?** Write its `COMMENT` in the same migration, then regenerate.
`tests/test_schema_comments.py` will fail until you do.

## Core tables

### `speeches` — what was said in the chamber

| Column | Meaning |
|---|---|
| `id` | Primary key, from the source record |
| `source_speech_id` | The source's own UUID, kept from an earlier key migration |
| `text` | The speech itself |
| `section_title` | Heading of the agenda item |
| `sequence` | Position within the debate |
| `activity_type` | Activity code; see `activity_types` in `parliament.yaml` |
| `speaker_name` | Who spoke, as printed. Not the presiding officer — plain `speaker` would be ambiguous in a parliamentary schema, which is why the column is not called that |
| `party`, `person_id` | Party code; person, referencing `people` |
| `date`, `source_datetime`, `year`, `session_year` | Date, raw source string, calendar year, and the parliamentary session's start year |
| `source_doc_id`, `related_doc_id`, `source_doc_number`, `source_record_id` | Source document references. `source_doc_id` is the protocol document the speech appeared in — not this row's own key |
| `title` | Title of the *protocol* the speech appeared in, not of the speech or the debate. `section_title` is the agenda item, which is what most questions actually mean |
| `debate_id` | Groups speeches into a debate, `"{date}:{index}"` |
| `is_reply` | A short right-of-reply intervention. The Swedish *replik* carries procedural standing this name does not capture; nothing downstream depends on it |
| `summary`, `tags`, `arguments` | LLM-derived |
| `summary_embedding` | Embedding of `summary`, for debate-level semantic search |
| `search_vector` | Full-text vector, maintained by trigger |

### `documents` — what members submitted

A *document* is anything a member formally puts to the chamber. `doc_type` says which
kind; today everything is `'motion'`, and the column exists so bills, written
questions and committee reports do not each need their own table later.

| Column | Meaning |
|---|---|
| `doc_id` | Primary key |
| `doc_type` | Kind of document; defaults to `'motion'` |
| `session_label` | Which annual session, e.g. `"2022/23"`. Not called *term*: the European Parliament uses that for its five-year cycle |
| `designation` | Number within the session |
| `subtype` | Whether submitted by an individual, a committee group, or a whole party |
| `committee` | Committee it was referred to |
| `title`, `subtitle`, `text` | Content |
| `proposals_text` | The proposal texts concatenated — high-signal, weighted separately in search |
| `parties`, `author_names` | Denormalised authorship, in signing order |
| `proposals_raw`, `attachments` | Raw records from the source |
| `has_text` | False for documents that exist only as scanned PDFs |
| `session_year` | Derived from `session_label` |

### `document_proposals` — the operative demands inside a document

The unit people actually search and cite. The Swedish *yrkande* is a numbered,
formally worded demand; English has no single word for it, hence the neutral name.

| Column | Meaning |
|---|---|
| `text` | The proposal's wording |
| `number`, `ordinal` | Its number as published, and its 0-based position |
| `committee_recommendation` | What the committee recommended |
| `chamber_decision` | What the chamber decided, e.g. `Bifall` / `Avslag` |
| `handled_in` | Which report handled it |

Values stay exactly as published. `decisions:` in `parliament.yaml` glosses them, so
prompts can explain them without the record being rewritten.

### `people` — the register of members

`person_id` is the source's identifier and the join key used throughout.
`constituency` means different things by country — a single-member seat in the UK, a
multi-member district in Sweden, a national list in the European Parliament — and
nothing in the code assumes any of them.

### `speech_chunks`, `document_chunks` — passages with embeddings

Text split into passages, each with a `vector(N)` embedding under an HNSW cosine
index. `N` must match `embeddings.dimension` in `parliament.yaml`; the application
checks this at startup, because a mismatch otherwise fails deep inside pgvector with
a message that never mentions configuration.

### `debates`

One row per debate, keyed `"{date}:{index}"`, with a synthesised summary and the list
of speeches it covers.

## Application tables

Country-neutral already: `users` and `auth_tokens` (zero-knowledge auth),
`chat_sessions` and `chat_snapshots`, `research_boards`, `research_threads`, `jobs`,
`job_events`, `error_log` and `llm_events`, and the `eval_*` tables.

Two notes:

`chat_snapshots.llm_messages` holds the full tool-call history so a forked snapshot
resumes with the model's context. Stat cards replay the SQL stored there, which is why
the rename cannot be a clean break — SQL written against the old column names is still
sitting in saved conversations.

`session_type` is `'general'` or `'mp'`. Read `'mp'` as "member". The values were left
alone deliberately: rewriting live rows and a CHECK constraint would buy nothing, and
the display label comes from `parliament.yaml`.

## Full-text search

Search vectors are built with the configuration named by `language.fts_config`, read
at runtime inside the triggers via `current_setting('app.fts_config')`. Set it per
database:

```sql
ALTER DATABASE plenum SET app.fts_config = 'swedish';
```

It only takes effect for new sessions, so restart the API after changing it.

Note that `pg_dump` does **not** capture database-level settings. Restoring a dump
onto a fresh server loses this one, and the symptom is searches quietly returning
nothing rather than any error.

## The rename

The schema was originally Swedish, inherited from the Riksdag-only predecessor.
`_postgres/migrations/20260803_01_rename_to_english.sql` and its ROLLBACK companion
were generated from a single map, so the two directions cannot disagree.

That migration is guarded by an existence check: a no-op on a database created from
the current `schema.sql`, and the real thing on one created before the rename — so a
single file serves both a fresh install and an existing deployment.
`ALTER TABLE ... RENAME` is catalog-only in PostgreSQL, so no data moves and no index
is rebuilt, regardless of table size.
