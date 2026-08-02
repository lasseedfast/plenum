# Database schema

The definitive version is [`_postgres/schema.sql`](../_postgres/schema.sql), which is
verified against the production database. This page explains it.

Column names are still Swedish, inherited from the original Riksdag-only project.
They are only names — nothing in the application derives meaning from them — but they
are an obstacle to reading the code, so a rename to neutral English is planned. Until
then, this is the translation.

## Core tables

### `talks` — speeches in the chamber

One row per speech. The primary key `id` comes from the source; note the separate
`dok_id`, which identifies the *protocol document* the speech appeared in.

| Column | Meaning |
|---|---|
| `id` | Primary key, from the source record's `id` field |
| `anforande_id` | The source's own UUID, preserved from an earlier key migration |
| `anforandetext` | The speech text |
| `avsnittsrubrik` | Heading of the agenda item |
| `anforande_nummer` | Position within the debate |
| `kammaraktivitet` | Activity type code; see `activity_types` in `parliament.yaml` |
| `talare` | Speaker name as printed. Not the presiding officer — "speaker" in the plain sense |
| `parti` | Party code |
| `intressent_id` | Person id, references `people` |
| `datum`, `dok_datum`, `year`, `period` | Date, raw source datetime, calendar year, session start year |
| `dok_id`, `rel_dok_id`, `dok_nummer`, `hangar_id` | Source document references |
| `titel` | Debate title |
| `debate` | Debate grouping key, `"{date}:{index}"` |
| `replik` | Whether this is a rebuttal (a short right-of-reply intervention) |
| `summary`, `tags`, `arguments` | LLM-derived |
| `summary_embedding` | Embedding of `summary`, for debate-level semantic search |
| `search_vector` | Full-text vector, maintained by trigger |

### `motions` — member-submitted documents

The Swedish *motion* is a formal proposal submitted by members. Each contains one or
more numbered *yrkanden* — operative demands — which is what people actually search
and cite.

| Column | Meaning |
|---|---|
| `dok_id` | Primary key |
| `rm` | Session label, e.g. `"2022/23"` (Swedish *riksmöte*) |
| `beteckning` | Number within the session |
| `subtyp` | Whether submitted by an individual, a committee group, or a whole party |
| `organ` | Committee the document was referred to |
| `titel`, `undertitel`, `text` | Content |
| `forslag_text` | The proposal texts concatenated — high-signal, weighted separately in search |
| `parties`, `author_names` | Denormalised authorship, in signing order |
| `forslag`, `bilagor` | Raw proposal and attachment records from the source |
| `has_text` | False for documents that exist only as scanned PDFs |

### `motion_yrkanden` — individual proposals within a document

| Column | Meaning |
|---|---|
| `lydelse` | The proposal's wording |
| `utskottet` | The committee's recommendation |
| `kammaren` | The chamber's decision, e.g. `Bifall` (approved) / `Avslag` (rejected) |
| `behandlas_i` | Which report handled it |

Values are stored exactly as published and never translated. `decisions:` in
`parliament.yaml` glosses them so prompts can explain them.

### `people` — the register of members

`intressent_id` is the source's person id and the join key used everywhere.
`valkrets` is the electoral district; its meaning varies by country — a single-member
constituency in the UK, a multi-member district in Sweden — and nothing in the code
assumes either.

### `chunks`, `motion_chunks` — passages with embeddings

Text split into passages, each with a `vector(N)` embedding under an HNSW cosine
index. `N` must match `embeddings.dimension` in `parliament.yaml`; the application
checks this at startup because a mismatch otherwise fails deep inside pgvector.

### `debates` — aggregated debates

One row per debate, keyed `"{date}:{index}"`, holding a synthesised summary and the
list of speeches it covers.

## Application tables

Already country-neutral: `users` and `auth_tokens` (zero-knowledge auth),
`chat_sessions` and `chat_snapshots` (encrypted chat storage and shared snapshots),
`research_boards`, `research_threads`, `jobs`, `job_events` (deep research),
`error_log` and `llm_events` (observability), and the `eval_*` tables.

Two notes:

`chat_snapshots.llm_messages` holds the full tool-call history so a forked snapshot
resumes with the model's context. Stat cards replay the SQL stored there, which is
why renaming a column is not a clean break — old SQL has to keep working.

`session_type` is `'general'` or `'mp'`. The `'mp'` value means a chat conducted in
the persona of a member; read it as "member", not as anything UK-specific.

## Full-text search

Every search vector is built with the configuration named by `language.fts_config`,
read at runtime from `current_setting('app.fts_config')` inside the triggers. Set it
per database:

```sql
ALTER DATABASE plenum SET app.fts_config = 'swedish';
```

This only takes effect for new sessions, so restart the API after changing it.
