"""
LLM tool implementations for the Riksdagen chat service.

Surface exposed to the orchestrator LLM:
  - arango_search         → PostgreSQL full-text + metadata filters (SearchService)
  - vector_search         → unified chunk + summary semantic search, merged by talk_id
  - vector_search_debates → debate-level discovery (navigation, not citable)
  - fetch_debate          → drill into one debate, return its talks with summaries
  - fetch_documents       → full-text retrieval by id list
  - read_documents_for    → focused sub-agent read: full texts in, short answer out
  - database_query        → direct SQL for aggregations
  - share_insight         → side-channel to surface findings to the user mid-loop
  - search_motions        → full-text + metadata search over motioner (MotionSearchService)
  - vector_search_motions → semantic chunk search over motioner
  - fetch_motion          → one motion: metadata, authors, yrkanden + outcomes, text
"""

import json
import os
import re
import time  # Add this import for timing
from contextvars import ContextVar
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import psycopg2.extras
from packages.colorprinter import *
from pgvector.psycopg2 import register_vector
from pydantic import BaseModel, Field

from packages.llm import LLM, get_tools, register_tool
from backend.services.search import MotionSearchService, SearchService
from postgres_client import pg
from prompts_loader import load_prompt


# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────

class HitDocument(BaseModel):
    """Normalized representation of a search hit across tools."""

    id: Optional[str] = Field(default=None, description="Document id (e.g. 'talks/H40911')")
    key: Optional[str] = Field(default=None, description="Document key without collection prefix.")
    speaker: Optional[str] = Field(default=None)
    party: Optional[str] = Field(default=None)
    date: Optional[str] = Field(default=None)
    snippet: Optional[str] = Field(default=None)
    text: Optional[str] = Field(default=None)
    score: Optional[float] = Field(default=None)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def to_string(self, include_metadata: bool = True) -> str:
        data: Dict[str, Any] = self.model_dump(exclude_none=True)
        metadata: Dict[str, Any] = data.pop("metadata", {})
        segments: List[str] = []
        # Prepend source tag for citation tracking
        bare_id = self.key or (self.id.split("/", 1)[1] if self.id and "/" in self.id else self.id)
        if bare_id:
            segments.append(f"[src:{bare_id}]")
        for field_name, field_value in data.items():
            segments.append(f"{field_name.upper()}\n{field_value}")
        for meta_key, meta_value in metadata.items():
            segments.append(f"{meta_key.upper()}\n{meta_value}")
        return "\n\n".join(segments)


def _format_src_tag(
    bare_id: str,
    speaker: Optional[str] = None,
    party: Optional[str] = None,
    date: Optional[str] = None,
) -> str:
    """Return an enriched [src:...] tag that carries speaker/party/date inline.

    The orchestrator uses bare [src:ID] tags for citation tracking; this richer
    variant is injected after summarisation so the orchestrator still sees the
    attribution metadata even if the fast model stripped the original tags.
    """
    parts = [bare_id]
    if speaker:
        parts.append(speaker)
    if party:
        parts.append(party)
    if date:
        parts.append(str(date))
    return f"[src:{' '.join(parts)}]"


class HitsResponse(BaseModel):
    hits: List[HitDocument] = Field(default_factory=list)

    def to_string(self, include_metadata: bool = True) -> str:
        if not self.hits:
            return ""
        return "\n\n---\n\n".join(
            hit.to_string(include_metadata=include_metadata) for hit in self.hits
        )


class SearchHitsResult(BaseModel):
    """Returned by arango_search. Wraps HitsResponse with search metadata."""
    type: str = "hits"
    response: HitsResponse
    stats: Dict[str, Any] = Field(default_factory=dict)
    focus_ids: List[str] = Field(default_factory=list)
    limit_reached: bool = False


# Side-channel for passing structured hit results out of tool functions.
# The @register_tool() wrapper JSON-serialises the return value, so tools that
# want to hand structured data to ChatService store it here and return a plain
# string to the framework.  ChatService reads this var immediately after the call.
_tool_structured_result: ContextVar[Optional[Any]] = ContextVar(
    "_tool_structured_result", default=None
)

# Callback for share_insight to publish SSE events directly without returning
# a value. Set by the shadow communicator thread before calling share_insight.
# Using a ContextVar means each thread has its own copy, so threads don't
# interfere with each other.
_insight_callback: ContextVar[Optional[Any]] = ContextVar(
    "_insight_callback", default=None
)

# Active provenance registry for the current chat turn. Set by ChatService
# before tool execution so tools (and `lookup_source` in particular) can read
# back grounding text by source ID without going through the message history.
_provenance_registry: ContextVar[Optional[Any]] = ContextVar(
    "_provenance_registry", default=None
)

# Fast LLM for the current request. Set by ChatService next to the provenance
# registry so the reader sub-agent (`read_documents_for`) honours per-request
# provider overrides without the user's key ever being stored module-side.
_fast_llm_var: ContextVar[Optional[Any]] = ContextVar(
    "_fast_llm_var", default=None
)


# ─────────────────────────────────────────────────────────────────────────────
# database_query  (direct SQL — no more SQL→AQL translation)
# ─────────────────────────────────────────────────────────────────────────────

