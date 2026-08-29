"""
LLM tool implementations for the Riksdagen chat service.

Surface exposed to the orchestrator LLM:
  - search_speeches         → PostgreSQL full-text + metadata filters (SearchService)
  - vector_search         → unified chunk + summary semantic search, merged by speech_id
  - vector_search_debates → debate-level discovery (navigation, not citable)
  - fetch_debate          → drill into one debate, return its speeches with summaries
  - fetch_speeches       → full-text retrieval by id list
  - read_documents_for    → focused sub-agent read: full texts in, short answer out
  - database_query        → direct SQL for aggregations
  - share_insight         → side-channel to surface findings to the user mid-loop
  - search_documents        → full-text + metadata search over motioner (MotionSearchService)
  - vector_search_documents → semantic chunk search over motioner
  - fetch_document          → one motion: metadata, authors, yrkanden + outcomes, text
"""

import json
import os
import re
import re as _re_guard
import time  # Add this import for timing
from contextvars import ContextVar
from typing import Any

from pydantic import BaseModel, Field

from backend.services.search import MotionSearchService, SearchService
from packages.colorprinter import *
from packages.llm import LLM, register_tool
from postgres_client import pg, pg_llm
from prompts_loader import load_prompt, tool_doc

# Guard for model-authored SQL.
#
# Corpus text reaches the model's context, and in a parliament anyone able to
# speak can get text into the corpus — so treat generated SQL as untrusted.
#
# Four checks, and it is worth being clear about what each one is for:
#   1. only SELECT / WITH may run;
#   2. one statement only — a second statement is how a write gets smuggled in
#      behind a leading SELECT;
#   3. the query may only name the corpus tables;
#   4. pg.execute_readonly() opens a READ ONLY transaction.
#
# (4) stops writes, not reads, and this database also holds users, auth_tokens
# and chat sessions — so (3) is the only thing keeping generated SQL away from
# them. Even (3) is not the real guarantee: it is a parser, and a parser can be
# fooled. The guarantee is connecting as a role that was never granted SELECT on
# anything else (see SECURITY.md). This layer earns its place by giving the model
# a clear, correctable message instead of a permission error.
_ALLOWED_SQL = _re_guard.compile(r"^\s*(?:WITH|SELECT)\b", _re_guard.IGNORECASE)

# The corpus. Everything else in the database is off limits to generated SQL.
_LLM_READABLE_TABLES = frozenset({
    "speeches", "people", "debates",
    "documents", "document_authors", "document_proposals",
})

# Set-returning functions that read no table, so they are safe in FROM position.
# `unnest` is in the prompt's own guidance for the array columns.
_ALLOWED_TABLE_FUNCTIONS = frozenset({"unnest", "generate_series"})

_SQL_COMMENTS = _re_guard.compile(r"--[^\n]*|/\*.*?\*/", _re_guard.DOTALL)
_SQL_STRINGS = _re_guard.compile(r"'(?:[^']|'')*'")
_IDENT = r'(?:"[^"]*"|[A-Za-z_][A-Za-z_0-9$]*)'
_QUALIFIED_IDENT = _re_guard.compile(rf"{_IDENT}(?:\s*\.\s*{_IDENT})*")
_CTE_NAME = _re_guard.compile(
    rf"(?:\bWITH\b|,)\s*(?:RECURSIVE\s+)?({_IDENT})\s+AS\b", _re_guard.IGNORECASE
)
_FROM_OR_JOIN = _re_guard.compile(r"\b(?:FROM|JOIN)\b", _re_guard.IGNORECASE)
# Seeing one of these means the FROM list is over, so it is not a table or alias.
_CLAUSE_KEYWORDS = frozenset({
    "where", "group", "order", "limit", "offset", "having", "union", "intersect",
    "except", "on", "using", "join", "inner", "left", "right", "full", "cross",
    "natural", "window", "fetch", "for", "as", "lateral", "with", "select",
})


def _strip_literals(sql: str) -> str:
    """Blank out comments and string literals.

    Without this, a search term that happens to contain a table name —
    websearch_to_tsquery('swedish', 'users') — would read as a table reference.
    """
    return _SQL_STRINGS.sub("''", _SQL_COMMENTS.sub(" ", sql))


def _normalise_ident(raw: str) -> str:
    return raw.replace('"', "").strip().lower()


def _skip_space(sql: str, pos: int) -> int:
    while pos < len(sql) and sql[pos].isspace():
        pos += 1
    return pos


def _referenced_tables(sql: str) -> tuple[set[str], set[str]]:
    """Names in FROM/JOIN position, as (tables, set-returning functions).

    Handles comma-separated lists, aliases, quoting and schema qualification.
    A subquery contributes nothing directly — its own FROM is found by the same
    scan, which runs over the whole statement rather than one clause.
    """
    tables: set[str] = set()
    functions: set[str] = set()
    for match in _FROM_OR_JOIN.finditer(sql):
        pos = match.end()
        while True:
            pos = _skip_space(sql, pos)
            if pos >= len(sql) or sql[pos] == "(":
                break  # subquery or VALUES list
            ident = _QUALIFIED_IDENT.match(sql, pos)
            if not ident:
                break
            raw = ident.group(0)
            if _normalise_ident(raw.split(".")[0]) in _CLAUSE_KEYWORDS:
                break
            pos = ident.end()
            after = _skip_space(sql, pos)
            if after < len(sql) and sql[after] == "(":
                functions.add(_normalise_ident(raw.rsplit(".", 1)[-1]))
                depth = 0
                pos = after
                while pos < len(sql):
                    if sql[pos] == "(":
                        depth += 1
                    elif sql[pos] == ")":
                        depth -= 1
                        if depth == 0:
                            pos += 1
                            break
                    pos += 1
            else:
                # Keep the schema: public.users and pg_catalog.pg_tables must
                # both be judged, not just their tails.
                tables.add(".".join(_normalise_ident(p) for p in raw.split(".")))
            # An optional alias, with or without AS, then the rest of the list.
            pos = _skip_space(sql, pos)
            alias = _QUALIFIED_IDENT.match(sql, pos)
            if alias and _normalise_ident(alias.group(0)) not in _CLAUSE_KEYWORDS:
                pos = alias.end()
            elif alias and _normalise_ident(alias.group(0)) == "as":
                pos = _skip_space(sql, alias.end())
                named = _QUALIFIED_IDENT.match(sql, pos)
                if named:
                    pos = named.end()
            pos = _skip_space(sql, pos)
            if pos < len(sql) and sql[pos] == ",":
                pos += 1
                continue
            break
    return tables, functions


