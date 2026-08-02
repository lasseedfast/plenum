# Reusable Source System Report (Design-Focused, Code-Verified)

This report describes the source-grounded answer system as a transferable design pattern.

The emphasis is on product behavior and architecture, not implementation names.
The goal is to help you recreate the same trust properties in a different app with different source material.

---

## 1. Core Idea

The system is built around one central contract:

1. The assistant may inspect many candidate sources while reasoning.
2. The final answer must only expose sources that are actually cited in the final text.
3. Every visible citation must resolve to a real, inspectable source.

This creates a high-trust user experience: claims are traceable, source lists are not noisy, and references are navigable.

---

## 2. Design Principles That Make It Reliable

### 2.1 Source-first, not prompt-first

Reliability does not depend only on prompt instructions. It is enforced in post-processing:

- citations are parsed,
- unknown citation IDs are removed,
- source numbering is normalized,
- source section is generated deterministically by the backend.

This prevents model drift from breaking citation quality.

### 2.2 Separate reasoning state from display state

The model can keep a large internal working set of potential evidence, while the user sees a compact, validated subset.

- Internal state: all discovered source candidates.
- Display state: only cited, validated sources.

This separation is essential for both quality and UX clarity.

### 2.3 Stable source identity across all tools

Every retrieval path emits canonical source IDs. Different retrieval methods can then converge into the same source registry.

Result: no duplicate references, clean deduplication, and consistent citation behavior.

### 2.4 Deterministic citation rendering

Citations in the answer body are transformed into numbered references and linked in the UI. The source list is produced server-side from the validated citation set.

Result: answer text and source panel cannot silently diverge.

### 2.5 Bounded context with recall-on-demand

Long tool outputs are compacted for context efficiency, but full grounding text is retained in a dedicated source registry.

Result: token budget stays manageable without losing auditability.

---

## 3. Portable Architecture

1. Ingestion layer
- Parse source material.
- Normalize metadata.
- Chunk long content.
- Generate embeddings.
- Store document records and chunk records.

2. Retrieval layer
- Lexical retrieval for exact matches.
- Semantic retrieval for conceptual matches.
- Structured query path for aggregates and counts.
- Optional collection-level retrieval for grouped sources.

3. Orchestration layer
- Multi-step tool loop with an iteration cap.
- Candidate-source collection during each retrieval step.
- Output compaction for large intermediate results.

4. Provenance layer
- Central source registry per chat session.
- Deduplicate by canonical source ID.
- Preserve best snippet and best available metadata.
- Keep enough source body to support later verification.

5. Answer finalization layer
- Parse citations from model output.
- Validate cited IDs against registry.
- Drop invalid citations.
- Renumber citations in first-appearance order.
- Generate source section from validated citations.

6. Presentation layer
- Render citation markers as clickable links.
- Link source entries to source detail pages.
- Optionally enrich trusted entity mentions with profile links.

7. Observability layer
- Behavioral event logging for model/tool failures and anomalies.
- Deduplicated error logging for operational incidents.
- Counters for dropped/invalid citations.

---

## 4. Reliability Mechanisms to Reuse

### 4.1 Citation integrity checks

- Accept only citations that exist in the current source registry.
- Remove malformed or unknown citation tags.
- Normalize output to a single citation format.

### 4.2 Anti-hallucination validation

- Any identifier used in intermediate UI artifacts must be validated against known retrieved IDs.
- Unknown IDs are stripped and logged.

### 4.3 Loop safety

- Hard cap on tool iterations.
- Detect repeated calls with identical arguments.
- Detect empty-result retries.
- Capture malformed tool arguments.

### 4.4 Query safety and performance discipline

- Encourage indexed search patterns for content queries.
- Prevent slow or misleading free-text scan patterns for large text fields.

### 4.5 Concurrency-safe intermediate updates

- If you surface background “insights” while searching, serialize updates and deduplicate semantically similar messages.

---

## 5. Information Model (Generic)

Minimum entities:

- Document: canonical source unit.
- Segment: chunked part of a document with embedding.
- Entity (optional): person/product/team/etc. for link enrichment.
- Collection (optional): logical grouping of documents.

Minimum metadata for source trust:

- stable source ID,
- display title/heading,
- author or speaker,
- date/time,
- short snippet,
- optional deep link URL,
- optional entity ID.

---

## 6. End-to-End Answer Lifecycle (Reusable)

1. User asks a question.
2. Orchestrator runs retrieval steps (lexical, semantic, structured, or collection-level).
3. Each retrieval result registers source candidates in session provenance.
4. Model writes final answer with source tags.
5. Backend validates and renumbers citations.
6. Backend constructs final source list from validated citations only.
7. Frontend renders answer and citation links.

Key outcome: retrieval breadth remains high, but visible evidence remains precise.

---

## 7. Optional but High-Value UX Pattern

Parallel “communicator” behavior can publish short intermediate findings while the main answer is still being built.

Design constraints for this pattern:

- Use a separate lightweight decision path.
- Allow only one output action: share a short insight card.
- Validate any IDs included in insight payloads.
- Deduplicate aggressively to avoid spam.

This improves perceived speed and user confidence during longer runs.

---

## 8. Migration Plan for a Different App

### Phase A: Source foundation

1. Define canonical source IDs and metadata model.
2. Build ingestion and segmentation pipeline.
3. Build lexical and semantic retrieval.

### Phase B: Citation backbone

1. Add per-session provenance registry.
2. Register source candidates from every retrieval path.
3. Add citation validation + renumbering + source-list generation.

### Phase C: UX and trust surface

1. Render citations as links in answer text.
2. Render validated source list.
3. Add optional entity linking.
4. Add optional intermediate insight cards.

### Phase D: hardening

1. Add behavioral event logs.
2. Add deduplicated operational error logs.
3. Add tests for citation integrity and source-list alignment.

---

## 9. Acceptance Criteria

A production-ready implementation should satisfy all of these:

1. Every visible citation resolves to a valid source.
2. No uncited source is shown in the final source list.
3. Citation numbering is stable and ordered by first mention.
4. Invalid citation IDs are removed and logged.
5. Duplicate references to the same source collapse cleanly.
6. Source links navigate to the correct detail context.
7. Loop anomalies and malformed tool inputs are observable.

---

## 10. Common Failure Modes

1. Showing retrieval dumps instead of cited evidence.
2. Letting source order drift from citation order.
3. Losing source identity when results come from mixed retrieval methods.
4. Not validating IDs before rendering UI cards.
5. Treating prompt instructions as sufficient without backend enforcement.

---

## 11. Practical Recommendation

If you implement only one thing first, implement this:

Only return sources that are explicitly cited in the final answer, and validate every citation against a session-level provenance registry.

That single rule provides the biggest trust gain and gives you a stable base for all later improvements.

