# Eval Harness — Chat Citation Accuracy

Runs the `ChatService` against many auto-generated Swedish questions and uses a
judge LLM to verdict whether each paragraph's citations actually support the
claim. Built after we spotted citation-misattribution bugs in production (e.g.
"Jan Björklund (M)" written under a citation that pointed to a talk by Helena
Bargholtz (L)).

Reusable across models and providers — every run is tagged with config so
results are comparable across runs.

---

## Setup

One-time migration (auto-applies on backend start; can also be run manually):

```bash
python -c "from postgres_client import pg; \
  pg.execute_void(open('_postgres/migrations/add_eval_tables.sql').read())"
```

Requires the same `.env` the backend uses (`PG_*`, `LLM_DIRECT_URL`, `LLM_BEARER`).

---

## Run

```bash
# smoke test (3 questions)
python scripts/eval_harness.py --label smoke --iterations 3

# overnight
python scripts/eval_harness.py --label "gpt-oss-20b baseline" --iterations 2000

# compare a different judge model
python scripts/eval_harness.py --label "judge=gpt-4.1" --iterations 500 \
    --judge-model gpt-4.1

# gentle pacing
python scripts/eval_harness.py --label overnight --iterations 5000 --sleep-ms 2000

# re-run the judge on questions that are missing judgments (e.g. after a judge fix)
python scripts/eval_harness.py --rejudge-run <run_id>

# re-run the judge on ALL questions in a run (wipe + redo)
python scripts/eval_harness.py --rejudge-run <run_id> --rejudge-all
```

Flags:
- `--label` (required) — human tag stored on the run row.
- `--iterations` — number of questions to generate.
- `--judge-model` — model used for BOTH the question generator and the judge.
  Defaults to `LLM_MODEL_SMART`.
- `--sleep-ms` — pause between iterations.

The script runs `ChatService.get_chat_response()` in-process (no HTTP). You can
start/stop it freely — partial runs are fine; the run row gets `finished_at`
only if the loop completes normally.

---

## How questions are generated

Two strategies, randomly picked per iteration (weighted 2:1 toward `talk_seed`):

