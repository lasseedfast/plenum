# Setting up plenum with an AI assistant

Most people setting this up will do it with an assistant rather than alone. This page
is written for that: a prompt to start with, and the interview the assistant should run.

---

## Give your assistant this

> I want to set up **plenum** for the *[your parliament]*. The repository is at
> `[path]`. Read `docs/ASSISTANT-SETUP.md` and walk me through it — ask me the
> questions in the interview one section at a time, run the checks yourself, and tell
> me what you find before changing anything. I am not an expert on databases or git,
> so explain trade-offs in plain terms and tell me when a choice does not matter.

---

## For the assistant

Your job is to end with a running deployment and a user who understands what was set
up. Rules:

1. **Run `python scripts/doctor.py` first**, before asking anything. It reports the
   machine's actual state — Python version, database, models, GPU, free ports. Several
   interview questions answer themselves from its output; do not ask what you can see.
2. **Ask one section at a time.** Do not present all twenty questions at once.
3. **Recommend a default for every question** and say why. Most users want to be told
   what is sensible, not handed a menu.
4. **Never invent a value.** If you do not know the parliament's party colours or its
   open-data URL, say so and ask, or leave the default and flag it.
5. **Verify each stage before moving on.** Every section below has a check. Report its
   real output; never infer success from an exit code.
6. **Prefer adding files to editing them.** See [YOUR-PARLIAMENT.md](YOUR-PARLIAMENT.md).
   Getting this wrong makes every future update painful.

---

### Section 1 — What is this deployment?

Ask:

