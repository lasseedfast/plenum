# Building an exploration/lead-generation agent over a large corpus

This is a generalized writeup of the architecture behind FUP's "Utforska" and
"Spel" features — an engine that digs through a large, growing body of source
documents, proposes investigative threads, and deepens them over many runs
without ever loading the whole corpus into one LLM call. The mechanisms below
are not FUP-specific; they're the reusable shape for *any* "explore a big
pile of data and keep finding more" feature.

The one-sentence version: **don't build one big agent with a huge context —
build a small, cheap scheduler that repeatedly launches short-lived,
bounded research trips, and save real progress after every trip.**

---

## 1. Two-level loop, not one big agent

Split the system into two loops with very different jobs:

- **Outer loop (scheduler)** — cheap, deterministic, no LLM (or a tiny one).
  Its only job is: *which topic/thread/lead should I work on next, and have
  I done enough of them?* It reads/writes a small amount of state (depth
  counters, status flags) and decides what to hand to the inner loop.
- **Inner loop (research trip)** — a bounded ReAct-style tool loop
  (LLM proposes a tool call → you execute it → feed back a truncated
  result → repeat) capped at a small fixed number of turns (e.g. 5–6),
  followed by one forced "now synthesize what you found" call with a
  small output-token cap.

Each trip is **one topic, one shot, short context** — never the whole
investigation history. The trip returns a small structured result
(findings, open questions, next leads), which the outer loop merges into
persistent storage and uses to decide what to explore next.

Why this matters: a single flat agent loop that tries to "explore
everything" inevitably accumulates context linearly with how much it has
already discovered. Splitting into trips means context size is bounded by
*one topic's* findings, not by total findings across the whole run.

```
outer_loop:
    while not done:
        topic = pick_next_topic(state)      # cheap, deterministic
        if topic is None: break
        result = research_trip(topic)       # bounded inner loop
        merge_and_save(state, topic, result)  # persist immediately
```

---

## 2. Never let the LLM read the raw corpus — retrieve, then feed small pieces

The inner loop's tools should never expose "read this whole 500-page
document." Put a retrieval layer between the agent and the data:

- **Hybrid retrieval** (lexical/BM25 + vector/embeddings, fused with
  something like Reciprocal Rank Fusion) narrows a huge corpus down to a
  handful of relevant chunks *before* any LLM sees text.
- **Merge adjacent high-scoring chunks** from the same document into a
  coherent passage (instead of returning isolated, disjointed chunks) so
  the model gets natural reading context — but still capped in size.
- **Hard character caps everywhere**, with an explicit "truncated" flag
  rather than silent data loss: cap snippet length, cap full-text reads,
  cap the size of any single tool result. Make the truncation visible in
  the returned data (`truncated: true`) so the agent — and you, debugging
  later — can tell when it happened.
