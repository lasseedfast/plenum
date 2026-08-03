# Citation coverage scoring

An **optional** add-on to the [evaluation harness](eval-harness.md). It measures how
well a cited source actually supports the paragraph citing it, filling in
`eval_judgments.coverage_score`.

Everything else in the harness works without it. If the scorer is unreachable the run
completes normally and `coverage_score` is left NULL.

## Why it exists

The harness already asks a judge model whether a paragraph is supported by its
citations. That is a generative call: slow, and it answers in prose that has to be
parsed.

A cross-encoder answers a narrower question — *how relevant is this source to this
text?* — as a single number. It reads the source and the paragraph together and emits
one score with no generation at all, so it runs roughly an order of magnitude faster
and costs almost no VRAM.

The two measure different things and are worth having together: the judge catches
claims a source contradicts, the scorer catches claims a source simply does not cover.

## Running it

Any endpoint implementing OpenAI's `/v1/score` or `/v1/rerank` will do. With vLLM:

```bash
docker run --gpus all -p 8005:8000 --name plenum-scorer \
  vllm/vllm-openai \
  --model BAAI/bge-reranker-v2-m3 \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.2
```

`bge-reranker-v2-m3` is multilingual and small — around 2 GB of VRAM, so it coexists
with a chat model on one consumer GPU.

Then point the harness at it:

```bash
export SCORER_ENDPOINT=http://localhost:8005/v1/score
```

> **Port note:** the MCP server (`make mcp`) also defaults to 8001, which is why the
> example above uses 8005. If you run both, give them different ports.

**Verify:**

```bash
curl -s http://localhost:8005/v1/score \
  -H 'Content-Type: application/json' \
  -d '{"model":"BAAI/bge-reranker-v2-m3",
       "text_1":"Riksdagen beslutade om ny kärnkraft.",
       "text_2":"Vad sa riksdagen om kärnkraft?"}' | head -c 200
```

A JSON body containing a score means it works. Connection refused means the container
is not running, and the harness will skip scoring rather than fail.

## Reading the result

`coverage_score` is 0–1, the sigmoid of the model's logit.

| Range | Reading |
|---|---|
| > 0.8 | The source directly supports the paragraph |
| 0.4 – 0.8 | Related, but the paragraph may overreach |
| < 0.4 | The citation does not support the claim — worth reading by hand |

Low scores are the interesting ones. A run where the judge says "supported" but
coverage is low usually means an answer that is technically defensible and practically
misleading — exactly the failure this project cares about most.

```sql
SELECT q.question, j.paragraph_text, j.coverage_score
FROM eval_judgments j JOIN eval_questions q ON q.id = j.question_id
WHERE j.coverage_score < 0.4 AND j.verdict = 'supported'
ORDER BY j.coverage_score
LIMIT 20;
```

## Implementation

`CitationScorer` in [`scripts/eval_harness.py`](../scripts/eval_harness.py). It probes
the endpoint once at startup, warns and disables itself if unreachable, and never
fails a run because scoring is unavailable.