@register_tool()
def database_query(sql: str) -> str:
    """Execute a SQL query against the Riksdag speeches database (PostgreSQL).

    Args:
        sql: A PostgreSQL SELECT query string.
    
    Returns:
        Query result formatted as a string (raw result or error message).
    
    Use this tool for structured queries on metadata: party breakdowns, aggregations,
    speaker statistics, and comparisons. Not for fuzzy/semantic search (use vector_search
    or arango_search instead).
    
    ✅ WHEN TO USE:
    - Counting or ranking: "how many speeches per party?", "top 10 speakers by year?"
    - Aggregations: votes per party, speeches over time periods, joins with demographics
    - Full-text aggregations: "how many speeches per party mentioned AI?" (using FTS)
    
    ❌ WHEN NOT TO USE:
    - Semantic/conceptual search → use vector_search
    - Exact phrase or keyword search → use arango_search
    - Fetching full documents → use fetch_documents
    
    DATABASE SCHEMA:
    
    talks table:
      id (TEXT)              - Speech ID (e.g. 'H40911-1')
      talare (TEXT)          - Speaker name
      parti (TEXT)           - Party code (S, M, V, KD, C, MP, SD, L, FP)
      year (INT)             - Year of speech
      datum (DATE)           - Date of speech (cast to text: datum::text)
      intressent_id (TEXT)   - Speaker ID (join key to people table)
      kammaraktivitet (TEXT) - Debate type/chamber activity
      anforandetext (TEXT)   - Full speech text (use search_vector for searching)
      summary (TEXT)         - Speech summary
      anforande_nummer (INT) - Speech number within debate
      debate (TEXT)          - Debate name
      replik (TEXT)          - Reply indicator
      tags (TEXT[])          - Tagged topics
      rel_dok_id (TEXT)      - Related document ID
      titel (TEXT)           - Speech title
    
    people table:
      intressent_id (TEXT)   - Speaker ID (join key to talks)
      namn (TEXT)            - Canonical speaker name
      parti (TEXT)           - Party affiliation
      fodd_ar (INT)          - Birth year
      kon (TEXT)             - Gender
      aktiv (BOOL)           - Active status
      valkrets (TEXT)        - Electoral district

    debates table:
      debate (TEXT, PK)      - Debate id of form "{YYYY-MM-DD}:{n}", matches talks.debate
      datum (DATE)           - Debate date (cast to text: datum::text)
      summary (TEXT)         - LLM-generated debate summary
      num_talks (INT)        - Number of talks in the debate
      talk_ids (TEXT[])      - Array of talk ids in the debate
      Note: some rows have NULL summary/summary_embedding (still backfilling).

    motions table (motioner — written proposals from MPs):
      dok_id (TEXT, PK)      - Motion id (e.g. 'HD02846')
      rm (TEXT)              - Riksmöte (e.g. '2022/23')
      year (INT)             - Riksmöte start year
      datum (DATE)           - Submission date (cast to text: datum::text)
      titel (TEXT)           - Motion title
      subtyp (TEXT)          - e.g. 'Enskild motion', 'Kommittémotion', 'Partimotion'
      organ (TEXT)           - Committee it was referred to (e.g. 'AU', 'UU')
      status (TEXT)          - e.g. 'Klar', 'Inkommen'
      parties (TEXT[])       - Party codes of all authors (use && for overlap: parties && ARRAY['S'])
      author_names (TEXT[])  - Author names in signing order
      num_yrkanden (INT)     - Number of proposals in the motion
      text (TEXT)            - Full motion text (use search_vector for searching)
      search_vector          - FTS index over titel + yrkanden + text: search_vector @@ websearch_to_tsquery('swedish', ...)

    motion_authors table (one row per signatory):
      dok_id (TEXT)          - Join key to motions
      intressent_id (TEXT)   - Join key to people (may be NULL for pre-2000 motions)
      namn (TEXT)            - Author name
      partibet (TEXT)        - Party code
      ordinal (INT)          - Signing order (0 = first author)

    motion_yrkanden table (one row per formal proposal/yrkande — condensed & precise):
      id (TEXT, PK)          - "{dok_id}:{ordinal}"
      dok_id (TEXT)          - Join key to motions
      nummer (TEXT)          - Proposal number as stated in the motion
      lydelse (TEXT)         - The proposal text itself (short, to the point)
      utskottet (TEXT)       - Committee proposal (e.g. 'Avslag')
      kammaren (TEXT)        - Chamber decision (e.g. 'Avslag'/'Bifall')
      behandlas_i (TEXT)     - Committee report where handled

    CRITICAL NOTES:
    - Use ONLY the column names listed above (do NOT invent columns)
    - For full-text search, use: search_vector @@ websearch_to_tsquery('swedish', 'query')
      This uses the GIN index (fast); do NOT use LIKE/ILIKE on anforandetext (slow + wrong results)
    - websearch_to_tsquery supports: plain words, "quoted phrases", OR, - (exclude), Swedish stemming
    - NEVER put search_vector @@ tsquery in a SELECT or SUM/CASE — it runs per-row without
      the index and causes 30-60 s queries. If you need two FTS counts, use two CTEs with WHERE:
        WITH a AS (SELECT id, parti FROM talks WHERE search_vector @@ tsquery('q1')),
             b AS (SELECT id FROM talks WHERE search_vector @@ tsquery('q2'))
        SELECT a.parti, COUNT(*) total, COUNT(b.id) matches FROM a LEFT JOIN b USING(id) GROUP BY 1
    - Join talks and people: talks.intressent_id = people.intressent_id
    - Include `intressent_id` in SELECT when querying talks to link back to speakers
    
    EXAMPLES:
    
      # Count speeches per party
      SELECT parti, COUNT(*) AS cnt FROM talks GROUP BY parti ORDER BY cnt DESC
      
      # Top 10 speakers in a party
      SELECT talare, COUNT(*) AS cnt FROM talks WHERE parti = 'M'
        GROUP BY talare ORDER BY cnt DESC LIMIT 10
      
      # Speeches per year for a party, with speaker birth year
      SELECT t.year, p.fodd_ar, COUNT(*) AS cnt
        FROM talks t JOIN people p ON t.intressent_id = p.intressent_id
        WHERE t.parti = 'S' AND t.year >= 2015
        GROUP BY t.year, p.fodd_ar ORDER BY t.year
      
      # Count speeches mentioning a topic per party (using FTS with GIN index)
      SELECT parti, COUNT(*) AS cnt FROM talks
        WHERE search_vector @@ websearch_to_tsquery('swedish', 'artificiell intelligens OR AI')
        GROUP BY parti ORDER BY cnt DESC
      
      # Count speeches about climate per year (FTS)
      SELECT year, COUNT(*) AS cnt FROM talks
        WHERE search_vector @@ websearch_to_tsquery('swedish', 'klimat')
        GROUP BY year ORDER BY year

      # Count motions about nuclear power per party (any co-author's party counts)
      SELECT unnest(parties) AS parti, COUNT(*) AS cnt FROM motions
        WHERE search_vector @@ websearch_to_tsquery('swedish', 'kärnkraft')
        GROUP BY parti ORDER BY cnt DESC

      # Most active motion authors in a year
      SELECT a.namn, a.partibet, COUNT(*) AS cnt
        FROM motion_authors a JOIN motions m ON a.dok_id = m.dok_id
        WHERE m.year = 2023 AND a.ordinal = 0
        GROUP BY a.namn, a.partibet ORDER BY cnt DESC LIMIT 10

    To surface results to the user as a stats card, call share_insight(sql="...", message="...")
    and pass the same SQL query; the backend re-executes it automatically.

    """
    print_blue(f"[database_query] SQL:\n{sql}")

    # Start timing the query execution
    start_time = time.time()

    import re as _re

    # Guard: rewrite `anforandetext @@` → `search_vector @@`.
    # The GIN index is on the stored tsvector column `search_vector`, not on `anforandetext`.
    # Using `anforandetext @@ tsquery` triggers an implicit on-the-fly to_tsvector conversion
    # with the default (not Swedish) text-search config → full table scan, 78+ seconds, empty results.
    _rewritten = _re.sub(
        r'\banforandetext\s*@@', 'search_vector @@', sql, flags=_re.IGNORECASE
    )
    if _rewritten != sql:
        print_yellow(f"[database_query] Rewrote anforandetext @@ → search_vector @@ (uses GIN index)")
        sql = _rewritten

    # Guard: reject LIKE/ILIKE on full-text columns — these bypass the FTS index,
    # cause slow sequential scans, and produce wrong results (e.g. 'ai' matches
    # 'Thai', 'kai', 'Ukraine').  The correct operator is @@ with websearch_to_tsquery.
    _text_cols = r"(anforandetext|summary)"
    if _re.search(rf"\b{_text_cols}\b.*?\bI?LIKE\b", sql, _re.IGNORECASE | _re.DOTALL) or \
       _re.search(rf"\bI?LIKE\b.*?\b{_text_cols}\b", sql, _re.IGNORECASE | _re.DOTALL):
        msg = (
            "TOOL USAGE ERROR: Do not use LIKE or ILIKE on 'anforandetext' or 'summary' — "
            "it is slow and produces wrong results. "
            "To search speech content, use the FTS operator instead:\n"
            "  WHERE search_vector @@ websearch_to_tsquery('swedish', 'your query here')\n"
            "This uses the GIN index and supports AND, OR, phrase search, and Swedish stemming. "
            "Example for counting speeches about AI per party:\n"
            "  SELECT parti, COUNT(*) AS cnt FROM talks\n"
            "  WHERE search_vector @@ websearch_to_tsquery('swedish', 'artificiell intelligens OR AI')\n"
            "  GROUP BY parti ORDER BY cnt DESC"
        )
        print_red(f"[database_query] Blocked LIKE on text column: {sql[:120]}")
        return msg

    try:
        rows = pg.execute(sql)
    except Exception as e:
        print_red(f"[database_query] Error: {e}")
        return f"ERROR executing SQL: {e}"

    # Calculate elapsed time and print it
    elapsed_time = time.time() - start_time
    print_yellow(f"[database_query] Query executed in {elapsed_time:.2f} seconds")

    ROW_CAP = 50
    truncated_note = ""
    if isinstance(rows, list) and len(rows) > ROW_CAP:
        truncated_note = f"\n[Result truncated: showing {ROW_CAP} of {len(rows)} rows. Refine your query if you need a different subset.]"
        rows = rows[:ROW_CAP]
    elif isinstance(rows, list) and len(rows) == 1:
        rows = rows[0]

    # Enrich with intressent_id when rows have talare but no intressent_id.
    # This lets the shadow communicator attach speaker portraits to stats insights,
    # and gives the main LLM the IDs for future arango_search(intressent_ids=...) calls.
    rows_list = rows if isinstance(rows, list) else ([rows] if isinstance(rows, dict) else [])
    if (
        rows_list
        and isinstance(rows_list[0], dict)
        and "talare" in rows_list[0]
        and "intressent_id" not in rows_list[0]
    ):
        names = list({r["talare"] for r in rows_list if isinstance(r, dict) and r.get("talare")})
        try:
            person_rows_extra = pg.execute(
                "SELECT intressent_id, namn FROM people WHERE namn = ANY(%s)",
                (names,),
            )
            name_to_iid = {r["namn"]: r["intressent_id"] for r in person_rows_extra}
            if name_to_iid:
                if isinstance(rows, list):
                    rows = [
                        {**r, "intressent_id": name_to_iid[r["talare"]]}
                        if isinstance(r, dict) and r.get("talare") in name_to_iid
                        else r
                        for r in rows
                    ]
                elif isinstance(rows, dict) and rows.get("talare") in name_to_iid:
                    rows = {**rows, "intressent_id": name_to_iid[rows["talare"]]}
                rows_list = rows if isinstance(rows, list) else [rows]
                print_yellow(f"[database_query] Enriched {len(name_to_iid)} rows with intressent_id")
        except Exception as e:
            print_yellow(f"[database_query] intressent_id enrichment failed: {e}")

    # Store rows for ChatService to populate collected_persons before shadow fires.
    _tool_structured_result.set({"type": "db_rows", "rows": rows_list})

    print_blue(f"[database_query] ---\n{sql}\n---")
    result_str = f"SQL result: {rows}{truncated_note}"
    print_blue(f"[database_query] Returning:\n{result_str[:200]}")
    return result_str


# ─────────────────────────────────────────────────────────────────────────────
# vector_search  (unified: chunks + summaries, merged by talk_id)
# ─────────────────────────────────────────────────────────────────────────────