1. **talk_seed** — samples a random row from `talks`, feeds the first 500 chars +
   speaker/party/date to the generator LLM, asks it to formulate a natural
   journalist question broader than the snippet (e.g. "what do different parties
   say about this topic?"). The talker's name is deliberately withheld so the
   chat has to rediscover it.
2. **free** — open-ended question from a broad topic palette (skola, vård,
   försvar, klimat, migration, etc.), with a rolling avoid-list of the last ~20
   questions.

Combined multi-angle questions are encouraged because they naturally exercise
more tools (keyword + vector + aggregation in one go).

---

## What gets stored

Three tables (see `_postgres/migrations/add_eval_tables.sql`):

| Table | Row granularity | Contents |
|---|---|---|
| `eval_runs` | one per CLI invocation | `label`, `config` JSONB (models + git SHA), start/end times |
| `eval_questions` | one per question | `question`, `answer`, compact `tool_trace` JSONB, `sources` JSONB, timings, any error |
| `eval_judgments` | one per answer paragraph | `paragraph_text`, `cited_indices`, `verdict`, `rationale`, `metadata_mismatch`, `coverage_score` |

### `coverage_score` (cross-encoder grounding signal)

A 0–1 probability from a `BAAI/bge-reranker-v2-m3` cross-encoder served locally on port 8001
via vLLM. For each paragraph, **all** cited source texts are concatenated (up to 28 000 chars /
≈7 000 tokens) and scored against the paragraph as a single call. The raw logit is converted
via sigmoid so 0.5 = neutral, >0.7 = likely grounded, <0.3 = likely hallucinated.

`NULL` means the scorer endpoint was unreachable when the judgment was recorded.

Start the scorer (first run downloads the model to `/home/lasse/models`):

```bash
nohup vllm serve BAAI/bge-reranker-v2-m3 \
  --port 8001 \
  --download-dir /home/lasse/models \
  --gpu-memory-utilization 0.2 \
  --max-model-len 8192 \
  --trust-remote-code \
  > /tmp/eval-scorer.log 2>&1 &
```

Override the endpoint with `SCORER_ENDPOINT=http://host:port/v1/score` if needed.

`tool_trace` is **compact** — tool name, hit IDs, counts, but no raw tool
payload text. `sources` only contains the cited sources (talk_id, speaker,
party, date, 400-char snippet).

Rows from `llm_events` and `error_log` produced during a run are auto-stamped
with `detail->>'eval_run_id'` and `detail->>'eval_question_id'` (via env vars
picked up inside `backend/services/event_logger.py`), so they can be joined
back after the fact.

---

## Verdict vocabulary

Only paragraphs that contain at least one `[N]` citation are sent to the judge —
headers, intros, and transition sentences are silently dropped before judging.
Each evaluated paragraph gets one of:

| Verdict | Meaning |
|---|---|
| `supported` | Claim is backed by the cited talk's full text. |
| `partial` | Partly correct but overstated or not fully verifiable from the talk text. |
| `unsupported` | Claim contradicts or has no basis in the cited talk text. Also used when the right speaker is cited but the described content isn't in that talk. |
| `wrong_speaker` | The name **or** party in the paragraph doesn't match who actually held the cited talk. Rationale names the correct speaker/party. |
| `wrong_attribution` | Right speaker named, but the specific claim is from a different source index than cited (e.g. content is in [7] but [3] is cited, both by the same person). |

`wrong_speaker` is the failure mode that motivated this harness; the judge's
system prompt calls it out explicitly. A non-empty `rationale` is required for
every verdict — responses without one are rejected and re-tried.

### Deterministic pre-check (`metadata_mismatch`)

Before the LLM judge runs, a deterministic check extracts `[Name](/mp/...) (PARTY)` links
from the paragraph and compares them against the cited source metadata. If name or party
doesn't match, the mismatch description is stored in `eval_judgments.metadata_mismatch`
and passed to the judge as a hint.

`metadata_mismatch IS NOT NULL` is a reliable filter for clear-cut speaker identity errors
that doesn't depend on the judge's interpretation. Use it to split deterministic catches
from LLM-caught subtleties:

```sql
-- How many wrong_speaker were caught deterministically vs by judge alone?
SELECT
  COUNT(*) FILTER (WHERE metadata_mismatch IS NOT NULL) AS det_caught,
  COUNT(*) FILTER (WHERE metadata_mismatch IS NULL)     AS llm_only,
  COUNT(*) AS total
FROM eval_judgments
WHERE verdict = 'wrong_speaker';
```

---

## Analysis

Replace `:run` with the run UUID from `SELECT id, label FROM eval_runs ORDER BY started_at DESC;`.

### Overall verdict distribution

```sql
SELECT j.verdict, COUNT(*), ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM eval_judgments j
JOIN eval_questions q ON q.id = j.question_id
WHERE q.run_id = :run
GROUP BY j.verdict
ORDER BY COUNT(*) DESC;
```

### Comparing runs

```sql
SELECT r.label, j.verdict, COUNT(*)
FROM eval_judgments j
JOIN eval_questions q ON q.id = j.question_id
JOIN eval_runs r ON r.id = q.run_id
GROUP BY r.label, j.verdict
ORDER BY r.label, j.verdict;
```

### Drill into the `wrong_speaker` failures

```sql
SELECT q.question, j.paragraph_text, j.cited_indices, j.rationale, q.sources
FROM eval_judgments j
JOIN eval_questions q ON q.id = j.question_id
WHERE q.run_id = :run AND j.verdict = 'wrong_speaker'
ORDER BY q.created_at
LIMIT 20;
```

### Which tools correlate with bad answers?

```sql
SELECT tool, COUNT(*) FILTER (WHERE bad) AS bad, COUNT(*) AS total,
       ROUND(100.0 * COUNT(*) FILTER (WHERE bad) / COUNT(*), 1) AS bad_pct
FROM (
  SELECT jsonb_array_elements(q.tool_trace)->>'tool' AS tool,
         EXISTS (
           SELECT 1 FROM eval_judgments j
           WHERE j.question_id = q.id
             AND j.verdict IN ('wrong_speaker','unsupported')
         ) AS bad
  FROM eval_questions q
  WHERE q.run_id = :run
) t
WHERE tool IS NOT NULL
GROUP BY tool
ORDER BY bad_pct DESC;
```

### Latency & iteration counts

```sql
SELECT
  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50_ms,
  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms,
  AVG(num_iterations)::numeric(10,1) AS avg_iters,
  MAX(num_iterations) AS max_iters,
  COUNT(*) FILTER (WHERE error IS NOT NULL) AS errors
FROM eval_questions
WHERE run_id = :run;
```

### Joining with the existing event logs

`llm_events` and `error_log` rows emitted during the run carry the IDs in their
`detail` JSONB:

```sql
-- All events from one question
SELECT event_type, created_at, detail
FROM llm_events
WHERE detail->>'eval_question_id' = :question_id
ORDER BY created_at;

-- All errors from a run, bucketed
SELECT error_type, COUNT(*)
FROM error_log e,
     jsonb_array_elements(COALESCE(e.detail, '[]'::jsonb)) d
WHERE detail->>'eval_run_id' = :run
GROUP BY error_type
ORDER BY COUNT(*) DESC;
```

(Simpler: `SELECT * FROM error_log WHERE detail->>'eval_run_id' = :run;`)

### Accuracy by question complexity

```sql
SELECT q.complexity,
       COUNT(*) AS paragraphs,
       ROUND(100.0 * COUNT(*) FILTER (WHERE j.verdict = 'supported') / COUNT(*), 1) AS supported_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE j.verdict = 'wrong_speaker') / COUNT(*), 1) AS wrong_speaker_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE j.verdict IN ('unsupported','wrong_speaker')) / COUNT(*), 1) AS bad_pct,
       AVG(q.num_iterations)::numeric(10,1) AS avg_iters,
       AVG(q.duration_ms / 1000.0)::numeric(10,1) AS avg_sec
FROM eval_judgments j
JOIN eval_questions q ON q.id = j.question_id
WHERE q.run_id = :run
GROUP BY q.complexity
ORDER BY q.complexity;
```

### Worst offenders — questions with multiple bad paragraphs

```sql
SELECT q.id, q.question,
       COUNT(*) FILTER (WHERE j.verdict IN ('wrong_speaker','unsupported')) AS bad,
       COUNT(*) AS total
FROM eval_questions q
JOIN eval_judgments j ON j.question_id = q.id
WHERE q.run_id = :run
GROUP BY q.id, q.question
HAVING COUNT(*) FILTER (WHERE j.verdict IN ('wrong_speaker','unsupported')) >= 2
ORDER BY bad DESC
LIMIT 20;
```

Pull the full answer for one of these with:

```sql
SELECT question, answer, sources, tool_trace
FROM eval_questions WHERE id = :question_id;
```

---

## Files touched

- `_postgres/migrations/add_eval_tables.sql` — three tables.
- `scripts/eval_harness.py` — generator + runner + judge.
- `backend/services/event_logger.py` — auto-stamps `eval_run_id` / `eval_question_id`
  from env into `llm_events.detail` and `error_log.detail`.

No changes to `ChatService` itself — the harness uses its existing
`event_callback` parameter.