- Which parliament? (country, and the body's name in its own language and in English)
- What should the site be called?
- Which language will the interface and the model's answers be in?
- Is there an existing site whose look it should resemble, or should it look neutral?

Then write `parliament.<code>.yaml` — a **new file**, not an edit of `parliament.yaml` —
and set `PARLIAMENT_CONFIG=parliament.<code>.yaml` in `.env`.

> **Quote short codes.** YAML reads unquoted `no`, `yes`, `on`, `off`, `y`, `n` as
> booleans. `country: NO` becomes false. Write `country: "NO"`.

Check: `python -c "from parliament import PARLIAMENT; print(PARLIAMENT.meta)"`

### Section 2 — How should it look?

The shipped design is deliberately Nordic-institutional — warm paper, deep blue, a
Garamond serif — and was drawn from riksdagen.se. **It will look Swedish anywhere you
deploy it** unless changed.

Ask whether that is fine, or whether it should match their own parliament's visual
language. If the latter, ask for a primary colour and whether they want a serif
(traditional, institutional) or sans-serif (plainer, more like a tool) for headings.

Set the `theme:` block in their config. Every key maps to a CSS variable, so nothing in
the stylesheet needs editing.

Check: `curl -s localhost:8000/api/meta | python -m json.tool | grep -A8 theme`

### Section 3 — The database

Usually decided by the doctor output. If PostgreSQL is missing, offer the container:

```bash
docker run -d --name plenum-pg \
  -e POSTGRES_USER=plenum -e POSTGRES_PASSWORD=<generate one> -e POSTGRES_DB=plenum \
  -p 5432:5432 pgvector/pgvector:pg16
```

Ask only what you cannot detect: whether an existing server should be used, and whether
they have rights to `CREATE EXTENSION` on it.

Set `app.fts_config` to a dictionary from `SELECT cfgname FROM pg_ts_config;` matching
their language. If there is none, `simple` works without stemming — tell them the
consequence: a search for a word will not match its inflected forms.

Check: `python scripts/doctor.py` — the PostgreSQL section should be all OK.

### Section 4 — The chat model

Read the doctor's GPU line before asking.

| What the doctor found | Recommend |
|---|---|
| A GPU with 12 GB or more | vLLM — fastest, handles concurrency |
| A GPU under 12 GB, or none | Ollama — easy, slower, or a hosted provider |
| No GPU and no wish to run models | OpenRouter, or Berget if EU data residency matters |

Ask: are they willing to pay per request, or does this need to run on their own
hardware? Does the data have to stay in a particular jurisdiction?

If local, offer to pull a model. **It must support tool calling** — without it, chat
answers without searching, which is the one failure this project cannot tolerate.
Qwen3 8B and above is a good default. Below ~7B, tool calling gets unreliable.

Check: `python scripts/doctor.py` — both "Chat model" and "Tool calling" must be OK. If
tool calling fails, change the model. Do not proceed.

### Section 5 — Embeddings

A **separate** endpoint from chat, and the most commonly confused step.

Ask whether to run embeddings locally (small model, ~2 GB VRAM, free) or via an API.

The dimension is load-bearing: it must match `embeddings.dimension` in their config and
the `vector(N)` columns. Changing it later means re-embedding everything. Settle it
**before** the schema is created.

| Model | Dimension |
|---|---|
| `Qwen/Qwen3-Embedding-0.6B` | 384 |
| `nomic-embed-text` (Ollama) | 768 |
| `text-embedding-3-small` (OpenAI) | 1536 |

Check: the doctor's Embeddings section must say the dimension matches.

### Section 6 — The data

Ask for their parliament's open-data URL and whether it publishes bulk archives or only
a per-record API.

If it is not Sweden, an adapter has to be written: `ingest/adapters/<name>.py`. Read
`ingest/adapters/riksdagen.py` first — it documents the traps that recur across
sources: repeated elements arriving as an object when there is one and a list when
there are several, null serialised as the string `"None"`, HTML in text fields, party
codes in mixed case.

Do not write an adapter from guesswork. Fetch one real record, show the user its
actual fields, and map from that.

Check: load a small batch and inspect it —
`python -m ingest.cli load --source documents --limit 50`, then query a few rows and
show them to the user. Wrong-looking data here is far cheaper to fix than after a
full load.

### Section 7 — Telling the model what the columns mean

Once the tables exist and a batch is loaded, the model still has to be told which
columns it may use and what they mean. That description is **generated from the
database**, so it cannot drift out of date the way a hand-written one does.

The rule: **a column reaches the prompt only if it has a comment**, and the comment is
the description.

| comment on the column | what happens |
|---|---|
| none | the build fails until someone decides |
| `-` | shown; the name and type already say it |
| `[hide] reason` | not shown, and the reason is recorded for the next person |
| any other text | shown, and the text becomes the note the model reads |

Start by running the generator. It refuses to write anything while a column is
undecided, and lists exactly which:

```bash
python scripts/generate_schema_prompt.py
```

Work through that list with the user. **Do not invent meanings.** For every column you
are not certain about, look before you write:

```sql
SELECT <column>, count(*) FROM <table> GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
SELECT count(<column>) * 100.0 / count(*) FROM <table>;   -- how much is filled
```

That query is the whole job, and it is where the value is. Real examples of what it
catches, from the Swedish deployment:

- a column named `status` that sounded like the outcome of a vote but held publishing
  workflow states — hidden, because a model would have used it to answer "was it
  approved?" and been wrong
- `birth_year` stored as `text`, so `WHERE birth_year > 1970` errors — noted
- a column filled for only 66 % of rows, which silently undercounts any `GROUP BY` —
  noted with its percentage
- a `doc_type` column with exactly one distinct value — hidden, since filtering on a
  constant tells the model nothing
- two columns whose names suggested a join that matches 29 rows out of 428,000 — hidden

Guidance for what to write:

- **Hide by default.** Ingest bookkeeping, source URLs, image paths, embeddings and
  pipeline flags are all noise to a model answering questions about politics. Roughly
  half the columns should end up hidden; that is normal.
- **Write a note only when the name misleads, the values would be guessed wrong, or
  the coverage changes the answer.** Everything else gets `-`. A note that restates the
  column name costs context and teaches nothing.
- **One line, 80 characters.** Longer explanations belong in the prose in
  `prompts/en/_shared/schema.md`. The tests enforce both this and a total size budget,
  because this block reaches every SQL-capable agent on every turn.

Put the statements in a migration so they survive a rebuild — copy
`_postgres/migrations/add_column_comments.sql` and rewrite it for their schema. Then
regenerate and commit the result.

Check: `python scripts/generate_schema_prompt.py` writes without complaining, and
`pytest tests/test_schema_comments.py` passes.

Tell the user the routine, because it is theirs from now on: **when a migration adds a
column, add its comment in the same migration and regenerate.** Renames and drops look
after themselves — a comment follows its column through `ALTER TABLE ... RENAME` and
disappears with a `DROP` — so only genuinely new columns need a decision, and the test
suite will ask for it.

### Section 8 — Serving it

Ask whether this is a personal machine or a public site.

Public means: nginx, TLS, systemd. Templates are in `deploy/examples/`, with
`__PROJECT_ROOT__` and `__DOMAIN__` to substitute.

Two things to state plainly rather than assume they know:

- `database_query` runs model-written SQL. It is restricted to the corpus tables by the
  guard, but it should also connect as a role granted `SELECT` on those tables and
  nothing else — otherwise a prompt injection can read your users table. Run
  `_postgres/migrations/add_llm_role.sql` and set `PG_LLM_USER`. Note the application
  itself cannot be read-only; it writes chat sessions. See [SECURITY.md](../SECURITY.md).
- Do **not** add `EnvironmentFile=` to the systemd unit. systemd's parser rejects
  `KEY =` with a space, silently yielding empty values. The app loads `.env` itself.

Check: `curl -s -o /dev/null -w '%{http_code}' https://<domain>/api/meta` → 200.

---

## Finish by telling them

- Where their config lives and that it is theirs to edit
- That `python scripts/doctor.py` diagnoses most later problems
- That updates come via `git fetch upstream && git merge upstream/main`
- Which parts are still Swedish: the interface strings are not translated unless they
  did it, and `content/` holds site copy they will want to rewrite

If anything was left unfinished — no adapter yet, no data loaded, TLS not set up — say
so explicitly rather than implying it is done.
