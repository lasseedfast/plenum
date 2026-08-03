# plenum

Search, chat and research over what a parliament actually said.

plenum ingests a parliament's open data — for now *speeches* from the chamber, *motions* and
other member-submitted documents, and the *register of members* — into PostgreSQL
with full-text and vector indexes, then puts three things on top:

- **Search.** Full-text search with phrase, prefix, boolean and exclusion syntax,
  filterable by party, year, speaker and debate type.
- **Chat.** A retrieval-augmented assistant that answers from the corpus and cites
  the speeches and documents it used. Every claim links back to a source.
- **Deep research.** A background agent that proposes research threads, digs into
  each one, and produces a report with citations.

plenum is the software, not a website. You run your own instance against your own
parliament's data.

One instance exists today: [rixdagen.se](https://rixdagen.se), covering the Swedish
Riksdag. It is a fork of this repository — the same code, plus its own configuration
and server setup — and it is what the project is tested against. Sweden therefore
ships here as the worked example: a complete `parliament.yaml`, a working ingest
adapter, and prompts in Swedish. Copy those as the starting point for your own.

> **Grounding is the point.** This is a tool for journalists and researchers, so an
> answer that cannot be traced to a source is a bug, not a rough edge. If you change
> the prompts or the retrieval logic, keep that property.

## Setting this up? Start here

There are a few moving parts — a database, a chat model, an embedding model, and your
parliament's data. The quickest way through all of it is to let an AI assistant walk
you through it.

**Open this repository with your assistant of choice and paste:**

> I want to set up **plenum** for the *[your parliament]*. Read
> `docs/ASSISTANT-SETUP.md` and walk me through it — ask me the questions in the
> interview one section at a time, run the checks yourself, and tell me what you find
> before changing anything. I am not an expert on databases or git, so explain
> trade-offs in plain terms and tell me when a choice does not matter.

[**docs/ASSISTANT-SETUP.md**](docs/ASSISTANT-SETUP.md) tells the assistant what to ask,
what to check, and what not to invent. It starts by running `python scripts/doctor.py`,
which inspects your machine — database, models, GPU, free ports — so you are not asked
things that can simply be looked up.

Prefer to do it yourself? [docs/SETUP.md](docs/SETUP.md) is the same ground, manually,
with a verification command after every step.

## How it stays country-agnostic

| Concern | Where it lives |
|---|---|
| Parties, colours, chamber activity types, vocabulary, ID shapes, source URLs | `parliament.yaml` |
| System prompts, tool descriptions | `prompts/<lang>/**.md` |
| Site copy | `content/<lang>/*.md` |
| Knowledge of one parliament's JSON shape | `ingest/adapters/<name>.py` |
| Everything else | country-neutral |

Adapting to another parliament means writing a config file, an ingest adapter, and a
set of prompts in your language — not editing the application.

**Start at [docs/YOUR-PARLIAMENT.md](docs/YOUR-PARLIAMENT.md)**, which covers how to
organise the work (fork rather than clone, and add files rather than edit them, so
later updates merge cleanly). Then [docs/PORTING.md](docs/PORTING.md) for what to
write.

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

Full step-by-step setup, including how to plug in each model provider and how to
verify each stage worked, is in **[docs/SETUP.md](docs/SETUP.md)**.

> Planning to run this for your own parliament, or change anything? **Fork the
> repository first**, then clone your fork — see
> [docs/YOUR-PARLIAMENT.md](docs/YOUR-PARLIAMENT.md). Cloning directly is fine only if
> you are reading the code and will not be changing it.

The short version:

```bash
git clone https://git.edfast.se/lasse/plenum && cd plenum
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

## Documentation

**Getting it running**

| | |
|---|---|
| [docs/ASSISTANT-SETUP.md](docs/ASSISTANT-SETUP.md) | **Setting this up with an AI assistant** — a prompt to start with, and the interview it should run. Probably the fastest way in. |
| [docs/SETUP.md](docs/SETUP.md) | Install, step by step: PostgreSQL with pgvector, a chat model (vLLM, Ollama, OpenRouter, Berget, OpenAI), an embeddings endpoint, and the first data load. Every step has a command that proves it worked, plus a symptom/cause/fix table. |
| [docs/YOUR-PARLIAMENT.md](docs/YOUR-PARLIAMENT.md) | **Read this before changing anything.** Fork vs clone, how to add your parliament without creating merge conflicts, how to pull in updates, how to contribute back, and how to undo mistakes. Written for people who do not use git much. |
| [docs/PORTING.md](docs/PORTING.md) | What a non-Swedish deployment actually has to write: `parliament.yaml`, an ingest adapter, prompts in your language. Honest about which parts are real work. |

**Understanding it**

| | |
|---|---|
| [docs/SCHEMA.md](docs/SCHEMA.md) | Every table and column, and why the awkward names are what they are — what your ingest adapter has to produce. |
| [docs/sources-system.md](docs/sources-system.md) | How a claim in an answer is tied back to the speech it came from. The core of the project's grounding guarantee. |
| [docs/deep-research.md](docs/deep-research.md) | The background agent: how it proposes threads, digs, and writes a report. |
| [docs/shadow-communicator.md](docs/shadow-communicator.md) | The running commentary shown while the model works. |
| [docs/multi-provider.md](docs/multi-provider.md) | Letting each user bring their own model API key, and how that key is kept out of the database. |

**Checking it is honest**

| | |
|---|---|
| [docs/eval-harness.md](docs/eval-harness.md) | Measuring whether answers are actually supported by the sources they cite. |
| [docs/eval-scorer.md](docs/eval-scorer.md) | Optional cross-encoder scoring, for finding answers that are technically defensible but misleading. |

**Operating it**

| | |
|---|---|
| [SECURITY.md](SECURITY.md) | Model-authored SQL and why it runs read-only, API-key handling, chat privacy. Read before exposing this publicly. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Conventions, and the one rule: answers must be traceable to sources. |
| [deploy/examples/](deploy/examples/) | systemd units and an nginx site, with placeholders. |

## Configuration

All settings are environment variables, documented in `.env.example`. Two paths let a
deployment keep its own values outside the repository entirely:

- `PARLIAMENT_CONFIG` — a `parliament.yaml` elsewhere on disk
- `PROMPTS_DIR` — a prompt tree elsewhere on disk
- `CONTENT_DIR` — site copy (explainer, guide) elsewhere on disk

Set `PROMPTS_RELOAD=1` in development to re-read prompt files on every call.

**Not sure what is missing?**

```bash
python scripts/doctor.py
```

Checks Python, the database and its extensions, the text-search configuration, the chat
model, whether that model can actually call tools, the embedding dimension, and which
ports are free. Changes nothing.

### Appearance

The shipped design is deliberately Nordic-institutional and was drawn from riksdagen.se
— it will look Swedish wherever you deploy it. Override the `theme:` block in
`parliament.yaml` to match your own parliament's visual language; the values are
injected as CSS custom properties at runtime, so no stylesheet needs editing.

### Running a fork

If you maintain a deployment as a fork, keep it differing from upstream only in
files upstream does not have. Everything branded or private is either an env-var
pointer (the three above), an untracked file (`.env`, `providers.yaml`), or an
addition under `deploy/prod/`. `make check-fork-divergence` fails the build if
anything else drifts, which turns a merge conflict into a caught mistake.

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