@register_tool()
def vector_search(query: str, limit: int = 10) -> HitsResponse:
    """
    Semantic/conceptual search over Riksdag speeches. Blends two sources of signal
    under the hood so the caller does not have to choose:

      - chunk embeddings   → granular, quote-ready passages
      - summary embeddings → thematic, whole-speech gist

    Results are merged by talk_id; when a talk is strong in both indexes it is
    returned once with the chunk passage as the snippet and the summary attached
    in metadata. Each hit is tagged with metadata["source_type"] ∈ {"chunk",
    "summary", "both"} so you can tell which signal fired.

    Use this tool when:
    - The user asks a thematic or conceptual question and exact keywords may not appear.
    - You want speeches similar in meaning to a phrase or idea.
    - You want a blended view of both whole-talk overview and specific passages.

    When NOT to use:
    - Exact word/phrase matching → use arango_search
    - Counts, aggregations, statistics → use database_query
    - You already know the speaker/party/year filter → use arango_search with filters

    Args:
        query: Natural-language description of the topic.
        limit: Number of merged hits to return (default 10).

    Returns:
        HitsResponse with the top-limit talks scored by max(chunk, summary).
    """
    print_yellow(f"[Tools] vector_search → query='{query}' (top_k={limit})")

    query_vec = pg.make_embeddings([query])[0]
    fetch_each = limit * 2  # oversample each index so the merge has room to dedupe

    chunk_rows = pg.execute(
        """
        SELECT id, talk_id, chunk_index, text,
               1 - (embedding <=> %s::vector) AS score
        FROM chunks
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """,
        (query_vec, query_vec, fetch_each),
    )

    summary_rows = pg.execute(
        """
        SELECT id, summary,
               1 - (summary_embedding <=> %s::vector) AS score
        FROM talks
        WHERE summary_embedding IS NOT NULL
        ORDER BY summary_embedding <=> %s::vector
        LIMIT %s
        """,
        (query_vec, query_vec, fetch_each),
    )

    if not chunk_rows and not summary_rows:
        return ""

    # Merge by talk_id. Per-talk keep the best chunk hit and the summary hit.
    merged: Dict[str, Dict[str, Any]] = {}
    for row in chunk_rows:
        talk_id = row["talk_id"]
        slot = merged.setdefault(talk_id, {})
        if row["score"] > slot.get("chunk_score", -1):
            slot["chunk_score"] = row["score"]
            slot["chunk_index"] = row["chunk_index"]
            slot["chunk_text"] = row["text"]
    for row in summary_rows:
        talk_id = row["id"]
        slot = merged.setdefault(talk_id, {})
        slot["summary_score"] = row["score"]
        slot["summary_text"] = row["summary"]

    # Score per talk = max of the two signals (treat missing as 0).
    for data in merged.values():
        data["score"] = max(data.get("chunk_score") or 0, data.get("summary_score") or 0)

    top_ids = sorted(merged.keys(), key=lambda tid: merged[tid]["score"], reverse=True)[:limit]
    if not top_ids:
        return ""

    talk_rows = pg.execute(
        """
        SELECT id, talare, parti, datum::text AS datum, intressent_id, titel, debateurl
        FROM talks
        WHERE id = ANY(%s::text[])
        """,
        (top_ids,),
    )
    talk_map = {row["id"]: row for row in talk_rows}

    hits: List[HitDocument] = []
    for talk_id in top_ids:
        data = merged[talk_id]
        parent = talk_map.get(talk_id, {})
        has_chunk = "chunk_text" in data
        has_summary = "summary_text" in data
        source_type = (
            "both" if has_chunk and has_summary else ("chunk" if has_chunk else "summary")
        )

        if has_chunk:
            # Neighbor chunks give the LLM a bit of context around the hit.
            neighbor_rows = pg.execute(
                """
                SELECT text, chunk_index
                FROM chunks
                WHERE talk_id = %s AND chunk_index IN (%s, %s, %s)
                ORDER BY chunk_index
                """,
                (talk_id, data["chunk_index"] - 1, data["chunk_index"], data["chunk_index"] + 1),
            )
            snippet = " ".join(r["text"] for r in neighbor_rows)
        else:
            snippet = (data.get("summary_text") or "")[:800]

        metadata: Dict[str, Any] = {
            "talk_id": talk_id,
            "source_type": source_type,
            "intressent_id": parent.get("intressent_id"),
            "titel": parent.get("titel"),
            "debateurl": parent.get("debateurl"),
        }
        if has_chunk:
            metadata["chunk_index"] = data["chunk_index"]
        if has_summary and has_chunk:
            # Only attach summary separately when the snippet is the chunk text.
            metadata["summary"] = (data["summary_text"] or "")[:500]

        hits.append(
            HitDocument(
                id=f"talks/{talk_id}",
                key=talk_id,
                speaker=parent.get("talare"),
                party=parent.get("parti"),
                date=str(parent.get("datum") or ""),
                snippet=snippet,
                score=data["score"],
                metadata=metadata,
            )
        )

    result = HitsResponse(hits=hits)
    _tool_structured_result.set(result)
    return result.to_string() or "(no results)"


# ─────────────────────────────────────────────────────────────────────────────
# vector_search_debates  (discovery — not citable)
# ─────────────────────────────────────────────────────────────────────────────

@register_tool()
def vector_search_debates(query: str, limit: int = 5) -> HitsResponse:
    """
    Semantic discovery tool that finds relevant parliamentary debates (whole
    sessions, not individual speeches) by their LLM-written summaries.

    This is a NAVIGATION tool. Use it to locate interesting debates, then call
    `fetch_debate(debate_id)` to see the talks inside. Do NOT cite a debate
    directly — cite the individual talks you read via `fetch_debate`.

    Workflow:
        vector_search_debates("klimatmål 2045")
        → returns ~5 debates with ids like "2021-06-17:42"
        → pick the most relevant → fetch_debate("2021-06-17:42")
        → read talk summaries → cite with [src:TALK_ID] in your answer

    Use this tool when:
    - The user asks a broad thematic question that likely spans a whole session.
    - You want a quick map of which debates touched a topic before drilling in.

    When NOT to use:
    - You want individual speech hits → use vector_search or arango_search
    - You already have a debate_id → call fetch_debate directly
    - Counts/aggregations → use database_query

    Args:
        query: Natural-language description of the topic.
        limit: Number of debates to return (default 5).

    Returns:
        HitsResponse. Each hit uses the bare debate id (e.g. "2021-06-17:42");
        `snippet` is the debate summary, metadata includes `num_talks` and `datum`.
    """
    print_yellow(f"[Tools] vector_search_debates → query='{query}' (top_k={limit})")

    query_vec = pg.make_embeddings([query])[0]

    rows = pg.execute(
        """
        SELECT d.debate, d.datum::text AS datum, d.summary, d.num_talks,
               1 - (d.summary_embedding <=> %s::vector) AS score
        FROM debates d
        WHERE d.summary_embedding IS NOT NULL
        ORDER BY d.summary_embedding <=> %s::vector
        LIMIT %s
        """,
        (query_vec, query_vec, limit),
    )

    if not rows:
        return ""

    # Bare debate id (no "debates/" prefix) so the chat service's provenance
    # guard can identify and skip these — debates are not citable on their own.
    hits: List[HitDocument] = [
        HitDocument(
            id=row["debate"],
            key=row["debate"],
            speaker=None,
            party=None,
            date=str(row.get("datum") or ""),
            snippet=(row.get("summary") or "")[:400],
            score=row.get("score"),
            metadata={
                "kind": "debate",
                "num_talks": row.get("num_talks"),
                "datum": str(row.get("datum") or ""),
            },
        )
        for row in rows
    ]

    result = HitsResponse(hits=hits)
    _tool_structured_result.set(result)
    return result.to_string() or "(no results)"


# ─────────────────────────────────────────────────────────────────────────────
# fetch_debate  (drill down from a debate id to the talks inside)
# ─────────────────────────────────────────────────────────────────────────────

# Combined character budget for talk summaries in the response. If the full set
# of summaries exceeds this, we either rank by relevance to `query` (if given)
# or fall back to the oldest-first subset whose summaries fit.
FETCH_DEBATE_SUMMARY_BUDGET_CHARS = 7000