def _forbidden_tables(sql: str) -> list[str]:
    """Names the query reads that are not corpus tables. Deny by default, so a
    table added to the database later is invisible until someone allows it."""
    cleaned = _strip_literals(sql)
    ctes = {_normalise_ident(m.group(1)) for m in _CTE_NAME.finditer(cleaned)}
    tables, functions = _referenced_tables(cleaned)
    forbidden = []
    for name in sorted(tables):
        bare = name[len("public."):] if name.startswith("public.") else name
        if "." in bare or (bare not in _LLM_READABLE_TABLES and bare not in ctes):
            forbidden.append(name)
    forbidden += [f"{fn}()" for fn in sorted(functions)
                  if fn not in _ALLOWED_TABLE_FUNCTIONS]
    return forbidden


def _reject_unsafe_sql(sql: str) -> str | None:
    """Return an error message if this SQL must not run, else None."""
    stripped = sql.strip().rstrip(";").strip()
    if not _ALLOWED_SQL.match(stripped):
        first = (stripped.split() or ["(empty)"])[0]
        return (
            f"REFUSED: only SELECT and WITH queries may run, got {first!r}. "
            f"This tool is read-only."
        )
    # A second statement is how a write gets smuggled past a leading SELECT.
    if ";" in stripped:
        return (
            "REFUSED: multiple statements are not allowed. "
            "Send one SELECT (a WITH clause may precede it)."
        )
    forbidden = _forbidden_tables(stripped)
    if forbidden:
        return (
            f"REFUSED: this tool reads the parliamentary corpus only, and "
            f"{', '.join(forbidden)} is not part of it. Readable tables: "
            f"{', '.join(sorted(_LLM_READABLE_TABLES))}."
        )
    return None

# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────

class HitDocument(BaseModel):
    """Normalized representation of a search hit across tools."""

    id: str | None = Field(default=None, description="Document id (e.g. 'speeches/H40911')")
    key: str | None = Field(default=None, description="Document key without collection prefix.")
    speaker: str | None = Field(default=None)
    party: str | None = Field(default=None)
    date: str | None = Field(default=None)
    snippet: str | None = Field(default=None)
    text: str | None = Field(default=None)
    score: float | None = Field(default=None)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_string(self, include_metadata: bool = True) -> str:
        data: dict[str, Any] = self.model_dump(exclude_none=True)
        metadata: dict[str, Any] = data.pop("metadata", {})
        segments: list[str] = []
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
    speaker: str | None = None,
    party: str | None = None,
    date: str | None = None,
) -> str:
    """Return an enriched [src:...] tag that carries speaker/party/date inline.

    The orchestrator uses bare [src:ID] tags for citation tracking; this richer
    variant is injected after summarisation so the orchestrator still sees the
    attribution metadata even if the fast model stripped the original tags.

    The pipe form is not cosmetic: provenance._SRC_PATTERN accepts `[src:ID]` and
    `[src:ID | anything]`, and nothing else. A space-separated tag parses as no
    citation at all, so an orchestrator that copied one verbatim — exactly what it
    is told to do — silently lost the source. tests/test_provenance.py pins the two
    together.
    """
    if not (speaker or party or date):
        return f"[src:{bare_id}]"
    who = speaker or ""
    if party:
        who = f"{who} ({party})".strip()
    tail = [part for part in (who, str(date) if date else "") if part]
    return f"[src:{bare_id} | {' | '.join(tail)}]"


class HitsResponse(BaseModel):
    hits: list[HitDocument] = Field(default_factory=list)

    def to_string(self, include_metadata: bool = True) -> str:
        if not self.hits:
            return ""
        return "\n\n---\n\n".join(
            hit.to_string(include_metadata=include_metadata) for hit in self.hits
        )


class SearchHitsResult(BaseModel):
    """Returned by search_speeches. Wraps HitsResponse with search metadata."""
    type: str = "hits"
    response: HitsResponse
    stats: dict[str, Any] = Field(default_factory=dict)
    focus_ids: list[str] = Field(default_factory=list)
    limit_reached: bool = False


# Side-channel for passing structured hit results out of tool functions.
# The @register_tool() wrapper JSON-serialises the return value, so tools that
# want to hand structured data to ChatService store it here and return a plain
# string to the framework.  ChatService reads this var immediately after the call.
_tool_structured_result: ContextVar[Any | None] = ContextVar(
    "_tool_structured_result", default=None
)

# Callback for share_insight to publish SSE events directly without returning
# a value. Set by the shadow communicator thread before calling share_insight.
# Using a ContextVar means each thread has its own copy, so threads don't
# interfere with each other.
_insight_callback: ContextVar[Any | None] = ContextVar(
    "_insight_callback", default=None
)

# Active provenance registry for the current chat turn. Set by ChatService
# before tool execution so tools (and `lookup_source` in particular) can read
# back grounding text by source ID without going through the message history.
_provenance_registry: ContextVar[Any | None] = ContextVar(
    "_provenance_registry", default=None
)

# Fast LLM for the current request. Set by ChatService next to the provenance
# registry so the reader sub-agent (`read_documents_for`) honours per-request
# provider overrides without the user's key ever being stored module-side.
_fast_llm_var: ContextVar[Any | None] = ContextVar(
    "_fast_llm_var", default=None
)


# ─────────────────────────────────────────────────────────────────────────────
# database_query
# ─────────────────────────────────────────────────────────────────────────────

@register_tool(description=tool_doc("database_schema"))
def database_schema() -> str:
    """Return the full database reference.

    The always-on `database_query` description carries only the column names, which is
    what stops the model inventing one. The rest — coverage caveats, decision values,
    the party-casing trap, worked queries — is ~1,400 tokens that most turns never need,
    so it is fetched on demand rather than paid for on every request.

    Returns:
        The schema reference as markdown.
    """
    print_blue("[database_schema] full reference requested")
    return load_prompt("_shared/schema")


