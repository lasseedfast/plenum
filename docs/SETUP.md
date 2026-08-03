# Setting up plenum

Written to be followed step by step, by a person or an assistant. Every step has a
command that proves it worked. Do not proceed past a failing check — later failures
will be confusing and unrelated-looking.

If you are an assistant: run the verification command after each step and report its
actual output. Do not infer success from a command exiting 0.

---

## 0. What plenum needs from you

Four external things. They are independent — you can swap any one without touching
the others.

| # | Thing | Why | Can you skip it? |
|---|---|---|---|
| 1 | **PostgreSQL 14+ with pgvector** | Stores everything; does full-text and vector search | No |
| 2 | **A chat model endpoint** | Powers chat and deep research | Search works without it; chat does not |
| 3 | **An embeddings endpoint** | Turns text into vectors for semantic search | Full-text search works without it; `vector_search` does not |
| 4 | **Source data** | A parliament's open data | No |

2 and 3 are **separate** and usually different servers. A common mistake is pointing
both at one URL and wondering why embedding fails.

---

## 1. PostgreSQL

Needs the `vector` extension (pgvector) and `pg_trgm`.

```bash
docker run -d --name plenum-pg \
  -e POSTGRES_USER=plenum -e POSTGRES_PASSWORD=changeme -e POSTGRES_DB=plenum \
  -p 5432:5432 pgvector/pgvector:pg16
```

Or on an existing server: `CREATE EXTENSION vector; CREATE EXTENSION pg_trgm;`

Set the text-search dictionary for your language. This is read by database triggers,
so it must be set on the database itself, not just in config:

```bash
psql -h localhost -U plenum -d plenum -c \
  "ALTER DATABASE plenum SET app.fts_config = 'swedish';"
```

Pick from `SELECT cfgname FROM pg_ts_config;`. Postgres ships stemmers for about two
dozen languages. If yours is absent, use `simple` — search still works but without
stemming, so `kärnkraften` will not match a search for `kärnkraft`.

Apply the schema:

```bash
psql -h localhost -U plenum -d plenum -f _postgres/schema.sql
```

**Verify:**

```bash
psql -h localhost -U plenum -d plenum -c \
  "SELECT count(*) AS tables FROM information_schema.tables WHERE table_schema='public';
   SELECT current_setting('app.fts_config') AS fts;"
```

Expect 22 tables and your chosen dictionary. If `fts` errors with "unrecognized
configuration parameter", the `ALTER DATABASE` did not run or you reconnected before
it took effect — it applies to new sessions only.

---

## 2. The chat model

plenum talks to anything speaking the **OpenAI chat-completions protocol**. It never
uses a provider's native API.

### Choosing

| Situation | Use | Notes |
|---|---|---|
| You have a GPU | **vLLM** | Fastest. Handles concurrent requests properly. |
| No GPU, want it working today | **Ollama** | Easiest. Slow for deep research, fine for chat. |
| You want no infrastructure | **OpenRouter** | One key, many models. |
| Data must stay in the EU | **Berget AI** | Swedish provider, EU-hosted. |
| You already have OpenAI | **OpenAI** | Works; reasoning models are handled specially. |

The model needs **tool calling**. Without it, chat cannot search and will make things
up — which for this project is the one unacceptable failure. Verified working:
Qwen3 (8B and up), GPT-4o and later, Claude, Llama 3.3, Mistral Large.

Below ~7B parameters, tool calling gets unreliable. If chat answers without citing
sources, suspect the model before the code.

### Setting it up

**vLLM**

```bash
vllm serve Qwen/Qwen3-8B --port 8000
```

In `.env`:
```
LLM_DIRECT_URL=http://localhost:8000/v1
LLM_MODEL_SMART=Qwen/Qwen3-8B
LLM_MODEL_FAST=Qwen/Qwen3-8B
LLM_BEARER=
```

**Ollama**

```bash
ollama serve
ollama pull qwen3:8b
```

In `.env`:
```
LLM_DIRECT_URL=http://localhost:11434/v1
LLM_MODEL_SMART=qwen3:8b
LLM_MODEL_FAST=qwen3:4b
LLM_BEARER=
```

The `/v1` matters. Ollama's native API is on `/api` and is not OpenAI-compatible.

**A hosted provider as the server default**

```
LLM_DIRECT_URL=https://openrouter.ai/api/v1
LLM_BEARER=sk-or-v1-...
LLM_MODEL_SMART=anthropic/claude-sonnet-4
LLM_MODEL_FAST=anthropic/claude-haiku-4
```

`LLM_BEARER` is the server's own key. Everyone using your site spends it — only do
this behind authentication.

### Letting users bring their own key

Copy `providers.template.yaml` to `providers.yaml` and keep the providers you want.
Anything with `user_api_key: true` appears in the model picker and prompts the user
for a key. That key is held for the request only, and is never written to the database
or logs; the sole persisted copy is encrypted in the user's own browser.

**Verify — this calls the model for real:**

```bash
.venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv()
import os
from packages.llm import LLM
llm = LLM(base_url=os.getenv('LLM_DIRECT_URL'), model=os.getenv('LLM_MODEL_SMART'),
          api_key=os.getenv('LLM_BEARER') or None, silent=True)
r = llm.generate(messages=[{'role':'user','content':'Reply with the word OK.'}], think=False)
print('FAILED:', r) if isinstance(r, str) else print('OK:', r.content[:60])
"
```

A string starting `LLM request failed:` means it did not work — the message names the
cause. `OK:` followed by text means the endpoint, model name and key are all correct.

**Verify tool calling separately** — a model can chat fine and still not call tools:

