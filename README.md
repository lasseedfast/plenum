# plenum

Search, chat and research over what a parliament actually said.

plenum ingests a parliament's open data — speeches from the chamber, motions and
other member-submitted documents, and the register of members — into PostgreSQL
with full-text and vector indexes, then puts three things on top:

- **Search.** Full-text search with phrase, prefix, boolean and exclusion syntax,
  filterable by party, year, speaker and debate type.
- **Chat.** A retrieval-augmented assistant that answers from the corpus and cites
  the speeches and documents it used. Every claim links back to a source.
- **Deep research.** A background agent that proposes research threads, digs into
  each one, and produces a report with citations.

It runs in production as [rixdagen.se](https://rixdagen.se) over the Swedish
Riksdag. The Swedish configuration ships here as the worked example; everything
country-specific lives in one file.

> **Grounding is the point.** This is a tool for journalists and researchers, so an
> answer that cannot be traced to a source is a bug, not a rough edge. If you change
> the prompts or the retrieval logic, keep that property.

## How it stays country-agnostic

| Concern | Where it lives |
|---|---|
| Parties, colours, chamber activity types, vocabulary, ID shapes, source URLs | `parliament.yaml` |
| System prompts, tool descriptions | `prompts/<lang>/**.md` |
| Site copy | `content/<lang>/*.md` |
| Knowledge of one parliament's JSON shape | `ingest/adapters/<name>.py` |
| Everything else | country-neutral |

Adapting to another parliament means writing a config file, an ingest adapter, and a
set of prompts in your language — not editing the application. See
[docs/PORTING.md](docs/PORTING.md).

The database schema is country-neutral English: `speeches`, `documents`,
`document_proposals`, `person_id`, `constituency`. Concepts that have no stable
English equivalent — the Swedish *yrkande* or *riksmöte* — carry neutral column names
while the country's own word lives in `parliament.yaml`. See
[docs/SCHEMA.md](docs/SCHEMA.md).

## Requirements

- PostgreSQL 14+ with [pgvector](https://github.com/pgvector/pgvector) and `pg_trgm`
- Python 3.10+
- Node 18+ (frontend build)
- An OpenAI-compatible chat endpoint and an OpenAI-compatible embeddings endpoint.
  Self-hosted vLLM, OpenAI, OpenRouter, Berget and Gemini's compatibility endpoint
  all work; see `providers.template.yaml`.

## Quickstart

```bash
git clone https://git.edfast.se/plenum/plenum && cd plenum
cp .env.example .env         # then fill in database and model settings
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

Create the database and apply the schema:

```bash
createdb plenum && psql -d plenum -c 'CREATE EXTENSION vector; CREATE EXTENSION pg_trgm;'
psql -d plenum -c "ALTER DATABASE plenum SET app.fts_config = 'swedish';"
psql -d plenum -f _postgres/schema.sql
```

Fetch and index data. This downloads several GB and takes hours; start with one range:

```bash
python -m ingest.cli fetch --source documents --range 2022-2025
python -m ingest.cli load  --source documents
python scripts/make_embeddings.py
```

Run it:

```bash
.venv/bin/uvicorn backend.app:app --reload    # API on :8000
cd frontend && npm install && npm run dev     # UI on :5173, proxying /api
```

## Layout

```
backend/          FastAPI app: routes, search, chat, deep research
  services/         chat orchestration, tools the model can call, retrieval
  services/research/ the background research agent
packages/llm/     provider-agnostic LLM client and the tool registry
_postgres/        schema, migrations, connection pool
ingest/           fetch -> adapt -> upsert -> chunk -> embed
prompts/          system prompts and tool descriptions, per language
parliament.yaml   everything specific to one parliament
frontend/         React + TypeScript + Vite
deploy/examples/  systemd units and an nginx site, with placeholders
```

## Configuration

All settings are environment variables, documented in `.env.example`. Two paths let a
deployment keep its own values outside the repository entirely:

- `PARLIAMENT_CONFIG` — path to a `parliament.yaml` elsewhere on disk
- `PROMPTS_DIR` — path to a prompt tree elsewhere on disk

Set `PROMPTS_RELOAD=1` in development to re-read prompt files on every call.

## Security

The `database_query` tool executes model-authored SQL. Give it a database role with
`SELECT` only — the application does not currently enforce that itself, and corpus
text reaches the model's context, so treat it as untrusted input. See
[SECURITY.md](SECURITY.md).

## License

AGPL-3.0-or-later. If you run a modified version as a network service, you must offer
its source to users of that service.

If AGPL does not work for your organisation, ask — I am open to granting other terms
for newsrooms and public-interest projects. Contact details are in the repository
metadata.

## Acknowledgements

Swedish parliamentary data comes from [data.riksdagen.se](https://data.riksdagen.se)
under the Riksdag's open-data terms. plenum is not affiliated with or endorsed by the
Swedish Riksdag.