@register_tool(description=tool_doc("database_query"))
def database_query(sql: str) -> str:
    """Execute a SQL query against the Riksdag speeches database (PostgreSQL).

    The description the model reads lives in prompts/<lang>/tools/database_query.md;
    parameter docs below still come from this docstring.

    Args:
        sql: A PostgreSQL SELECT query string.

    Returns:
        Query result formatted as a string (raw result or error message).
    """
    print_blue(f"[database_query] SQL:\n{sql}")

    # Start timing the query execution
    start_time = time.time()

    import re as _re

    # Guard: rewrite `text @@` → `search_vector @@`.
    # The GIN index is on the stored tsvector column `search_vector`, not on `text`.
    # Using `text @@ tsquery` triggers an implicit on-the-fly to_tsvector conversion
    # with the default (not Swedish) text-search config → full table scan, 78+ seconds, empty results.
    _rewritten = _re.sub(
        r'\banforandetext\s*@@', 'search_vector @@', sql, flags=_re.IGNORECASE
    )
    if _rewritten != sql:
        print_yellow("[database_query] Rewrote text @@ → search_vector @@ (uses GIN index)")
        sql = _rewritten

    # Guard: reject LIKE/ILIKE on full-text columns — these bypass the FTS index,
    # cause slow sequential scans, and produce wrong results (e.g. 'ai' matches
    # 'Thai', 'kai', 'Ukraine').  The correct operator is @@ with websearch_to_tsquery.
    _text_cols = r"(text|summary)"
    if _re.search(rf"\b{_text_cols}\b.*?\bI?LIKE\b", sql, _re.IGNORECASE | _re.DOTALL) or \
       _re.search(rf"\bI?LIKE\b.*?\b{_text_cols}\b", sql, _re.IGNORECASE | _re.DOTALL):
        msg = (
            "TOOL USAGE ERROR: Do not use LIKE or ILIKE on 'text' or 'summary' — "
            "it is slow and produces wrong results. "
            "To search speech content, use the FTS operator instead:\n"
            "  WHERE search_vector @@ websearch_to_tsquery('swedish', 'your query here')\n"
            "This uses the GIN index and supports AND, OR, phrase search, and Swedish stemming. "
            "Example for counting speeches about AI per party:\n"
            "  SELECT party, COUNT(*) AS cnt FROM speeches\n"
            "  WHERE search_vector @@ websearch_to_tsquery('swedish', 'artificiell intelligens OR AI')\n"
            "  GROUP BY party ORDER BY cnt DESC"
        )
        print_red(f"[database_query] Blocked LIKE on text column: {sql[:120]}")
        return msg

    refusal = _reject_unsafe_sql(sql)
    if refusal:
        print_red(f"[database_query] {refusal} | {sql[:120]}")
        return refusal

    try:
        rows = pg_llm.execute_readonly(sql)
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

    # Enrich with person_id when rows have speaker_name but no person_id.
    # This lets the shadow communicator attach speaker portraits to stats insights,
    # and gives the main LLM the IDs for future search_speeches(person_ids=...) calls.
    rows_list = rows if isinstance(rows, list) else ([rows] if isinstance(rows, dict) else [])
    if (
        rows_list
        and isinstance(rows_list[0], dict)
        and "speaker_name" in rows_list[0]
        and "person_id" not in rows_list[0]
    ):
        names = list({r["speaker_name"] for r in rows_list if isinstance(r, dict) and r.get("speaker_name")})
        try:
            person_rows_extra = pg.execute(
                "SELECT person_id, name FROM people WHERE name = ANY(%s)",
                (names,),
            )
            name_to_iid = {r["name"]: r["person_id"] for r in person_rows_extra}
            if name_to_iid:
                if isinstance(rows, list):
                    rows = [
                        {**r, "person_id": name_to_iid[r["speaker_name"]]}
                        if isinstance(r, dict) and r.get("speaker_name") in name_to_iid
                        else r
                        for r in rows
                    ]
                elif isinstance(rows, dict) and rows.get("speaker_name") in name_to_iid:
                    rows = {**rows, "person_id": name_to_iid[rows["speaker_name"]]}
                rows_list = rows if isinstance(rows, list) else [rows]
                print_yellow(f"[database_query] Enriched {len(name_to_iid)} rows with person_id")
        except Exception as e:
            print_yellow(f"[database_query] person_id enrichment failed: {e}")

    # Store rows for ChatService to populate collected_persons before shadow fires.
    _tool_structured_result.set({"type": "db_rows", "rows": rows_list})

    print_blue(f"[database_query] ---\n{sql}\n---")
    result_str = f"SQL result: {rows}{truncated_note}"
    print_blue(f"[database_query] Returning:\n{result_str[:200]}")
    return result_str


# ─────────────────────────────────────────────────────────────────────────────
# vector_search  (unified: speech_chunks + summaries, merged by speech_id)
# ─────────────────────────────────────────────────────────────────────────────

@register_tool(description=tool_doc("vector_search"))
def vector_search(query: str, limit: int = 10) -> HitsResponse:
    """

    The description the model reads lives in prompts/<lang>/tools/vector_search.md;
    parameter docs below still come from this docstring.

    Args:
        query: Natural-language description of the topic.
        limit: Number of merged hits to return (default 10).

    Returns:
        HitsResponse with the top-limit speeches scored by max(chunk, summary).
    """
    print_yellow(f"[Tools] vector_search → query='{query}' (top_k={limit})")

    query_vec = pg.make_embeddings([query])[0]
    fetch_each = limit * 2  # oversample each index so the merge has room to dedupe

    chunk_rows = pg.execute(
        """
        SELECT id, speech_id, chunk_index, text,
               1 - (embedding <=> %s::vector) AS score
        FROM speech_chunks
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """,
        (query_vec, query_vec, fetch_each),
    )

    summary_rows = pg.execute(
        """
        SELECT id, summary,
               1 - (summary_embedding <=> %s::vector) AS score
        FROM speeches
        WHERE summary_embedding IS NOT NULL
        ORDER BY summary_embedding <=> %s::vector
        LIMIT %s
        """,
        (query_vec, query_vec, fetch_each),
    )

    if not chunk_rows and not summary_rows:
        return ""

    # Merge by speech_id. Per-talk keep the best chunk hit and the summary hit.
    merged: dict[str, dict[str, Any]] = {}
    for row in chunk_rows:
        speech_id = row["speech_id"]
        slot = merged.setdefault(speech_id, {})
        if row["score"] > slot.get("chunk_score", -1):
            slot["chunk_score"] = row["score"]
            slot["chunk_index"] = row["chunk_index"]
            slot["chunk_text"] = row["text"]
    for row in summary_rows:
        speech_id = row["id"]
        slot = merged.setdefault(speech_id, {})
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
        SELECT id, speaker_name, party, date::text AS date, person_id, title, url_video
        FROM speeches
        WHERE id = ANY(%s::text[])
        """,
        (top_ids,),
    )
    talk_map = {row["id"]: row for row in talk_rows}

    hits: list[HitDocument] = []
    for speech_id in top_ids:
        data = merged[speech_id]
        parent = talk_map.get(speech_id, {})
        has_chunk = "chunk_text" in data
        has_summary = "summary_text" in data
        source_type = (
            "both" if has_chunk and has_summary else ("chunk" if has_chunk else "summary")
        )

        if has_chunk:
            # Neighbor speech_chunks give the LLM a bit of context around the hit.
            neighbor_rows = pg.execute(
                """
                SELECT text, chunk_index
                FROM speech_chunks
                WHERE speech_id = %s AND chunk_index IN (%s, %s, %s)
                ORDER BY chunk_index
                """,
                (speech_id, data["chunk_index"] - 1, data["chunk_index"], data["chunk_index"] + 1),
            )
            snippet = " ".join(r["text"] for r in neighbor_rows)
        else:
            snippet = (data.get("summary_text") or "")[:800]

        metadata: dict[str, Any] = {
            "speech_id": speech_id,
            "source_type": source_type,
            "person_id": parent.get("person_id"),
            "title": parent.get("title"),
            "url_video": parent.get("url_video"),
        }
        if has_chunk:
            metadata["chunk_index"] = data["chunk_index"]
        if has_summary and has_chunk:
            # Only attach summary separately when the snippet is the chunk text.
            metadata["summary"] = (data["summary_text"] or "")[:500]

        hits.append(
            HitDocument(
                id=f"speeches/{speech_id}",
                key=speech_id,
                speaker=parent.get("speaker_name"),
                party=parent.get("party"),
                date=str(parent.get("date") or ""),
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

@register_tool(description=tool_doc("vector_search_debates"))
def vector_search_debates(query: str, limit: int = 5) -> HitsResponse:
    """

    The description the model reads lives in prompts/<lang>/tools/vector_search_debates.md;
    parameter docs below still come from this docstring.

    Args:
        query: Natural-language description of the topic.
        limit: Number of debates to return (default 5).

    Returns:
        HitsResponse. Each hit uses the bare debate id (e.g. "2021-06-17:42");
        `snippet` is the debate summary, metadata includes `num_talks` and `date`.
    """
    print_yellow(f"[Tools] vector_search_debates → query='{query}' (top_k={limit})")

    query_vec = pg.make_embeddings([query])[0]

    rows = pg.execute(
        """
        SELECT d.debate, d.date::text AS date, d.summary, d.num_talks,
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
    hits: list[HitDocument] = [
        HitDocument(
            id=row["debate"],
            key=row["debate"],
            speaker=None,
            party=None,
            date=str(row.get("date") or ""),
            snippet=(row.get("summary") or "")[:400],
            score=row.get("score"),
            metadata={
                "kind": "debate",
                "num_talks": row.get("num_talks"),
                "date": str(row.get("date") or ""),
            },
        )
        for row in rows
    ]

    result = HitsResponse(hits=hits)
    _tool_structured_result.set(result)
    return result.to_string() or "(no results)"