```bash
.venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv()
import os
from packages.llm import LLM, register_tool, get_tools

@register_tool
def get_population(country: str) -> str:
    '''Return a country's population.

    Args:
        country: Country name.
    '''
    return f'{country}: 10.4 million'

llm = LLM(base_url=os.getenv('LLM_DIRECT_URL'), model=os.getenv('LLM_MODEL_SMART'),
          api_key=os.getenv('LLM_BEARER') or None, tools=get_tools(['get_population']), silent=True)
llm.generate(messages=[{'role':'user','content':'Use the tool: population of Sweden?'}], think=False)
calls = [m for m in llm.messages if m.get('role') == 'tool']
print('TOOL CALLING WORKS:', calls) if calls else print('NO TOOL CALL — pick a different model')
"
```

---

## 3. Embeddings

Separate from chat, and configured separately. Also OpenAI-compatible.

```
EMBEDDING_BASE_URL=http://localhost:8003/v1
EMBEDDING_API_KEY=
LLM_MODEL_EMBEDDING=qwen3-embedding
```

**The dimension must match the database.** `parliament.yaml` declares
`embeddings.dimension: 384`, and the `vector(N)` columns are that width. Change the
model and you must change both, then re-embed the entire corpus — there is no
converting existing vectors.

plenum checks this at startup and refuses to run on a mismatch, because the failure
otherwise surfaces deep inside pgvector with a message that never mentions
configuration.

Options:

| Approach | Command |
|---|---|
| vLLM | `vllm serve Qwen/Qwen3-Embedding-0.6B --port 8003` |
| Ollama | `ollama pull nomic-embed-text` → `EMBEDDING_BASE_URL=http://localhost:11434/v1`, dimension **768** |
| OpenAI | `EMBEDDING_BASE_URL=https://api.openai.com/v1`, model `text-embedding-3-small`, dimension **1536** |

If you change the dimension, edit `parliament.yaml` **before** creating the schema —
`schema.sql` reads it. On an existing database you must alter every `vector(N)` column
and re-embed.

**Verify:**

```bash
.venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv()
from postgres_client import pg
from parliament import PARLIAMENT
v = pg.make_embeddings(['test'])[0]
print(f'got {len(v)} dims, config expects {PARLIAMENT.embeddings.dimension}')
print('MATCH' if len(v) == PARLIAMENT.embeddings.dimension else 'MISMATCH — fix before ingesting')
"
```

---

## 4. Source data

For Sweden, everything is already configured:

```bash
.venv/bin/python -m ingest.cli fetch --source documents --range 2022-2025
.venv/bin/python -m ingest.cli load  --source documents
.venv/bin/python scripts/make_embeddings.py documents
```

Start with one range. A full corpus is tens of GB and takes hours.

For another parliament, see [PORTING.md](PORTING.md) — you write a config file and an
adapter.

**Verify:**

```bash
psql -h localhost -U plenum -d plenum -c "
SELECT count(*) AS documents, count(search_vector) AS searchable FROM documents;"
```

Both numbers should be equal and non-zero. If `searchable` is 0, the search-vector
trigger did not fire — check `app.fts_config` from step 1.

---

## 5. Run it

```bash
.venv/bin/uvicorn backend.app:app --port 8000    # API
cd frontend && npm install && npm run dev        # UI on :5173
```

**Verify the whole stack:**

```bash
curl -s localhost:8000/api/meta | head -c 200
curl -s -X POST localhost:8000/api/search -H 'Content-Type: application/json' \
  -d '{"q":"test","limit":3}' | head -c 200
```

---

## When something fails

| Symptom | Cause | Fix |
|---|---|---|
| `LLM request failed: ... unreachable` | Wrong `LLM_DIRECT_URL`, or the server is down | `curl $LLM_DIRECT_URL/models` |
| `LLM request failed: ... 401` | Missing or wrong `LLM_BEARER` | Check the key; self-hosted usually needs none |
| Chat answers but never cites sources | Model cannot call tools | Run the tool-calling check in §2 |
| `column X does not exist` | Schema older than the code | Apply `_postgres/migrations/*.sql` in order |
| Search returns nothing, no error | `app.fts_config` wrong or unset | `SELECT current_setting('app.fts_config');` then §1 |
| `expected N dimensions, got M` | Embedding model changed | Match `parliament.yaml` to the model, re-embed |
| Startup: `app.fts_config is 'x' but parliament.yaml declares 'y'` | The two disagree | Make them match; the database wins |
| `No LLM endpoint configured` | `LLM_DIRECT_URL` unset | Set it in `.env` |
| Service starts, then model calls fail with an empty model name | `EnvironmentFile=` in a systemd unit | Remove it. systemd's parser rejects `KEY =` with a space; let python-dotenv read `.env` |
| `.env` values missing in a shell script | `source .env` aborts on unquoted parentheses | Use python-dotenv, not `source` |

## Notes that save time later

**Nothing here is global.** Chat provider, embedding provider and database are three
independent choices. Changing the chat model needs no re-indexing; changing the
embedding model needs a full re-embed.

**Prompts are files.** `prompts/<lang>/*.md`. Set `PROMPTS_RELOAD=1` to re-read them
per call and iterate without restarting.

**Keep deployment values out of the repo.** `PARLIAMENT_CONFIG`, `PROMPTS_DIR` and
`CONTENT_DIR` each point somewhere else on disk, so a deployment's own settings never
appear in a diff against upstream.

**`database_query` runs model-written SQL.** Two layers restrict it to reads, but give
it a `SELECT`-only database role anyway. See [SECURITY.md](../SECURITY.md).