@register_tool()
def fetch_debate(debate_id: str, query: Optional[str] = None) -> dict:
    """
    Look up a single debate by its id and return a list of its talks with
    per-talk summaries. Registers each returned talk as a citable source.

    Typical flow: call `vector_search_debates(query)` to discover relevant
    debate ids, then call `fetch_debate(debate_id, query=query)` on the best
    match. Passing the same `query` lets the tool rank talks by semantic
    relevance when the debate is too long to return in full.

    Args:
        debate_id: Debate id of the form "{YYYY-MM-DD}:{n}" (e.g. "2021-06-17:42").
        query: Optional search query. When the debate's combined talk summaries
            exceed the response budget, talks are ranked by embedding distance
            to this query and only the most relevant ones are returned
            (presented in chronological order). Strongly recommended for long
            debates — without it, a truncated chronological slice is returned.

    Returns:
        dict with keys:
          debate_id, datum, summary, num_talks,
          talks: [{id, talare, parti, intressent_id, summary}, ...],
          note (optional): present when not all talks are returned; explains
            how many were omitted and on what basis.
    """
    print_yellow(
        f"[Tools] fetch_debate → debate_id='{debate_id}'"
        + (f" query='{query}'" if query else "")
    )

    debate_rows = pg.execute(
        """
        SELECT debate, datum::text AS datum, summary, num_talks, talk_ids
        FROM debates
        WHERE debate = %s
        """,
        (debate_id,),
    )
    if not debate_rows:
        return {"error": f"No debate found with id '{debate_id}'."}
    debate = debate_rows[0]

    talk_ids: List[str] = list(debate.get("talk_ids") or [])

    talk_rows = pg.execute(
        """
        SELECT id, anforande_nummer, talare, parti, intressent_id, summary,
               datum::text AS datum, titel, debateurl
        FROM talks
        WHERE id = ANY(%s::text[])
        ORDER BY anforande_nummer ASC
        """,
        (talk_ids,),
    )

    total_summary_chars = sum(len(r.get("summary") or "") for r in talk_rows)
    trimmed_rows = talk_rows
    note: Optional[str] = None

    if total_summary_chars > FETCH_DEBATE_SUMMARY_BUDGET_CHARS:
        ranking_method = "chronological"
        chosen_ids: set = set()
        running = 0

        if query:
            # Rank talks in this debate by embedding distance to the query,
            # then keep the top-K whose summaries fit the budget.
            embedding = pg.make_embeddings([query])[0]
            ranked = pg.execute(
                """
                SELECT id, (summary_embedding <=> %s::vector) AS distance
                FROM talks
                WHERE id = ANY(%s::text[])
                  AND summary_embedding IS NOT NULL
                ORDER BY distance ASC
                """,
                (embedding, talk_ids),
            )
            row_by_id = {r["id"]: r for r in talk_rows}
            for r in ranked:
                tid = r["id"]
                src = row_by_id.get(tid)
                if not src:
                    continue
                summary_len = len(src.get("summary") or "")
                if running + summary_len > FETCH_DEBATE_SUMMARY_BUDGET_CHARS and chosen_ids:
                    break
                chosen_ids.add(tid)
                running += summary_len
            if chosen_ids:
                ranking_method = "relevance"

        if not chosen_ids:
            # Either no query was given, or no talks in this debate have
            # summary_embedding populated yet. Fall back to chronological.
            running = 0
            for r in talk_rows:
                summary_len = len(r.get("summary") or "")
                if not summary_len:
                    continue
                if running + summary_len > FETCH_DEBATE_SUMMARY_BUDGET_CHARS and chosen_ids:
                    break
                chosen_ids.add(r["id"])
                running += summary_len

        trimmed_rows = [r for r in talk_rows if r["id"] in chosen_ids]
        omitted = len(talk_rows) - len(trimmed_rows)

        if ranking_method == "relevance":
            note = (
                f"Debate has {len(talk_rows)} talks (combined summaries "
                f"{total_summary_chars} chars). Returned the {len(trimmed_rows)} "
                f"most relevant to query '{query}'; {omitted} talks omitted. "
                f"Use fetch_documents with specific ids for full texts."
            )
        else:
            reason = (
                "no summary embeddings available for this debate yet; ranked chronologically"
                if query
                else "ranked chronologically — pass `query` for relevance ranking"
            )
            note = (
                f"Debate has {len(talk_rows)} talks (combined summaries "
                f"{total_summary_chars} chars). Returned the first "
                f"{len(trimmed_rows)} summarised talks ({reason}); "
                f"{omitted} talks omitted. Use fetch_documents for full texts."
            )

    # Build a compact dict for the LLM; register the talks as provenance sources.
    talks_out: List[Dict[str, Any]] = []
    hits: List[HitDocument] = []
    for row in trimmed_rows:
        talk_id = row["id"]
        summary_text = row.get("summary") or ""
        talks_out.append(
            {
                "id": talk_id,
                "talare": row.get("talare"),
                "parti": row.get("parti"),
                "intressent_id": row.get("intressent_id"),
                "summary": summary_text,
            }
        )
        hits.append(
            HitDocument(
                id=f"talks/{talk_id}",
                key=talk_id,
                speaker=row.get("talare"),
                party=row.get("parti"),
                date=str(row.get("datum") or ""),
                snippet=summary_text[:500],
                metadata={
                    "intressent_id": row.get("intressent_id"),
                    "titel": row.get("titel"),
                    "debateurl": row.get("debateurl"),
                    "debate": debate_id,
                },
            )
        )

    if hits:
        _tool_structured_result.set(HitsResponse(hits=hits))

    result: Dict[str, Any] = {
        "debate_id": debate.get("debate"),
        "datum": debate.get("datum"),
        "summary": debate.get("summary"),
        "num_talks": debate.get("num_talks") or len(talk_ids),
        "talks": talks_out,
    }
    if note:
        result["note"] = note
    return result


# ─────────────────────────────────────────────────────────────────────────────
# fetch_documents
# ─────────────────────────────────────────────────────────────────────────────

@register_tool()
def fetch_documents(_ids: list[str], collection: str = "", fields: list = []) -> list:
    """
    Fetch full documents by their id from the talks table.

    Use this tool when:
    - arango_search or vector_search returned _ids and you need the full speech text.
    - You want specific fields for a known set of documents.

    When NOT to use:
    - To search → use arango_search or vector_search
    - To count/aggregate → use database_query

    Args:
        _ids: List of document IDs (e.g. ["talks/H40911", "talks/H40912"] or bare keys)
        collection: Optional prefix to add to bare IDs (e.g. "talks")
        fields: Optional list of field names to return (empty = common fields)

    Returns:
        List of document dicts, or error message string.
    """
    # Normalize IDs: strip "talks/" prefix
    talk_ids = []
    for i in _ids:
        if "/" in i:
            talk_ids.append(i.split("/", 1)[1])
        elif collection:
            talk_ids.append(i)
        else:
            talk_ids.append(i)

    if not talk_ids:
        return []

    # Default fields if none specified
    if fields:
        allowed = {
            "id", "anforandetext", "anforande_nummer", "kammaraktivitet",
            "talare", "datum", "year", "parti", "intressent_id", "titel",
            "rel_dok_id", "debate", "replik", "summary", "tags",
        }
        # Cast datum to text so Python receives a string, not a date object
        def _col(f: str) -> str:
            return "datum::text AS datum" if f == "datum" else f
        select = ", ".join(_col(f) for f in fields if f in allowed or f.startswith("_"))
        if not select:
            select = "id, anforandetext, talare, parti, datum::text AS datum, year, kammaraktivitet"
    else:
        select = (
            "id, anforandetext, anforande_nummer, kammaraktivitet, "
            "talare, datum::text AS datum, year, parti, intressent_id, titel, "
            "rel_dok_id, debate, replik, summary, tags"
        )

    rows = pg.execute(
        f"SELECT {select} FROM talks WHERE id = ANY(%s::text[])",
        (talk_ids,),
    )

    # Re-add _id and _key virtual fields for downstream compatibility
    result = []
    for row in rows:
        doc = dict(row)
        talk_id = doc.get("id", "")
        doc["_id"] = f"talks/{talk_id}"
        doc["_key"] = talk_id
        result.append(doc)

    # Publish structured provenance so ChatService can track these as sources
    hits = []
    for doc in result:
        hits.append(
            HitDocument(
                id=doc.get("_id"),
                key=doc.get("_key"),
                speaker=doc.get("talare"),
                party=doc.get("parti"),
                date=doc.get("datum"),
                text=doc.get("anforandetext", ""),
                snippet=doc.get("summary") or (doc.get("anforandetext") or "")[:300],
                metadata={
                    "intressent_id": doc.get("intressent_id"),
                    "titel": doc.get("titel"),
                    "kammaraktivitet": doc.get("kammaraktivitet"),
                },
            )
        )
    if hits:
        _tool_structured_result.set(HitsResponse(hits=hits))

    return result


# ─────────────────────────────────────────────────────────────────────────────
# read_documents_for  (focused sub-agent read — full text never enters the
# orchestrator's context, only the answer to one specific question does)
# ─────────────────────────────────────────────────────────────────────────────

_READER_SYSTEM = load_prompt("tools/reader")

_READER_MAX_DOCS = 6
_READER_SINGLE_BUDGET = 30000   # chars of full text when reading one document
_READER_MULTI_BUDGET = 12000    # chars per document when reading several

# Lazy fallback reader for callers that haven't set _fast_llm_var (e.g. the
# research job runner or ad-hoc scripts). Built once from server-side env.
_default_reader_llm: Optional[LLM] = None


def _get_reader_llm():
    llm = _fast_llm_var.get()
    if llm is not None:
        return llm
    global _default_reader_llm
    if _default_reader_llm is None:
        _default_reader_llm = LLM(
            model=os.getenv("LLM_MODEL_FAST", "smart"),
            base_url=os.getenv("LLM_DIRECT_URL"),
            temperature=0.1,
        )
    return _default_reader_llm