- **Cap breadth per source** (e.g. "max 3 passages from the same
  document") so one huge or noisy document can't crowd out everything
  else in a retrieval result.

The agent's tools are the only way it can "see more" — and every tool is
retrieval-bounded, never a raw dump.

---

## 3. Delegate expensive reads to a sub-agent that returns a distilled answer

Sometimes retrieval isn't enough — you genuinely need a model to read one
full long document to answer a specific question. Don't inline that into
the orchestrating agent's context. Instead:

- Give the orchestrator a tool like `read_document_for(question, doc_id)`.
- That tool's implementation is *itself* an LLM call — often on a cheaper
  model — that loads the full document (or a size-capped slice of it) and
  answers only the specific question.
- Only the short answer string gets appended to the orchestrator's
  conversation. The full document text never enters the orchestrator's
  context at all.

This turns "read everything to be sure" into "delegate the reading, keep
only the conclusion" — the orchestrating agent's context grows with the
*number of questions asked*, not the *size of what was read* to answer
them.

---

## 4. Compact context between phases, don't just keep appending

If a pipeline has multiple phases that logically continue one conversation
(e.g. phase 1 finds candidates, phase 2 confirms them, phase 3 finalizes),
don't hand phase 2 the raw, verbose tool-call transcript from phase 1.
Collapse it first:

- Turn `assistant.tool_calls` + tool-result pairs into a single short
  plain-text note ("In phase 1 we found: X, Y, Z") before starting the
  next phase.
- Watch for models that degrade after long chains of compacted
  system/user-only messages with no assistant turns in between — you may
  need to inject a small synthetic "assistant acknowledges" turn to keep
  some models from returning empty output.

---

## 5. Persist after every atomic step — that's your resumability story

Don't design for "the whole run either finishes or state is lost." Instead:

- Store one small state document per unit-of-work (per project, per
  thread, per case — whatever your top-level grouping is).
- After *every single trip*, write the updated state back immediately.
- Track a `depth` (or "trips completed") counter per topic and a target
  ceiling. The scheduler's only job on resume is: look at what's below
  target, keep going. A crash, restart, or manual cancel mid-run leaves
  real, saved progress — there's no separate "checkpoint" mechanism needed
  because the atomic unit of work *is* the checkpoint.
- Make cancellation cooperative: thread an `is_cancelled()` check through
  the loop and check it between trips (not mid-trip), plus a wall-clock
  watchdog as a backstop for jobs that hang.

---

## 6. Idempotency via content hash, not timestamps

Before redoing expensive discovery work, check "has the underlying data
actually changed" rather than "how long ago did I last run this":

- Hash the inputs that matter (e.g. sorted IDs of source documents plus
  a summary/synopsis of the corpus), salt it with a `logic_version` int
  you bump whenever you change the algorithm itself.
- If the hash matches what's stored, skip straight to "nothing to do" —
  cheap to check, avoids wasted LLM calls on unchanged data, and forces a
  clean re-run whenever you ship a logic change (bump the version).

---

## 7. Deterministic dedup, not LLM self-policing

Don't ask the model "please don't repeat yourself" — it will, especially
across many separate trips that don't share full context. Instead:

- Normalize text (lowercase, strip whitespace/punctuation) and dedup
  findings/questions/leads by that normalized key.
- Dedup relational leads by a structural key like `(kind, target_id)`,
  not by text similarity.
- When merging new trip results into existing state, this dedup step is
  what makes "run another trip" additive instead of duplicative.
- Tell the *next* trip what's already known — but only as a short list of
  distilled labels/headlines (capped to something like 10–15 items), not
  full previous findings. This avoids re-deriving the same thing without
  re-inflating context.

---

## 8. Depth/stopping: structural limits, LLM picks direction

Keep "when do we stop" simple and structural, and let the LLM only
influence *which direction* to go, not *how long* to keep going:

- Hard depth ceiling per topic (a config number, not learned).
- Greedy breadth-leveling: always deepen the shallowest topic next, so
  every thread gets attention before any one thread runs away.
- A hard safety cap on total trips per run (e.g.
  `max_topics * target_depth + slack`) so a bug can't spin forever.
- Direction comes from the LLM: each research trip proposes "leads" —
  next entities/topics worth following — and the next trip's seed is
  drawn from the previous trip's leads. This is what makes the system
  feel like it's "exploring deeper" rather than repeating the same
  question.
- If you also want a fast, LLM-free "candidates" list (e.g. for a UI that
  needs to render instantly), a separate deterministic scorer over your
  graph/edges (fixed priority weights per relationship type + a minimum
  corroboration count to filter out noise) is a good complement to the
  LLM-driven deep-dive — cheap, instant, and never hallucinates a
  connection that isn't in the data.

---

## 9. Agentic tool-loop safety nets

On top of the basic "LLM calls tools until it stops" loop, add:

- **Dedup identical calls** within a session (hash tool name + args) so a
  confused model can't loop on the same call.
- **Once-only tools** for anything expensive/orienting (like a full
  case/corpus overview) — cap it to once per session and instruct the
  model accordingly.
- **Graceful degradation over hard failure** for tools that return large
  payloads: if a "full text" tool gets called too many times in one
  session, strip the heavy field from further calls and nudge the model
  toward a lighter alternative tool, instead of erroring out.
- **A tool can itself be a bounded sub-agent** — nesting is fine as long
  as each level is still bounded (turns, tokens, chars).

---

## 10. Model tiering — spend the expensive model only where it matters

Don't use one model for everything:

- **Small/cheap model**: high-volume mechanical work — classification,
  extraction, page-by-page reads, thread-discovery seeding.
- **Big/default model**: synthesis, structured findings, anything that
  needs real reasoning quality.
- **"Smart"/expensive tier reserved for user-interactive moments only**
  (live chat, live dialogue) — never used in unattended batch jobs, so
  it's safe to point at your priciest model without runaway cost.
- **Vision tier** routed automatically whenever a call includes images.
- Make this a config-level "role → model" resolution, not hardcoded per
  call site, so you can retune cost/quality tradeoffs in one place.
- **Detect context-length errors explicitly** (pattern-match the
  provider's error message) and automatically retry the same call on the
  small/cheap model as a fallback, rather than crashing the whole job.

---

## 11. Prompt-prefix caching for repeated-persona calls

If part of your system reuses the same large system prompt many times
(e.g. an NPC/persona, a fixed tool-schema preamble), build that prompt
**deterministically** and put anything session-specific *after* it, not
interleaved. That lets the provider's automatic prefix caching kick in.
You can even fire a cheap "priming" call the instant a session starts (before
the user's first real message) purely to warm that cache before the real
user question arrives.

---

## 12. Concurrency: sequential where it's stateful, parallel where it's independent

- **Keep dependent, stateful steps sequential.** If multiple trips would
  read-modify-write the same shared state document, running them in
  parallel just creates races. One trip at a time, in a fixed order
  (e.g. shallowest-topic-first), is simpler, cheaper on rate limits, and
  trivially debuggable.
- **Parallelize only genuinely independent, page/item-level batch work**
  (e.g. classify page N of a document — no shared mutable state, no
  cross-item dependency) using a plain thread pool, since LLM calls over
  HTTP are I/O-bound blocking calls — you don't need asyncio for this,
  a `ThreadPoolExecutor`-style parallel map is enough. Cap worker count
  via config.
- **Run long/heavy jobs in a separate OS process**, not inside your API's
  request loop — CPU-bound Python work (or GIL contention from many
  threads) will otherwise stall request handling. Communicate progress
  and cancellation through your database (heartbeats, status docs), not
  shared memory, so it also naturally supports the API server restarting
  mid-job.
- **Cooperative cancellation**: every long loop threads an `is_cancelled`
  callback and checks it between trips/items, plus a wall-clock watchdog
  and a heartbeat-based reaper that finalizes abandoned jobs.

---

## Putting it together: minimal architecture checklist for a new app

1. Define your "unit of work" (a project, a case, a topic board — whatever
   groups related exploration together) and store one small state
   document per unit.
2. Build a retrieval layer (hybrid search, chunked + capped) — this is
   what keeps every tool call bounded regardless of corpus size.
3. Write the inner "research trip": bounded tool loop (turns capped) +
   forced structured synthesis call (output tokens capped) + hard
   truncation on every tool result.
4. Write the outer scheduler: pick next topic (shallowest first or
   priority-scored), enforce depth ceiling + safety cap on total trips,
   merge new results into state with deterministic dedup, save
   immediately.
5. Add idempotency: content hash + logic version, skip if nothing
   relevant changed.
6. Add model tiering config (small/big/smart/vision) and route each call
   site explicitly.
7. Add job infrastructure: separate process, cancellation flag checked
   between (not during) trips, heartbeat watchdog.
8. Only after all of the above works sequentially and correctly, consider
   parallelizing the genuinely independent batch stages (not the
   stateful deepening loop).

The throughline across all of these: **bound everything (turns, tokens,
chars, depth, breadth) explicitly and structurally, save progress after
every bounded step, and let retrieval — not raw context — be how the
system "sees" a large corpus.** None of this requires a fundamentally
different kind of model or a fancy planning algorithm; it's disciplined
plumbing around a very ordinary tool-calling loop.

---

## Appendix: where this pattern lives in this codebase (FUP-specific)

For reference, if you want to go read the actual implementation this guide
was derived from:

- `backend/app/features/explore/brain.py` — the Utforska orchestrator
  (discovery, thread build/deepen, storage).
- `backend/app/features/explore/leads.py` — the separate, deterministic
  (LLM-free) graph-edge lead scorer.
- `backend/app/features/game/scenario.py` — the fuller Fallspel pipeline
  (foundations → cast → world → thread discovery → assembly).
- `backend/app/features/game/research.py` — the shared `research_thread`
  agentic research engine reused by both features.
- `backend/app/features/case_backbone/service.py` and `subagent.py` — the
  three-phase event-extraction pipeline with the "sub-agent reads full
  document, returns short answer" pattern and context compaction between
  phases.
- `backend/app/features/chat/tools.py` + `backend/app/features/search/unified.py`
  — the shared tool registry and the hybrid lexical+vector retrieval engine.
- `backend/app/features/chat/agent.py` — the base `ChatAgent` ReAct loop
  (call dedup, once-only tools, turn cap).
- `backend/app/llm/client.py` — `LLMClient`, the sync/blocking model-tiering
  client (big/small/vision/smart roles).
- `backend/app/llm/parallel.py` — thread-pool parallel map for independent
  batch work.
- `backend/app/features/_shared/background.py` + `backend/app/job_handlers.py`
  — the separate-process job runner with heartbeats and cooperative
  cancellation.
- README.md §5C (model roles), §5L/§6M (Fallspel), §6L (Utforska), §6 (case
  backbone), §5B (background jobs).