# ─────────────────────────────────────────────────────────────────────────────
# fetch_debate  (drill down from a debate id to the speeches inside)
# ─────────────────────────────────────────────────────────────────────────────

# Combined character budget for talk summaries in the response. If the full set
# of summaries exceeds this, we either rank by relevance to `query` (if given)
# or fall back to the oldest-first subset whose summaries fit.
FETCH_DEBATE_SUMMARY_BUDGET_CHARS = 7000


@register_tool(description=tool_doc("fetch_debate"))
def fetch_debate(debate_id: str, query: str | None = None) -> dict:
    """

    The description the model reads lives in prompts/<lang>/tools/fetch_debate.md;
    parameter docs below still come from this docstring.

    Args:
        debate_id: Debate id of the form "{YYYY-MM-DD}:{n}" (e.g. "2021-06-17:42").
        query: Optional search query. When the debate's combined talk summaries
            exceed the response budget, speeches are ranked by embedding distance
            to this query and only the most relevant ones are returned
            (presented in chronological order). Strongly recommended for long
            debates — without it, a truncated chronological slice is returned.

    Returns:
        dict with keys:
          debate_id, date, summary, num_talks,
          speeches: [{id, speaker_name, party, person_id, summary}, ...],
          note (optional): present when not all speeches are returned; explains
            how many were omitted and on what basis.
    """
    print_yellow(
        f"[Tools] fetch_debate → debate_id='{debate_id}'"
        + (f" query='{query}'" if query else "")
    )

    debate_rows = pg.execute(
        """
        SELECT id, date::text AS date, summary, num_talks, talk_ids
        FROM debates
        WHERE id = %s
        """,
        (debate_id,),
    )
    if not debate_rows:
        return {"error": f"No debate found with id '{debate_id}'."}
    debate = debate_rows[0]

    talk_ids: list[str] = list(debate.get("talk_ids") or [])

    talk_rows = pg.execute(
        """
        SELECT id, sequence, speaker_name, party, person_id, summary,
               date::text AS date, title, url_video
        FROM speeches
        WHERE id = ANY(%s::text[])
        ORDER BY sequence ASC
        """,
        (talk_ids,),
    )

    total_summary_chars = sum(len(r.get("summary") or "") for r in talk_rows)
    trimmed_rows = talk_rows
    note: str | None = None

    if total_summary_chars > FETCH_DEBATE_SUMMARY_BUDGET_CHARS:
        ranking_method = "chronological"
        chosen_ids: set = set()
        running = 0

        if query:
            # Rank speeches in this debate by embedding distance to the query,
            # then keep the top-K whose summaries fit the budget.
            embedding = pg.make_embeddings([query])[0]
            ranked = pg.execute(
                """
                SELECT id, (summary_embedding <=> %s::vector) AS distance
                FROM speeches
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
            # Either no query was given, or no speeches in this debate have
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
                f"Debate has {len(talk_rows)} speeches (combined summaries "
                f"{total_summary_chars} chars). Returned the {len(trimmed_rows)} "
                f"most relevant to query '{query}'; {omitted} speeches omitted. "
                f"Use fetch_speeches with specific ids for full texts."
            )
        else:
            reason = (
                "no summary embeddings available for this debate yet; ranked chronologically"
                if query
                else "ranked chronologically — pass `query` for relevance ranking"
            )
            note = (
                f"Debate has {len(talk_rows)} speeches (combined summaries "
                f"{total_summary_chars} chars). Returned the first "
                f"{len(trimmed_rows)} summarised speeches ({reason}); "
                f"{omitted} speeches omitted. Use fetch_speeches for full texts."
            )

    # Build a compact dict for the LLM; register the speeches as provenance sources.
    talks_out: list[dict[str, Any]] = []
    hits: list[HitDocument] = []
    for row in trimmed_rows:
        speech_id = row["id"]
        summary_text = row.get("summary") or ""
        talks_out.append(
            {
                "id": speech_id,
                "speaker_name": row.get("speaker_name"),
                "party": row.get("party"),
                "person_id": row.get("person_id"),
                "summary": summary_text,
            }
        )
        hits.append(
            HitDocument(
                id=f"speeches/{speech_id}",
                key=speech_id,
                speaker=row.get("speaker_name"),
                party=row.get("party"),
                date=str(row.get("date") or ""),
                snippet=summary_text[:500],
                metadata={
                    "person_id": row.get("person_id"),
                    "title": row.get("title"),
                    "url_video": row.get("url_video"),
                    "debate": debate_id,
                },
            )
        )

    if hits:
        _tool_structured_result.set(HitsResponse(hits=hits))

    result: dict[str, Any] = {
        "debate_id": debate.get("id"),
        "date": debate.get("date"),
        "summary": debate.get("summary"),
        "num_talks": debate.get("num_talks") or len(talk_ids),
        "speeches": talks_out,
    }
    if note:
        result["note"] = note
    return result


# ─────────────────────────────────────────────────────────────────────────────
# fetch_speeches
# ─────────────────────────────────────────────────────────────────────────────

@register_tool(description=tool_doc("fetch_speeches"))
def fetch_speeches(_ids: list[str], collection: str = "", fields: list = None) -> list:
    """

    The description the model reads lives in prompts/<lang>/tools/fetch_speeches.md;
    parameter docs below still come from this docstring.

    Args:
        _ids: List of document IDs (e.g. ["speeches/H40911", "speeches/H40912"] or bare keys)
        collection: Optional prefix to add to bare IDs (e.g. "speeches")
        fields: Optional list of field names to return (empty = common fields)

    Returns:
        List of document dicts, or error message string.
    """
    # Normalize IDs: strip "speeches/" prefix
    if fields is None:
        fields = []
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
            "id", "text", "sequence", "activity_type",
            "speaker_name", "date", "year", "party", "person_id", "title",
            "related_doc_id", "debate_id", "is_reply", "summary", "tags",
        }
        # Cast date to text so Python receives a string, not a date object
        def _col(f: str) -> str:
            return "date::text AS date" if f == "date" else f
        select = ", ".join(_col(f) for f in fields if f in allowed or f.startswith("_"))
        if not select:
            select = "id, text, speaker_name, party, date::text AS date, year, activity_type"
    else:
        select = (
            "id, text, sequence, activity_type, "
            "speaker_name, date::text AS date, year, party, person_id, title, "
            "related_doc_id, debate_id, is_reply, summary, tags"
        )

    rows = pg.execute(
        f"SELECT {select} FROM speeches WHERE id = ANY(%s::text[])",
        (talk_ids,),
    )

    # Re-add _id and _key virtual fields for downstream compatibility
    result = []
    for row in rows:
        doc = dict(row)
        speech_id = doc.get("id", "")
        doc["_id"] = f"speeches/{speech_id}"
        doc["_key"] = speech_id
        result.append(doc)

    # Publish structured provenance so ChatService can track these as sources
    hits = []
    for doc in result:
        hits.append(
            HitDocument(
                id=doc.get("_id"),
                key=doc.get("_key"),
                speaker=doc.get("speaker_name"),
                party=doc.get("party"),
                date=doc.get("date"),
                text=doc.get("text", ""),
                snippet=doc.get("summary") or (doc.get("text") or "")[:300],
                metadata={
                    "person_id": doc.get("person_id"),
                    "title": doc.get("title"),
                    "activity_type": doc.get("activity_type"),
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
_default_reader_llm: LLM | None = None


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


@register_tool(description=tool_doc("read_documents_for"))
def read_documents_for(question: str, _ids: list[str]) -> str:
    """

    The description the model reads lives in prompts/<lang>/tools/read_documents_for.md;
    parameter docs below still come from this docstring.

    Args:
        question: One concrete question in Swedish, e.g.
            "Vilka argument anför talarna mot höjd bensinskatt?"
        _ids: 1-6 document IDs from earlier search results
            (e.g. ["H40911", "speeches/H40912"]). Motion ids from search_documents
            (e.g. "documents/HD02846") work too — the full motion text is read.

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
        "SELECT id, text, speaker_name, party, date::text AS date, title, "
        "person_id, summary FROM speeches WHERE id = ANY(%s::text[])",
        (talk_ids,),
    )
    by_id = {r["id"]: dict(r) for r in rows}

    # Ids not found among speeches may be documents — read those too.
    missing_ids = [tid for tid in talk_ids if tid not in by_id]
    if missing_ids:
        motion_rows = pg.execute(
            "SELECT doc_id, text, title, parties, author_names, date::text AS date "
            "FROM documents WHERE doc_id = ANY(%s::text[])",
            (missing_ids,),
        )
        for r in motion_rows:
            names = r.get("author_names") or []
            speaker = ", ".join(names[:3]) + (" m.fl." if len(names) > 3 else "")
            by_id[r["doc_id"]] = {
                "id": r["doc_id"],
                "text": r.get("text"),
                "speaker_name": speaker,
                "party": "/".join(r.get("parties") or []),
                "date": r.get("date"),
                "title": r.get("title"),
                "person_id": None,
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
        text = (doc.get("text") or "").strip()
        if not text:
            blocks.append(f"== [src:{tid}] ==\n(dokumentet saknar text)")
            continue
        if len(text) > budget:
            text = text[:budget] + "\n\n[...trunkerat...]"
        header_parts = [f"[src:{tid}]"]
        if doc.get("speaker_name"):
            speaker = doc["speaker_name"]
            if doc.get("party"):
                speaker += f" ({doc['party']})"
            header_parts.append(speaker)
        if doc.get("date"):
            header_parts.append(doc["date"])
        header = "== " + " | ".join(header_parts)
        if doc.get("title"):
            header += f" == {doc['title']}"
        else:
            header += " =="
        blocks.append(f"{header}\n{text}")
        collection = "documents" if doc.get("_kind") == "motion" else "speeches"
        hits.append(
            HitDocument(
                id=f"{collection}/{tid}",
                key=tid,
                speaker=doc.get("speaker_name"),
                party=doc.get("party"),
                date=doc.get("date"),
                text=doc.get("text", "")[:3000],
                snippet=doc.get("summary") or (doc.get("text") or "")[:300],
                metadata={
                    "person_id": doc.get("person_id"),
                    "title": doc.get("title"),
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
# _normalize_search_args  (unchanged helper)
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_search_args(
    query: str,
    parties: str | list[str] | None = None,
    people: str | list[str] | None = None,
    debates: str | list[str] | None = None,
    from_year: str | int | None = None,
    to_year: str | int | None = None,
    limit: str | int | None = 10,
    speaker_ids: str | list[str] | bool | None = None,
) -> dict[str, Any]:
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


@register_tool(description=tool_doc("search_speeches"))
def search_speeches(
    query: str,
    parties: list[str] | None = None,
    people: list[str] | None = None,
    from_year: int | None = None,
    to_year: int | None = None,
    limit: int = 20,
    return_snippets: bool = False,
    focus_ids: list[str] | None = None,
    person_ids: str | list[str] | bool | None = None,
) -> "SearchHitsResult":
    """

    The description the model reads lives in prompts/<lang>/tools/search_speeches.md;
    parameter docs below still come from this docstring.

    Args:
        query: The search string (supports AND, OR, NOT, phrases in quotes, år:2018-2022).
        parties: List of party codes to filter by (e.g., ["S", "M"]).
        people: List of speaker names to filter by.
        from_year: Start year for filtering.
        to_year: End year for filtering.
        limit: Maximum number of results (default 20).
        return_snippets: If True, return only snippets with highlights.
        focus_ids: Restrict search to these specific document ids.
        person_ids: An array of numeric strings (e.g., ['0448485371626', '0448485371627']). NEVER guess or make up an ID. If you do not know the exact numeric ID, you MUST use the people parameter instead."

    Returns:
        SearchHitsResult with hits and search metadata.
    """
    # Validate person_ids: they must be purely numeric strings.
    # If the model passes a placeholder like "PERS_ID_FOR_X", reject the whole call
    # so it knows to use `people=` instead or wait until it has real IDs from results.
    if person_ids:
        ids_list = (
            json.loads(person_ids) if isinstance(person_ids, str) else person_ids
        )
        if isinstance(ids_list, list):
            bad = [sid for sid in ids_list if not str(sid).strip().isdigit()]
            if bad:
                return f"ERROR: person_ids must be numeric strings (e.g. '0448485371626'). Invalid values: {bad}. Use the `people` parameter to search by name, or only pass person_ids you have seen in previous results."

    args = _normalize_search_args(
        query=query,
        parties=parties,
        people=people,
        from_year=from_year,
        to_year=to_year,
        limit=limit,
        speaker_ids=person_ids,
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

    focus_id_list: list[str] = []
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
            speaker_ids=args["speaker_ids"],
        ),
        include_snippets=True,
        return_snippets=False,
    )

    # Decide whether to include full text or fall back to snippets.
    # Two triggers: caller explicitly asked for snippets, or total text is too large.
    total_text_chars = sum(len(item.get("text") or "") for item in results if isinstance(item, dict))
    auto_snippet_mode = total_text_chars > 20_000
    snippet_mode = return_snippets or auto_snippet_mode

    hits: list[HitDocument] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        speech_id = item.get("_id", "")
        if snippet_mode:
            # Only include snippet fields, not full text
            hits.append(HitDocument(
                id=speech_id,
                key=speech_id.removeprefix("speeches/"),
                speaker=item.get("speaker"),
                party=item.get("party"),
                date=item.get("date"),
                snippet=item.get("snippet_long") or item.get("snippet") or "",
                text=None,
                score=item.get("bm25"),
                metadata={
                    "person_id": item.get("person_id"),
                    "url_video": item.get("url_session") or item.get("url_video"),
                    "title": item.get("title"),
                    "activity_type": item.get("activity_type"),
                    "chunk_index": item.get("chunk_index", -1),
                },
            ))
        else:
            hits.append(HitDocument(
                id=speech_id,
                key=speech_id.removeprefix("speeches/"),
                speaker=item.get("speaker"),
                party=item.get("party"),
                date=item.get("date"),
                snippet=item.get("snippet") or item.get("snippet_long") or "",
                text=item.get("text") or "",
                score=item.get("bm25"),
                metadata={
                    "person_id": item.get("person_id"),
                    "url_video": item.get("url_session") or item.get("url_video"),
                    "title": item.get("title"),
                    "activity_type": item.get("activity_type"),
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

@register_tool(description=tool_doc("search_documents"))
def search_documents(
    query: str,
    parties: list[str] | None = None,
    people: list[str] | None = None,
    from_year: int | None = None,
    to_year: int | None = None,
    limit: int = 20,
    return_snippets: bool = False,
    focus_ids: list[str] | None = None,
    person_ids: str | list[str] | bool | None = None,
) -> "SearchHitsResult":
    """

    The description the model reads lives in prompts/<lang>/tools/search_documents.md;
    parameter docs below still come from this docstring.

    Args:
        query: The search string (supports AND, OR, NOT, phrases in quotes, år:2018-2022).
        parties: List of party codes to filter by (e.g., ["S", "M"]). Matches any co-author.
        people: List of author names to filter by (matches any signatory).
        from_year: Start year (riksmöte start year) for filtering.
        to_year: End year for filtering.
        limit: Maximum number of results (default 20).
        return_snippets: If True, return only snippets with highlights.
        focus_ids: Restrict search to these specific motion ids.
        person_ids: An array of numeric strings (e.g., ['0448485371626']). NEVER guess
            or make up an ID. If you do not know the exact numeric ID, use `people` instead.

    Returns:
        SearchHitsResult. Hit ids look like "documents/HD02846"; metadata includes title,
        committee (committee), session_label (riksmöte) and num_proposals. Cite with [src:DOK_ID].
    """
    if person_ids:
        ids_list = (
            json.loads(person_ids) if isinstance(person_ids, str) else person_ids
        )
        if isinstance(ids_list, list):
            bad = [sid for sid in ids_list if not str(sid).strip().isdigit()]
            if bad:
                return f"ERROR: person_ids must be numeric strings (e.g. '0448485371626'). Invalid values: {bad}. Use the `people` parameter to search by name, or only pass person_ids you have seen in previous results."

    args = _normalize_search_args(
        query=query,
        parties=parties,
        people=people,
        from_year=from_year,
        to_year=to_year,
        limit=limit,
        speaker_ids=person_ids,
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

    focus_id_list: list[str] = []
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

    print_yellow(f"[Tools] search_documents → query='{query}' limit={args['limit']}")

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
            speaker_ids=args["speaker_ids"],
        ),
        include_snippets=True,
        return_snippets=False,
    )

    # Same size guard as search_speeches: fall back to snippets when the combined
    # full texts would blow up the orchestrator context.
    total_text_chars = sum(len(item.get("text") or "") for item in results if isinstance(item, dict))
    auto_snippet_mode = total_text_chars > 20_000
    snippet_mode = return_snippets or auto_snippet_mode

    hits: list[HitDocument] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        doc_id = item.get("_id", "")
        metadata = {
            "kind": "motion",
            "title": item.get("title"),
            "session_label": item.get("session_label"),
            "committee": item.get("committee"),
            "subtype": item.get("subtype"),
            "num_proposals": item.get("num_proposals"),
            "url_video": item.get("url_session"),
        }
        if not item.get("has_text"):
            metadata["note"] = "endast inskannad PDF — fulltext saknas"
        hits.append(HitDocument(
            id=doc_id,
            key=doc_id.removeprefix("documents/"),
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
            "  1. Pick specific motion IDs and call fetch_document(doc_id) for the full text, or\n"
            "  2. Repeat the search with a lower `limit` to reduce the result set."
        )
        output = output + "\n\n---\n\n" + note
    return output


@register_tool(description=tool_doc("vector_search_documents"))
def vector_search_documents(query: str, limit: int = 10) -> HitsResponse:
    """

    The description the model reads lives in prompts/<lang>/tools/vector_search_documents.md;
    parameter docs below still come from this docstring.

    Args:
        query: Natural-language description of the topic.
        limit: Number of documents to return (default 10).

    Returns:
        HitsResponse with one hit per motion. The snippet is the best-matching
        yrkande (the motion's condensed formal proposal) when that is the
        strongest signal, otherwise the best full-text passage; metadata["matched"]
        says which ("yrkande" or "text"). Hit ids look like "documents/HD02846";
        cite with [src:DOK_ID].
    """
    print_yellow(f"[Tools] vector_search_documents → query='{query}' (top_k={limit})")

    query_vec = pg.make_embeddings([query])[0]

    # Two semantic signals: full-text speech_chunks (coverage) and yrkanden (condensed,
    # to-the-point proposals). Merge per motion, keeping the best of each.
    chunk_rows = pg.execute(
        """
        SELECT doc_id, chunk_index, 1 - (embedding <=> %s::vector) AS score
        FROM document_chunks
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """,
        (query_vec, query_vec, limit * 2),
    )
    yrkande_rows = pg.execute(
        """
        SELECT doc_id, number, text, committee_recommendation, chamber_decision,
               1 - (embedding <=> %s::vector) AS score
        FROM document_proposals
        WHERE embedding IS NOT NULL
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """,
        (query_vec, query_vec, limit * 2),
    )
    if not chunk_rows and not yrkande_rows:
        return "(no results — motion embeddings may not be built yet)"

    merged: dict[str, dict[str, Any]] = {}
    for row in chunk_rows:
        slot = merged.setdefault(row["doc_id"], {"score": -1})
        if row["score"] > slot.get("chunk_score", -1):
            slot["chunk_score"] = row["score"]
            slot["chunk_index"] = row["chunk_index"]
        slot["score"] = max(slot["score"], row["score"])
    for row in yrkande_rows:
        slot = merged.setdefault(row["doc_id"], {"score": -1})
        if row["score"] > slot.get("yrkande_score", -1):
            slot["yrkande_score"] = row["score"]
            slot["yrkande"] = row
        slot["score"] = max(slot["score"], row["score"])

    top_ids = sorted(merged.keys(), key=lambda mid: merged[mid]["score"], reverse=True)[:limit]

    motion_rows = pg.execute(
        """
        SELECT doc_id, title, session_label, committee, date::text AS date, session_year AS year,
               parties, author_names, num_proposals, url_html
        FROM documents
        WHERE doc_id = ANY(%s::text[])
        """,
        (top_ids,),
    )
    motion_map = {row["doc_id"]: row for row in motion_rows}

    hits: list[HitDocument] = []
    for doc_id in top_ids:
        data = merged[doc_id]
        parent = motion_map.get(doc_id, {})

        # Prefer the matching yrkande as the snippet — it is condensed and to the
        # point — falling back to the full-text chunk (with neighbours) otherwise.
        yrkande = data.get("yrkande")
        prefer_yrkande = yrkande is not None and (
            "chunk_index" not in data or data.get("yrkande_score", 0) >= data.get("chunk_score", 0)
        )
        metadata: dict[str, Any] = {
            "kind": "motion",
            "title": parent.get("title"),
            "session_label": parent.get("session_label"),
            "committee": parent.get("committee"),
            "num_proposals": parent.get("num_proposals"),
            "url_video": parent.get("url_html"),
        }
        if prefer_yrkande:
            snippet = yrkande["text"]
            outcome = yrkande.get("chamber_decision") or yrkande.get("committee_recommendation")
            if outcome:
                snippet += f"  [beslut: {outcome}]"
            metadata["matched"] = "yrkande"
            metadata["yrkande_nummer"] = yrkande.get("number")
        elif "chunk_index" in data:
            neighbor_rows = pg.execute(
                """
                SELECT text, chunk_index
                FROM document_chunks
                WHERE doc_id = %s AND chunk_index IN (%s, %s, %s)
                ORDER BY chunk_index
                """,
                (doc_id, data["chunk_index"] - 1, data["chunk_index"], data["chunk_index"] + 1),
            )
            snippet = " ".join(r["text"] for r in neighbor_rows)
            metadata["matched"] = "text"
            metadata["chunk_index"] = data["chunk_index"]
        else:
            snippet = yrkande["text"] if yrkande else ""
            metadata["matched"] = "yrkande" if yrkande else "text"

        author_names = parent.get("author_names") or []
        speaker = ", ".join(author_names[:3])
        if len(author_names) > 3:
            speaker += " m.fl."

        hits.append(
            HitDocument(
                id=f"documents/{doc_id}",
                key=doc_id,
                speaker=speaker,
                party="/".join(parent.get("parties") or []),
                date=str(parent.get("date") or ""),
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
    "number", "text", "lydelse2", "committee_recommendation", "chamber_decision",
    "kammarbeslutstyp", "handled_in",
)


@register_tool(description=tool_doc("fetch_document"))
def fetch_document(doc_id: str) -> dict:
    """

    The description the model reads lives in prompts/<lang>/tools/fetch_document.md;
    parameter docs below still come from this docstring.

    Args:
        doc_id: Motion id, e.g. "HD02846" or "documents/HD02846".

    Returns:
        dict with keys:
          doc_id, title, subtitle, session_label, designation, subtype, committee, status, date,
          authors: [{name, party, person_id, role}, ...],
          yrkanden: [{number, text, committee_recommendation, chamber_decision, handled_in}, ...]
            (committee_recommendation = committee proposal, chamber_decision = chamber decision,
             handled_in = committee report where it was handled),
          text (truncated to ~30 000 chars),
          url_pdf, url,
          note (optional): present when the motion only exists as a scanned PDF.
    """
    bare_id = doc_id.split("/", 1)[1] if "/" in doc_id else doc_id
    print_yellow(f"[Tools] fetch_document → doc_id='{bare_id}'")

    rows = pg.execute(
        """
        SELECT doc_id, title, subtitle, session_label, designation, subtype, committee, status,
               date::text AS date, session_year AS year, text, has_text, proposals_raw,
               parties, author_names, url_pdf, url_html
        FROM documents
        WHERE doc_id = %s
        """,
        (bare_id,),
    )
    if not rows:
        return {"error": f"No motion found with doc_id '{bare_id}'."}
    motion = rows[0]

    author_rows = pg.execute(
        """
        SELECT name, party, person_id, role
        FROM document_authors
        WHERE doc_id = %s
        ORDER BY ordinal
        """,
        (bare_id,),
    )

    proposals_raw = motion.get("proposals_raw") or []
    if isinstance(proposals_raw, str):
        proposals_raw = json.loads(proposals_raw)
    yrkanden = [
        {k: f.get(k) for k in _YRKANDE_KEYS if f.get(k) is not None}
        for f in proposals_raw
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
            id=f"documents/{bare_id}",
            key=bare_id,
            speaker=speaker,
            party="/".join(motion.get("parties") or []),
            date=str(motion.get("date") or ""),
            text=text[:3000],
            snippet=(motion.get("title") or "") + " — " + text[:300],
            metadata={
                "kind": "motion",
                "title": motion.get("title"),
                "session_label": motion.get("session_label"),
                "committee": motion.get("committee"),
                "url_video": motion.get("url_html"),
            },
        )
    ]))

    result: dict[str, Any] = {
        "doc_id": motion.get("doc_id"),
        "title": motion.get("title"),
        "subtitle": motion.get("subtitle"),
        "session_label": motion.get("session_label"),
        "designation": motion.get("designation"),
        "subtype": motion.get("subtype"),
        "committee": motion.get("committee"),
        "status": motion.get("status"),
        "date": motion.get("date"),
        "authors": [dict(a) for a in author_rows],
        "yrkanden": yrkanden,
        "text": text,
        "url_pdf": motion.get("url_pdf"),
        "url": motion.get("url_html"),
    }
    if not motion.get("has_text"):
        result["note"] = (
            "Motionen finns endast som inskannad PDF — fulltext saknas i databasen. "
            "Metadata och eventuella yrkanden ovan är kompletta."
        )
    if truncated:
        result["text_note"] = "Texten är trunkerad."
    return result


@register_tool(description=tool_doc("share_insight"))
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

    The description the model reads lives in prompts/<lang>/tools/share_insight.md;
    parameter docs below still come from this docstring.

    Args:
        message (str): Brief observation in Swedish (1–3 sentences). Be specific and concrete.
            Use this to put other insights in context. It appears as the header of the card
            and should explain what the attached data shows. If you refer to specific persons
            in this message, make sure to include their person_id in speaker_ids so their
            portraits can be highlighted visually.
        hit_ids (list[str]): Optional. Talk IDs to surface as a search card (backend fetches
            metadata). Pass the talk IDs (e.g. ["H40911", "H40912"]) you saw in a previous
            search_speeches or vector_search result. The backend fetches speaker/party/date/
            summary for each ID automatically — you do NOT need to copy the data yourself.
        sql (str): Optional. SQL query to re-execute for a stats card (preferred for surfacing
            stats tables). Re-pass the same SQL query you used in database_query (or a simplified
            variant). The backend re-executes it and builds the rows — you do NOT write rows=[]
            by hand.
        speaker_ids (list[str]): Optional. person_id values for portrait highlights (to show
            speaker portrait photos). List of person_id values for speakers you want to
            highlight visually. Always pair with speaker_ids_context to explain why they matter.
        speaker_ids_context (str): Optional. Caption for the speaker highlights (1–2 sentences
            explaining why these specific speakers are notable in this context).

    Returns:
        dict: Consumed by ChatService to emit a search_card, stats_card, or insight SSE event.
    """
    # Resolve hit_ids → hits by querying the speeches table (documents as fallback)
    if hit_ids and not hits:
        # Normalize: strip "speeches/"/"documents/" prefix if present
        bare_ids = [i.split("/", 1)[1] if "/" in i else i for i in hit_ids]
        try:
            talk_rows = pg.execute(
                """
                SELECT id, speaker_name, party, date::text AS date, person_id, summary
                FROM speeches
                WHERE id = ANY(%s::text[])
                """,
                (bare_ids,),
            )
            hits = [
                {
                    "_id": f"speeches/{r['id']}",
                    "speaker": r.get("speaker_name"),
                    "party": r.get("party"),
                    "date": r.get("date"),
                    "snippet": (r.get("summary") or "")[:300],
                    "person_id": r.get("person_id"),
                }
                for r in talk_rows
            ]
            found = {r["id"] for r in talk_rows}
            missing = [i for i in bare_ids if i not in found]
            if missing:
                motion_rows = pg.execute(
                    """
                    SELECT doc_id, title, parties, author_names, date::text AS date, text
                    FROM documents
                    WHERE doc_id = ANY(%s::text[])
                    """,
                    (missing,),
                )
                for r in motion_rows:
                    names = r.get("author_names") or []
                    speaker = ", ".join(names[:3]) + (" m.fl." if len(names) > 3 else "")
                    hits.append(
                        {
                            "_id": f"documents/{r['doc_id']}",
                            "speaker": speaker,
                            "party": "/".join(r.get("parties") or []),
                            "date": r.get("date"),
                            "snippet": (r.get("title") or "") or (r.get("text") or "")[:300],
                            "person_id": None,
                        }
                    )
        except Exception as e:
            print_red(f"[share_insight] Failed to fetch hit_ids: {e}")

    # Resolve sql → rows by re-executing the query
    if sql and not rows:
        # Replayed from a saved snapshot, so it is no more trusted than fresh output.
        refusal = _reject_unsafe_sql(sql)
        if refusal:
            print_red(f"[share_insight] {refusal}")
            return refusal
        try:
            rows = pg_llm.execute_readonly(sql)
        except Exception as e:
            print_red(f"[share_insight] Failed to execute sql: {e}")
            rows = [{"error": str(e)}]

    # Resolve [src:ID] tags in plain insight messages to debateurls for footnote links.
    src_sources: dict = {}
    src_ids = re.findall(r"\[src:([A-Za-z0-9_-]+)\]", message)
    if src_ids and not hits and not rows:
        try:
            src_rows = pg.execute(
                "SELECT id, url_video FROM speeches WHERE id = ANY(%s::text[])",
                (src_ids,),
            )
            src_sources = {r["id"]: r["url_video"] for r in src_rows if r.get("url_video")}
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


@register_tool(description=tool_doc("lookup_source"))
def lookup_source(source_ids: list[str]) -> str:
    """Återhämta lagrad grundtext för en eller flera tidigare registrerade källor.

    The description the model reads lives in prompts/<lang>/tools/lookup_source.md;
    parameter docs below still come from this docstring.

    Args:
        source_ids: Lista av bara tal-id:n (t.ex. ["H40911", "GH09100"]) eller
            "speeches/H40911"-format. ID:n som inte finns i registret utelämnas.

    Returns:
        Sträng med speaker_name, party, date, rubrik och lagrad text per id.
        Returnerar en kort meddelandetext om inga av id:na hittades.
    """
    registry = _provenance_registry.get()
    if registry is None:
        return "ERROR: ingen provenance-registry är active för denna session."
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