@register_tool()
def read_documents_for(question: str, _ids: list[str]) -> str:
    """
    Read the FULL text of up to 6 speeches/documents and get a focused answer
    to ONE specific question about them.

    Use this tool when:
    - You need to know what specific documents actually SAY about something
      (positions, arguments, exact statements) — not just their metadata.
    - A snippet or summary is too sparse and you would otherwise fetch full text.

    When NOT to use:
    - To search → use arango_search or vector_search.
    - When the user explicitly asks to see the complete raw text → fetch_documents.

    Args:
        question: One concrete question in Swedish, e.g.
            "Vilka argument anför talarna mot höjd bensinskatt?"
        _ids: 1-6 document IDs from earlier search results
            (e.g. ["H40911", "talks/H40912"]). Motion ids from search_motions
            (e.g. "motions/HD02846") work too — the full motion text is read.

    Returns:
        A short Swedish answer grounded in the documents, with [src:ID] tags,
        or a message saying the documents contain nothing relevant.
    """
    if not (question or "").strip():
        return "Tom fråga — inget att besvara."
    talk_ids: list[str] = []
    for i in _ids or []:
        bare = i.split("/", 1)[1] if "/" in i else i
        bare = (bare or "").strip()
        if bare and bare not in talk_ids:
            talk_ids.append(bare)
    if not talk_ids:
        return "Inga dokument-id angivna."
    talk_ids = talk_ids[:_READER_MAX_DOCS]

    rows = pg.execute(
        "SELECT id, anforandetext, talare, parti, datum::text AS datum, titel, "
        "intressent_id, summary FROM talks WHERE id = ANY(%s::text[])",
        (talk_ids,),
    )
    by_id = {r["id"]: dict(r) for r in rows}

    # Ids not found among talks may be motions — read those too.
    missing_ids = [tid for tid in talk_ids if tid not in by_id]
    if missing_ids:
        motion_rows = pg.execute(
            "SELECT dok_id, text, titel, parties, author_names, datum::text AS datum "
            "FROM motions WHERE dok_id = ANY(%s::text[])",
            (missing_ids,),
        )
        for r in motion_rows:
            names = r.get("author_names") or []
            speaker = ", ".join(names[:3]) + (" m.fl." if len(names) > 3 else "")
            by_id[r["dok_id"]] = {
                "id": r["dok_id"],
                "anforandetext": r.get("text"),
                "talare": speaker,
                "parti": "/".join(r.get("parties") or []),
                "datum": r.get("datum"),
                "titel": r.get("titel"),
                "intressent_id": None,
                "summary": None,
                "_kind": "motion",
            }

    budget = _READER_SINGLE_BUDGET if len(talk_ids) == 1 else _READER_MULTI_BUDGET
    blocks: list[str] = []
    hits: list[HitDocument] = []
    for tid in talk_ids:
        doc = by_id.get(tid)
        if doc is None:
            blocks.append(f"== [src:{tid}] ==\n(dokumentet kunde inte laddas)")
            continue
        text = (doc.get("anforandetext") or "").strip()
        if not text:
            blocks.append(f"== [src:{tid}] ==\n(dokumentet saknar text)")
            continue
        if len(text) > budget:
            text = text[:budget] + "\n\n[...trunkerat...]"
        header_parts = [f"[src:{tid}]"]
        if doc.get("talare"):
            speaker = doc["talare"]
            if doc.get("parti"):
                speaker += f" ({doc['parti']})"
            header_parts.append(speaker)
        if doc.get("datum"):
            header_parts.append(doc["datum"])
        header = "== " + " | ".join(header_parts)
        if doc.get("titel"):
            header += f" == {doc['titel']}"
        else:
            header += " =="
        blocks.append(f"{header}\n{text}")
        collection = "motions" if doc.get("_kind") == "motion" else "talks"
        hits.append(
            HitDocument(
                id=f"{collection}/{tid}",
                key=tid,
                speaker=doc.get("talare"),
                party=doc.get("parti"),
                date=doc.get("datum"),
                text=doc.get("anforandetext", "")[:3000],
                snippet=doc.get("summary") or (doc.get("anforandetext") or "")[:300],
                metadata={
                    "intressent_id": doc.get("intressent_id"),
                    "titel": doc.get("titel"),
                },
            )
        )

    if not hits:
        return "Inga av de angivna dokumenten kunde laddas."

    user_prompt = (
        f"Fråga: {question}\n\nDokument att läsa:\n\n" + "\n\n".join(blocks)
    )
    try:
        answer = _get_reader_llm().generate(
            messages=[
                {"role": "system", "content": _READER_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            think=False,
            tools=[],
            max_tokens=1200,
        )
    except Exception as e:
        print_red(f"[read_documents_for] reader LLM failed: {e}")
        return f"ERROR: läsningen misslyckades ({e})."
    # _llm returns a plain string on API errors, a ChatCompletionMessage on success.
    answer_text = answer if isinstance(answer, str) else (answer.content or "")
    answer_text = answer_text.strip()
    if not answer_text or (isinstance(answer, str) and "error" in answer_text.lower()):
        return "ERROR: läsningen gav inget svar."

    # Publish provenance so the read documents stay citable in the registry.
    _tool_structured_result.set(HitsResponse(hits=hits))
    return answer_text


# ─────────────────────────────────────────────────────────────────────────────
# _normalize_arango_search_args  (unchanged helper)
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_arango_search_args(
    query: str,
    parties: Optional[Union[str, List[str]]] = None,
    people: Optional[Union[str, List[str]]] = None,
    debates: Optional[Union[str, List[str]]] = None,
    from_year: Optional[Union[str, int]] = None,
    to_year: Optional[Union[str, int]] = None,
    limit: Optional[Union[str, int]] = 10,
    speaker_ids: Optional[Union[str, List[str], bool]] = None,
) -> Dict[str, Any]:
    def to_list(val):
        if val is None:
            return []
        if isinstance(val, list):
            return val
        if isinstance(val, str):
            val = val.strip()
            if val.startswith("["):
                try:
                    parsed = json.loads(val)
                    if isinstance(parsed, list):
                        return [str(v).strip() for v in parsed if v]
                except (json.JSONDecodeError, ValueError):
                    pass
            if "," in val:
                return [v.strip() for v in val.split(",") if v.strip()]
            return [val.strip()]
        return [val]

    def to_int(val):
        if val is None:
            return None
        if isinstance(val, int):
            return val
        if isinstance(val, str):
            try:
                return int(val)
            except ValueError:
                return None
        return None

    return {
        "query": str(query) if query is not None else "",
        "parties": to_list(parties),
        "people": to_list(people),
        "debates": to_list(debates),
        "from_year": to_int(from_year),
        "to_year": to_int(to_year),
        "limit": to_int(limit) if limit is not None else 10,
        "speaker_ids": to_list(speaker_ids),
    }


@register_tool()
def arango_search(
    query: str,
    parties: Optional[list[str]] = None,
    people: Optional[list[str]] = None,
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
    limit: int = 20,
    return_snippets: bool = False,
    focus_ids: Optional[List[str]] = None,
    intressent_ids: Optional[Union[str, List[str], bool]] = None,
) -> "SearchHitsResult":
    """
    Perform a full-text and metadata search in the Riksdagen 'talks' table using PostgreSQL FTS.
    
    Args:
        query: The search string (supports AND, OR, NOT, phrases in quotes, år:2018-2022).
        parties: List of party codes to filter by (e.g., ["S", "M"]).
        people: List of speaker names to filter by.
        from_year: Start year for filtering.
        to_year: End year for filtering.
        limit: Maximum number of results (default 20).
        return_snippets: If True, return only snippets with highlights.
        focus_ids: Restrict search to these specific document ids.
        intressent_ids: An array of numeric strings (e.g., ['0448485371626', '0448485371627']). NEVER guess or make up an ID. If you do not know the exact numeric ID, you MUST use the people parameter instead."

    Returns:
        SearchHitsResult with hits and search metadata.

    Possible to use `return_snippets=True` to only return snippets with highlights instead of
    full documents, which can be useful to get an overview of the results. Use this if you're not sure the results are relevant and want to quickly scan them before deciding to fetch full documents.
    If searching for specific words or phrases, consider using quotes (") for phrases,
    AND/OR/NOT operators, and year ranges (e.g., år:2018-2022).
    Always use a limit to avoid too many results. Hits are ranked by relevance (ts_rank_cd).

    This tool uses advanced text search (with stemming, language analysis, and ranking) and
    can also filter by party, speaker, debate type, and year range.

    When NOT to use:
      - Fuzzy/semantic similarity → use vector_search
      - Exact aggregations, joins, or structured metadata queries → use database_query
    """
    # Validate intressent_ids: they must be purely numeric strings.
    # If the model passes a placeholder like "PERS_ID_FOR_X", reject the whole call
    # so it knows to use `people=` instead or wait until it has real IDs from results.
    if intressent_ids:
        ids_list = (
            json.loads(intressent_ids) if isinstance(intressent_ids, str) else intressent_ids
        )
        if isinstance(ids_list, list):
            bad = [sid for sid in ids_list if not str(sid).strip().isdigit()]
            if bad:
                return f"ERROR: intressent_ids must be numeric strings (e.g. '0448485371626'). Invalid values: {bad}. Use the `people` parameter to search by name, or only pass intressent_ids you have seen in previous results."

    args = _normalize_arango_search_args(
        query=query,
        parties=parties,
        people=people,
        from_year=from_year,
        to_year=to_year,
        limit=limit,
        speaker_ids=intressent_ids,
    )

    class Payload:
        def __init__(self, q, parties, people, debates, from_year, to_year, limit,
                     return_snippets=False, focus_ids=None, speaker_ids=None):
            self.q = q
            self.parties = parties or []
            self.people = people or []
            self.debates = debates or []
            self.from_year = from_year
            self.to_year = to_year
            self.limit = limit
            self.return_snippets = return_snippets
            self.focus_ids = focus_ids or []
            self.speaker_ids = speaker_ids

    focus_id_list: List[str] = []
    if focus_ids:
        if isinstance(focus_ids, list):
            focus_id_list = [str(item) for item in focus_ids if isinstance(item, (str, int))]
        elif isinstance(focus_ids, str):
            try:
                parsed = json.loads(focus_ids)
                if isinstance(parsed, list):
                    focus_id_list = [str(item) for item in parsed if isinstance(item, (str, int))]
            except json.JSONDecodeError:
                focus_id_list = [focus_ids]

    search_service = SearchService()
    # Always fetch full text (return_snippets=False) so we can measure total size
    # before deciding whether to include text or fall back to snippets.
    results, stats, limit_reached = search_service.search(
        payload=Payload(
            q=args["query"],
            parties=args["parties"],
            people=args["people"],
            debates=args.get("debates", []),
            from_year=args["from_year"],
            to_year=args["to_year"],
            limit=args["limit"],
            return_snippets=False,
            focus_ids=focus_id_list,
            speaker_ids=intressent_ids,
        ),
        include_snippets=True,
        return_snippets=False,
    )

    # Decide whether to include full text or fall back to snippets.
    # Two triggers: caller explicitly asked for snippets, or total text is too large.
    total_text_chars = sum(len(item.get("text") or "") for item in results if isinstance(item, dict))
    auto_snippet_mode = total_text_chars > 20_000
    snippet_mode = return_snippets or auto_snippet_mode

    hits: List[HitDocument] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        talk_id = item.get("_id", "")
        if snippet_mode:
            # Only include snippet fields, not full text
            hits.append(HitDocument(
                id=talk_id,
                key=talk_id.removeprefix("talks/"),
                speaker=item.get("speaker"),
                party=item.get("party"),
                date=item.get("date"),
                snippet=item.get("snippet_long") or item.get("snippet") or "",
                text=None,
                score=item.get("bm25"),
                metadata={
                    "intressent_id": item.get("intressent_id"),
                    "debateurl": item.get("url_session") or item.get("debateurl"),
                    "titel": item.get("titel"),
                    "kammaraktivitet": item.get("kammaraktivitet"),
                    "chunk_index": item.get("chunk_index", -1),
                },
            ))
        else:
            hits.append(HitDocument(
                id=talk_id,
                key=talk_id.removeprefix("talks/"),
                speaker=item.get("speaker"),
                party=item.get("party"),
                date=item.get("date"),
                snippet=item.get("snippet") or item.get("snippet_long") or "",
                text=item.get("text") or "",
                score=item.get("bm25"),
                metadata={
                    "intressent_id": item.get("intressent_id"),
                    "debateurl": item.get("url_session") or item.get("debateurl"),
                    "titel": item.get("titel"),
                    "kammaraktivitet": item.get("kammaraktivitet"),
                    "chunk_index": item.get("chunk_index", -1),
                },
            ))

    structured = SearchHitsResult(
        response=HitsResponse(hits=hits),
        stats=stats,
        focus_ids=[h.id for h in hits if h.id],
        limit_reached=limit_reached,
    )
    _tool_structured_result.set(structured)
    output = structured.response.to_string() or "(no results)"
    if auto_snippet_mode:
        note = (
            f"NOTE: The full texts of these {len(hits)} results total {total_text_chars:,} characters "
            f"(exceeds 20 000), so only snippets are shown above. "
            "You can either:\n"
            "  1. Pick specific document IDs from the results and use `focus_ids` to fetch only those, or\n"
            "  2. Repeat the search with a lower `limit` to reduce the result set."
        )
        output = output + "\n\n---\n\n" + note
    return output


# ─────────────────────────────────────────────────────────────────────────────
# Motioner (written proposals from MPs) — search, semantic search, fetch
# ─────────────────────────────────────────────────────────────────────────────

@register_tool()
def search_motions(
    query: str,
    parties: Optional[list[str]] = None,
    people: Optional[list[str]] = None,
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
    limit: int = 20,
    return_snippets: bool = False,
    focus_ids: Optional[List[str]] = None,
    intressent_ids: Optional[Union[str, List[str], bool]] = None,
) -> "SearchHitsResult":
    """
    Full-text and metadata search over MOTIONER (written proposals submitted by MPs),
    as opposed to arango_search which searches chamber SPEECHES (anföranden).

    Speeches (anföranden) are the PRIMARY source — search them first with
    arango_search/vector_search. Use this tool as a COMPLEMENT: to deepen research
    with the concrete proposals (yrkanden) behind positions found in speeches, to
    add committee/chamber outcomes, or when the user explicitly asks about motioner
    ("vad har X föreslagit/motionerat om?", "vilka motioner finns om Y?").

    Args:
        query: The search string (supports AND, OR, NOT, phrases in quotes, år:2018-2022).
        parties: List of party codes to filter by (e.g., ["S", "M"]). Matches any co-author.
        people: List of author names to filter by (matches any signatory).
        from_year: Start year (riksmöte start year) for filtering.
        to_year: End year for filtering.
        limit: Maximum number of results (default 20).
        return_snippets: If True, return only snippets with highlights.
        focus_ids: Restrict search to these specific motion ids.
        intressent_ids: An array of numeric strings (e.g., ['0448485371626']). NEVER guess
            or make up an ID. If you do not know the exact numeric ID, use `people` instead.

    Returns:
        SearchHitsResult. Hit ids look like "motions/HD02846"; metadata includes titel,
        organ (committee), rm (riksmöte) and num_yrkanden. Cite with [src:DOK_ID].

    When NOT to use:
      - Chamber speeches/debates → arango_search or vector_search
      - Fuzzy/semantic similarity over motions → vector_search_motions
      - Counts/aggregations → database_query (motions table)
    """
    if intressent_ids:
        ids_list = (
            json.loads(intressent_ids) if isinstance(intressent_ids, str) else intressent_ids
        )
        if isinstance(ids_list, list):
            bad = [sid for sid in ids_list if not str(sid).strip().isdigit()]
            if bad:
                return f"ERROR: intressent_ids must be numeric strings (e.g. '0448485371626'). Invalid values: {bad}. Use the `people` parameter to search by name, or only pass intressent_ids you have seen in previous results."

    args = _normalize_arango_search_args(
        query=query,
        parties=parties,
        people=people,
        from_year=from_year,
        to_year=to_year,
        limit=limit,
        speaker_ids=intressent_ids,
    )

    class Payload:
        def __init__(self, q, parties, people, from_year, to_year, limit,
                     focus_ids=None, speaker_ids=None):
            self.q = q
            self.parties = parties or []
            self.people = people or []
            self.from_year = from_year
            self.to_year = to_year
            self.limit = limit
            self.focus_ids = focus_ids or []
            self.speaker_ids = speaker_ids

    focus_id_list: List[str] = []
    if focus_ids:
        if isinstance(focus_ids, list):
            focus_id_list = [str(item) for item in focus_ids if isinstance(item, (str, int))]
        elif isinstance(focus_ids, str):
            try:
                parsed = json.loads(focus_ids)
                if isinstance(parsed, list):
                    focus_id_list = [str(item) for item in parsed if isinstance(item, (str, int))]
            except json.JSONDecodeError:
                focus_id_list = [focus_ids]

    print_yellow(f"[Tools] search_motions → query='{query}' limit={args['limit']}")

    search_service = MotionSearchService()
    results, stats, limit_reached = search_service.search(
        payload=Payload(
            q=args["query"],
            parties=args["parties"],
            people=args["people"],
            from_year=args["from_year"],
            to_year=args["to_year"],
            limit=args["limit"],
            focus_ids=focus_id_list,
            speaker_ids=intressent_ids,
        ),
        include_snippets=True,
        return_snippets=False,
    )

    # Same size guard as arango_search: fall back to snippets when the combined
    # full texts would blow up the orchestrator context.
    total_text_chars = sum(len(item.get("text") or "") for item in results if isinstance(item, dict))
    auto_snippet_mode = total_text_chars > 20_000
    snippet_mode = return_snippets or auto_snippet_mode

    hits: List[HitDocument] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        motion_id = item.get("_id", "")
        metadata = {
            "kind": "motion",
            "titel": item.get("titel"),
            "rm": item.get("rm"),
            "organ": item.get("organ"),
            "subtyp": item.get("subtyp"),
            "num_yrkanden": item.get("num_yrkanden"),
            "debateurl": item.get("url_session"),
        }
        if not item.get("has_text"):
            metadata["note"] = "endast inskannad PDF — fulltext saknas"
        hits.append(HitDocument(
            id=motion_id,
            key=motion_id.removeprefix("motions/"),
            speaker=item.get("speaker"),
            party=item.get("party"),
            date=item.get("date"),
            snippet=(item.get("snippet_long") if snippet_mode else item.get("snippet"))
                    or item.get("snippet") or "",
            text=None if snippet_mode else (item.get("text") or ""),
            score=item.get("bm25"),
            metadata=metadata,
        ))

    structured = SearchHitsResult(
        response=HitsResponse(hits=hits),
        stats=stats,
        focus_ids=[h.id for h in hits if h.id],
        limit_reached=limit_reached,
    )
    _tool_structured_result.set(structured)
    output = structured.response.to_string() or "(no results)"
    if auto_snippet_mode:
        note = (
            f"NOTE: The full texts of these {len(hits)} results total {total_text_chars:,} characters "
            f"(exceeds 20 000), so only snippets are shown above. "
            "You can either:\n"
            "  1. Pick specific motion IDs and call fetch_motion(dok_id) for the full text, or\n"
            "  2. Repeat the search with a lower `limit` to reduce the result set."
        )
        output = output + "\n\n---\n\n" + note
    return output


@register_tool()
def vector_search_motions(query: str, limit: int = 10) -> HitsResponse:
    """
    Semantic/conceptual search over MOTIONER (written proposals from MPs), using
    chunk embeddings of the motion texts. Complements search_motions the same way
    vector_search complements arango_search.

    Speeches (anföranden) are the PRIMARY source — search them first. Use this
    tool as a complement when:
    - You want to deepen speech-based findings with what MPs formally proposed
      and exact keywords may not appear in the motion text.
    - The user explicitly asks about motioner, or speeches gave no coverage.
    - You want motions similar in meaning to a phrase or idea.

    When NOT to use:
    - Exact word/phrase matching in motions → search_motions
    - Chamber speeches → vector_search
    - Counts/aggregations → database_query

    Args:
        query: Natural-language description of the topic.
        limit: Number of motions to return (default 10).

    Returns:
        HitsResponse with one hit per motion. The snippet is the best-matching
        yrkande (the motion's condensed formal proposal) when that is the
        strongest signal, otherwise the best full-text passage; metadata["matched"]
        says which ("yrkande" or "text"). Hit ids look like "motions/HD02846";
        cite with [src:DOK_ID].
    """
    print_yellow(f"[Tools] vector_search_motions → query='{query}' (top_k={limit})")

    query_vec = pg.make_embeddings([query])[0]

    # Two semantic signals: full-text chunks (coverage) and yrkanden (condensed,
    # to-the-point proposals). Merge per motion, keeping the best of each.
    chunk_rows = pg.execute(
        """
        SELECT motion_id, chunk_index, 1 - (embedding <=> %s::vector) AS score
        FROM motion_chunks
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """,
        (query_vec, query_vec, limit * 2),
    )
    yrkande_rows = pg.execute(
        """
        SELECT dok_id, nummer, lydelse, utskottet, kammaren,
               1 - (embedding <=> %s::vector) AS score
        FROM motion_yrkanden
        WHERE embedding IS NOT NULL
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """,
        (query_vec, query_vec, limit * 2),
    )
    if not chunk_rows and not yrkande_rows:
        return "(no results — motion embeddings may not be built yet)"

    merged: Dict[str, Dict[str, Any]] = {}
    for row in chunk_rows:
        slot = merged.setdefault(row["motion_id"], {"score": -1})
        if row["score"] > slot.get("chunk_score", -1):
            slot["chunk_score"] = row["score"]
            slot["chunk_index"] = row["chunk_index"]
        slot["score"] = max(slot["score"], row["score"])
    for row in yrkande_rows:
        slot = merged.setdefault(row["dok_id"], {"score": -1})
        if row["score"] > slot.get("yrkande_score", -1):
            slot["yrkande_score"] = row["score"]
            slot["yrkande"] = row
        slot["score"] = max(slot["score"], row["score"])

    top_ids = sorted(merged.keys(), key=lambda mid: merged[mid]["score"], reverse=True)[:limit]

    motion_rows = pg.execute(
        """
        SELECT dok_id, titel, rm, organ, datum::text AS datum, year,
               parties, author_names, num_yrkanden, dokument_url_html
        FROM motions
        WHERE dok_id = ANY(%s::text[])
        """,
        (top_ids,),
    )
    motion_map = {row["dok_id"]: row for row in motion_rows}

    hits: List[HitDocument] = []
    for motion_id in top_ids:
        data = merged[motion_id]
        parent = motion_map.get(motion_id, {})

        # Prefer the matching yrkande as the snippet — it is condensed and to the
        # point — falling back to the full-text chunk (with neighbours) otherwise.
        yrkande = data.get("yrkande")
        prefer_yrkande = yrkande is not None and (
            "chunk_index" not in data or data.get("yrkande_score", 0) >= data.get("chunk_score", 0)
        )
        metadata: Dict[str, Any] = {
            "kind": "motion",
            "titel": parent.get("titel"),
            "rm": parent.get("rm"),
            "organ": parent.get("organ"),
            "num_yrkanden": parent.get("num_yrkanden"),
            "debateurl": parent.get("dokument_url_html"),
        }
        if prefer_yrkande:
            snippet = yrkande["lydelse"]
            outcome = yrkande.get("kammaren") or yrkande.get("utskottet")
            if outcome:
                snippet += f"  [beslut: {outcome}]"
            metadata["matched"] = "yrkande"
            metadata["yrkande_nummer"] = yrkande.get("nummer")
        elif "chunk_index" in data:
            neighbor_rows = pg.execute(
                """
                SELECT text, chunk_index
                FROM motion_chunks
                WHERE motion_id = %s AND chunk_index IN (%s, %s, %s)
                ORDER BY chunk_index
                """,
                (motion_id, data["chunk_index"] - 1, data["chunk_index"], data["chunk_index"] + 1),
            )
            snippet = " ".join(r["text"] for r in neighbor_rows)
            metadata["matched"] = "text"
            metadata["chunk_index"] = data["chunk_index"]
        else:
            snippet = yrkande["lydelse"] if yrkande else ""
            metadata["matched"] = "yrkande" if yrkande else "text"

        author_names = parent.get("author_names") or []
        speaker = ", ".join(author_names[:3])
        if len(author_names) > 3:
            speaker += " m.fl."

        hits.append(
            HitDocument(
                id=f"motions/{motion_id}",
                key=motion_id,
                speaker=speaker,
                party="/".join(parent.get("parties") or []),
                date=str(parent.get("datum") or ""),
                snippet=snippet,
                score=data["score"],
                metadata=metadata,
            )
        )

    result = HitsResponse(hits=hits)
    _tool_structured_result.set(result)
    return result.to_string() or "(no results)"


# Keys worth surfacing from the raw dokforslag JSON per yrkande.
_YRKANDE_KEYS = (
    "nummer", "lydelse", "lydelse2", "utskottet", "kammaren",
    "kammarbeslutstyp", "behandlas_i",
)


@register_tool()
def fetch_motion(dok_id: str) -> dict:
    """
    Fetch one motion by its dok_id: metadata, all authors, all yrkanden (proposals)
    with committee and chamber outcomes, and the full text.

    Typical flow: search_motions / vector_search_motions → pick a hit →
    fetch_motion(dok_id) to read the yrkanden and full text.

    Args:
        dok_id: Motion id, e.g. "HD02846" or "motions/HD02846".

    Returns:
        dict with keys:
          dok_id, titel, undertitel, rm, beteckning, subtyp, organ, status, datum,
          authors: [{namn, partibet, intressent_id, roll}, ...],
          yrkanden: [{nummer, lydelse, utskottet, kammaren, behandlas_i}, ...]
            (utskottet = committee proposal, kammaren = chamber decision,
             behandlas_i = committee report where it was handled),
          text (truncated to ~30 000 chars),
          pdf_url, url,
          note (optional): present when the motion only exists as a scanned PDF.
    """
    bare_id = dok_id.split("/", 1)[1] if "/" in dok_id else dok_id
    print_yellow(f"[Tools] fetch_motion → dok_id='{bare_id}'")

    rows = pg.execute(
        """
        SELECT dok_id, titel, undertitel, rm, beteckning, subtyp, organ, status,
               datum::text AS datum, year, text, has_text, forslag,
               parties, author_names, pdf_url, dokument_url_html
        FROM motions
        WHERE dok_id = %s
        """,
        (bare_id,),
    )
    if not rows:
        return {"error": f"No motion found with dok_id '{bare_id}'."}
    motion = rows[0]

    author_rows = pg.execute(
        """
        SELECT namn, partibet, intressent_id, roll
        FROM motion_authors
        WHERE dok_id = %s
        ORDER BY ordinal
        """,
        (bare_id,),
    )

    forslag = motion.get("forslag") or []
    if isinstance(forslag, str):
        forslag = json.loads(forslag)
    yrkanden = [
        {k: f.get(k) for k in _YRKANDE_KEYS if f.get(k) is not None}
        for f in forslag
        if isinstance(f, dict)
    ]

    text = (motion.get("text") or "").strip()
    truncated = len(text) > _READER_SINGLE_BUDGET
    if truncated:
        text = text[:_READER_SINGLE_BUDGET] + "\n\n[...trunkerat...]"

    author_names = motion.get("author_names") or []
    speaker = ", ".join(author_names[:3])
    if len(author_names) > 3:
        speaker += " m.fl."

    # Register as citable source.
    _tool_structured_result.set(HitsResponse(hits=[
        HitDocument(
            id=f"motions/{bare_id}",
            key=bare_id,
            speaker=speaker,
            party="/".join(motion.get("parties") or []),
            date=str(motion.get("datum") or ""),
            text=text[:3000],
            snippet=(motion.get("titel") or "") + " — " + text[:300],
            metadata={
                "kind": "motion",
                "titel": motion.get("titel"),
                "rm": motion.get("rm"),
                "organ": motion.get("organ"),
                "debateurl": motion.get("dokument_url_html"),
            },
        )
    ]))

    result: Dict[str, Any] = {
        "dok_id": motion.get("dok_id"),
        "titel": motion.get("titel"),
        "undertitel": motion.get("undertitel"),
        "rm": motion.get("rm"),
        "beteckning": motion.get("beteckning"),
        "subtyp": motion.get("subtyp"),
        "organ": motion.get("organ"),
        "status": motion.get("status"),
        "datum": motion.get("datum"),
        "authors": [dict(a) for a in author_rows],
        "yrkanden": yrkanden,
        "text": text,
        "pdf_url": motion.get("pdf_url"),
        "url": motion.get("dokument_url_html"),
    }
    if not motion.get("has_text"):
        result["note"] = (
            "Motionen finns endast som inskannad PDF — fulltext saknas i databasen. "
            "Metadata och eventuella yrkanden ovan är kompletta."
        )
    if truncated:
        result["text_note"] = "Texten är trunkerad."
    return result


@register_tool()
def share_insight(
    message: str,
    speaker_ids: list = None,
    speaker_ids_context: str = None,
    hit_ids: list = None,
    sql: str = None,
    hits: list = None,
    rows: list = None,
) -> dict:
    """Surface a concrete finding to the user while you continue researching.

    Args:
        message (str): Brief observation in Swedish (1–3 sentences). Be specific and concrete.
            Use this to put other insights in context. It appears as the header of the card
            and should explain what the attached data shows. If you refer to specific persons
            in this message, make sure to include their intressent_id in speaker_ids so their
            portraits can be highlighted visually.
        hit_ids (list[str]): Optional. Talk IDs to surface as a search card (backend fetches
            metadata). Pass the talk IDs (e.g. ["H40911", "H40912"]) you saw in a previous
            arango_search or vector_search result. The backend fetches speaker/party/date/
            summary for each ID automatically — you do NOT need to copy the data yourself.
        sql (str): Optional. SQL query to re-execute for a stats card (preferred for surfacing
            stats tables). Re-pass the same SQL query you used in database_query (or a simplified
            variant). The backend re-executes it and builds the rows — you do NOT write rows=[]
            by hand.
        speaker_ids (list[str]): Optional. intressent_id values for portrait highlights (to show
            speaker portrait photos). List of intressent_id values for speakers you want to
            highlight visually. Always pair with speaker_ids_context to explain why they matter.
        speaker_ids_context (str): Optional. Caption for the speaker highlights (1–2 sentences
            explaining why these specific speakers are notable in this context).

    Returns:
        dict: Consumed by ChatService to emit a search_card, stats_card, or insight SSE event.

    Examples:
        >>> share_insight(
        ...     message="60 % av SD:s AI-debatter 2019–2022 hölls av tre talare.",
        ...     speaker_ids=["0448485371626", "0448485371627", "0448485371628"],
        ...     speaker_ids_context="Dessa tre talare stod för 60 % av SD:s AI-debatter 2019–2022.",
        ... )

        >>> share_insight(
        ...     message="Lista över politiker som nämnt 'artificiell intelligens' i sina tal, och hur många gånger var.",
        ...     sql="SELECT talare, COUNT(*) AS cnt FROM talks WHERE anforandetext @@ websearch_to_tsquery('swedish', 'artificiell intelligens') GROUP BY talare ORDER BY cnt DESC",
        ... )

        >>> share_insight(
        ...     message="Debatten om Luftvärn präglas av två huvudstrider: valet av vapensystem och kopplingen mellan antal stridsflygplan och luftvärsbehov.",
        ...     hit_ids=["H40911", "H40912", "H40913", "H40914", "H40915"],
        ... )
    """
    # Resolve hit_ids → hits by querying the talks table (motions as fallback)
    if hit_ids and not hits:
        # Normalize: strip "talks/"/"motions/" prefix if present
        bare_ids = [i.split("/", 1)[1] if "/" in i else i for i in hit_ids]
        try:
            talk_rows = pg.execute(
                """
                SELECT id, talare, parti, datum::text AS datum, intressent_id, summary
                FROM talks
                WHERE id = ANY(%s::text[])
                """,
                (bare_ids,),
            )
            hits = [
                {
                    "_id": f"talks/{r['id']}",
                    "speaker": r.get("talare"),
                    "party": r.get("parti"),
                    "date": r.get("datum"),
                    "snippet": (r.get("summary") or "")[:300],
                    "intressent_id": r.get("intressent_id"),
                }
                for r in talk_rows
            ]
            found = {r["id"] for r in talk_rows}
            missing = [i for i in bare_ids if i not in found]
            if missing:
                motion_rows = pg.execute(
                    """
                    SELECT dok_id, titel, parties, author_names, datum::text AS datum, text
                    FROM motions
                    WHERE dok_id = ANY(%s::text[])
                    """,
                    (missing,),
                )
                for r in motion_rows:
                    names = r.get("author_names") or []
                    speaker = ", ".join(names[:3]) + (" m.fl." if len(names) > 3 else "")
                    hits.append(
                        {
                            "_id": f"motions/{r['dok_id']}",
                            "speaker": speaker,
                            "party": "/".join(r.get("parties") or []),
                            "date": r.get("datum"),
                            "snippet": (r.get("titel") or "") or (r.get("text") or "")[:300],
                            "intressent_id": None,
                        }
                    )
        except Exception as e:
            print_red(f"[share_insight] Failed to fetch hit_ids: {e}")

    # Resolve sql → rows by re-executing the query
    if sql and not rows:
        try:
            rows = pg.execute(sql)
        except Exception as e:
            print_red(f"[share_insight] Failed to execute sql: {e}")
            rows = [{"error": str(e)}]

    # Resolve [src:ID] tags in plain insight messages to debateurls for footnote links.
    src_sources: dict = {}
    src_ids = re.findall(r"\[src:([A-Za-z0-9_-]+)\]", message)
    if src_ids and not hits and not rows:
        try:
            src_rows = pg.execute(
                "SELECT id, debateurl FROM talks WHERE id = ANY(%s::text[])",
                (src_ids,),
            )
            src_sources = {r["id"]: r["debateurl"] for r in src_rows if r.get("debateurl")}
        except Exception as e:
            print_red(f"[share_insight] Failed to fetch src debateurls: {e}")

    cb = _insight_callback.get()
    if cb:
        # Dispatch to the appropriate SSE event type based on what data is attached.
        if hits:
            cb({
                "type": "search_card",
                # message = insight text shown as card header (✓ …)
                # query is intentionally empty — the insight text is not a search query
                "message": message,
                "query": "",
                "results": hits[:8],
                "total": len(hits),
                "limit_reached": False,
                "stats": {},
                "speaker_ids": speaker_ids or [],
                "speaker_ids_context": speaker_ids_context or "",
            })
        elif rows:
            cb({
                "type": "stats_card",
                # message = insight text shown as card header (✓ …)
                "message": message,
                "rows": rows[:20],
                "speaker_ids": speaker_ids or [],
                "speaker_ids_context": speaker_ids_context or "",
            })
        else:
            cb({
                "type": "insight",
                "message": message,
                "sources": src_sources,
                "speaker_ids": speaker_ids or [],
                "speaker_ids_context": speaker_ids_context or "",
            })


@register_tool()
def lookup_source(source_ids: list[str]) -> str:
    """Återhämta lagrad grundtext för en eller flera tidigare registrerade källor.

    Sökverktyg (`arango_search`, `vector_search`, `fetch_debate`, `fetch_documents`)
    komprimeras automatiskt i meddelandehistoriken: bara `[src:ID]` plus en kort
    rubrikrad sparas. När du behöver det faktiska textinnehållet (t.ex. för att
    citera ordagrant eller verifiera ett påstående) — anropa det här verktyget
    med en lista av tal-id:n du redan sett.

    Args:
        source_ids: Lista av bara tal-id:n (t.ex. ["H40911", "GH09100"]) eller
            "talks/H40911"-format. ID:n som inte finns i registret utelämnas.

    Returns:
        Sträng med talare, parti, datum, rubrik och lagrad text per id.
        Returnerar en kort meddelandetext om inga av id:na hittades.
    """
    registry = _provenance_registry.get()
    if registry is None:
        return "ERROR: ingen provenance-registry är aktiv för denna session."
    if not source_ids:
        return "ERROR: source_ids är tom."

    # Cap output: at most 5 sources per call, 1500 chars per body. Keeps the
    # orchestrator's history bounded even if the model asks for many at once.
    MAX_IDS = 5
    PER_BODY_CAP = 1500

    requested = list(dict.fromkeys(source_ids))
    dropped_for_cap = requested[MAX_IDS:]
    requested = requested[:MAX_IDS]

    parts: list[str] = []
    missing: list[str] = []
    for raw_id in requested:
        sid = raw_id.split("/", 1)[1] if "/" in raw_id else raw_id
        rec = registry.get(sid)
        if rec is None:
            missing.append(raw_id)
            continue
        header = f"[src:{sid}]"
        meta = " | ".join(
            v for v in [rec.speaker, f"({rec.party})" if rec.party else None, rec.date, rec.heading]
            if v
        )
        body = rec.body or rec.snippet or "(ingen lagrad text)"
        if len(body) > PER_BODY_CAP:
            body = body[:PER_BODY_CAP].rstrip() + "…"
        parts.append(f"{header} {meta}\n{body}")

    if not parts:
        return f"Inga källor hittades. Kontrollera id:n: {missing}"

    notes: list[str] = []
    if missing:
        notes.append(f"{len(missing)} id hittades inte: {missing}")
    if dropped_for_cap:
        notes.append(
            f"max {MAX_IDS} källor per anrop — {len(dropped_for_cap)} utelämnades: {dropped_for_cap}. "
            "Anropa lookup_source igen om du behöver dessa."
        )
    suffix = "\n\n[Notera: " + " | ".join(notes) + "]" if notes else ""
    return "\n\n---\n\n".join(parts) + suffix


if __name__ == "__main__":
    print(vector_search("klimatförändringar", limit=3))
