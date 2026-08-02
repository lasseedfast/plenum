"""Board storage + the outer research loop primitives.

A board is one research topic; its threads are rows in ``research_threads``
(never a board-level blob, so the background dig and user-seeded threads can't
overwrite each other). ``deepen_step`` runs ONE bounded trip on the shallowest
active thread (or an explicit one), merges with deterministic dedup, persists
immediately, and bumps the board ``revision`` so a polling client sees change.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import List, Optional

from postgres_client import pg

from backend.services import crypto_blob
from backend.services.llm_tools import (
    SearchHitsResult,
    _tool_structured_result,
)
from backend.services.research.models import (
    BoardSeeds,
    ScoutQueries,
    ThreadResearch,
    ThreadSeed,
)
from backend.services.research.trip import research_trip
from prompts_loader import load_prompt

log = logging.getLogger("riksdagen.research.board")

RESEARCH_MAX_THREADS = int(os.getenv("RESEARCH_MAX_THREADS", "5"))
RESEARCH_TARGET_DEPTH = int(os.getenv("RESEARCH_TARGET_DEPTH", "3"))
RESEARCH_SCOUT_ROUNDS = int(os.getenv("RESEARCH_SCOUT_ROUNDS", "3"))
RESEARCH_SCOUT_MATERIAL_CHARS = int(os.getenv("RESEARCH_SCOUT_MATERIAL_CHARS", "9000"))
RESEARCH_PROPOSAL_COUNT = int(os.getenv("RESEARCH_PROPOSAL_COUNT", "7"))
RESEARCH_FOLLOWUP_COUNT = int(os.getenv("RESEARCH_FOLLOWUP_COUNT", "4"))
RESEARCH_MAX_PROPOSED = int(os.getenv("RESEARCH_MAX_PROPOSED", "8"))

# Caps applied on merge so a board can't grow without bound.
_MAX_FINDINGS = 40
_MAX_QUESTIONS = 12
_MAX_LEADS = 10

_DISCOVER_SYSTEM = load_prompt("research/discover")


# ---------------------------------------------------------------------------
# Storage
#
# Encrypted boards: every function takes an optional ``key`` (the raw board
# key, present only in a spawn request or a job child's memory). With a key,
# content fields are decrypted after SELECT and encrypted before
# INSERT/UPDATE, so callers always work with plaintext dicts. Without a key,
# values pass through untouched — the anonymous/plaintext path and the poll
# routes (which serve ciphertext for the client to decrypt) share this code.
# ---------------------------------------------------------------------------

_THREAD_TEXT_FIELDS = ("title", "question", "why", "guidance", "answer")
_THREAD_JSON_FIELDS = ("findings", "open_questions", "leads", "hints")
_BOARD_TEXT_FIELDS = ("title", "topic", "intro", "report")


def _placeholder_title(topic: str) -> str:
    """Stand-in board title for the ~minute before the scout writes a real one.

    Topics are typically several sentences, and the board H1 is a large display
    face — so take the first sentence and cut on a word boundary rather than
    dumping 80 characters of prose into the heading.
    """
    first = re.split(r"(?<=[.!?])\s", topic.strip(), maxsplit=1)[0].strip()
    first = first or topic.strip()
    if len(first) <= 60:
        return first.rstrip(".") or "Ny research"
    return first[:60].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


def _llm_call_failed(res) -> bool:
    """True when LLM.generate hit an API error.

    It doesn't raise — it catches everything and returns the literal
    "Remote API failed. An error occurred." (see _llm/_llm/llm.py), which is
    otherwise indistinguishable from a model that just answered badly.
    """
    return isinstance(res, str) and res.startswith("Remote API failed")


def _dec_jsonb(value, key: Optional[bytes]):
    """JSONB content field: encrypted boards store the whole JSON value as one
    ciphertext string (a bare JSON string is valid jsonb)."""
    if key is not None and crypto_blob.is_encrypted(value):
        return json.loads(crypto_blob.decrypt_str(value, key))
    return value


def _enc_jsonb(value, key: Optional[bytes]) -> str:
    dumped = json.dumps(value, ensure_ascii=False, default=str)
    if key is not None:
        return json.dumps(crypto_blob.encrypt_str(dumped, key))
    return dumped


def _dec_thread_row(row: dict, key: Optional[bytes]) -> dict:
    if key is None:
        return row
    for f in _THREAD_TEXT_FIELDS:
        if f in row:
            row[f] = crypto_blob.dec(row[f], key)
    for f in _THREAD_JSON_FIELDS:
        if f in row:
            row[f] = _dec_jsonb(row[f], key)
    return row


def _dec_board_row(row: dict, key: Optional[bytes]) -> dict:
    if key is None:
        return row
    for f in _BOARD_TEXT_FIELDS:
        if f in row:
            row[f] = crypto_blob.dec(row[f], key)
    return row


def create_board(topic: str, title: Optional[str] = None,
                 target_depth: int = RESEARCH_TARGET_DEPTH,
                 owner_session: Optional[str] = None,
                 user_id: Optional[str] = None,
                 wrapped_board_key: Optional[str] = None,
                 key: Optional[bytes] = None) -> dict:
    topic = " ".join((topic or "").split()).strip()
    title = (title or "").strip() or _placeholder_title(topic)
    rows = pg.execute(
        """
        INSERT INTO research_boards
            (title, topic, target_depth, owner_session, user_id, enc, wrapped_board_key)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id::text AS id, status, revision, target_depth,
                  created_at::text AS created_at
        """,
        (
            crypto_blob.enc(title, key),
            crypto_blob.enc(topic, key),
            target_depth,
            owner_session,
            user_id,
            key is not None,
            wrapped_board_key,
        ),
    )
    return {**dict(rows[0]), "title": title, "topic": topic}


def board_access(board_id: str) -> Optional[dict]:
    """{"owner_session", "user_id"} for a board, or None if it doesn't exist."""
    rows = pg.execute(
        "SELECT owner_session, user_id::text AS user_id FROM research_boards WHERE id = %s",
        (board_id,),
    )
    return dict(rows[0]) if rows else None


def get_board(board_id: str, key: Optional[bytes] = None) -> Optional[dict]:
    rows = pg.execute(
        """
        SELECT id::text AS id, title, topic, intro, status, revision, target_depth,
               logic_version, enc, wrapped_board_key,
               report, report_generated_at::text AS report_generated_at,
               created_at::text AS created_at, updated_at::text AS updated_at
        FROM research_boards WHERE id = %s
        """,
        (board_id,),
    )
    return _dec_board_row(dict(rows[0]), key) if rows else None


def list_boards(owner_session: Optional[str] = None,
                user_id: Optional[str] = None) -> List[dict]:
    """Boards for one browser and/or one account. With neither, returns
    nothing — the list is always scoped so no one sees another's research.
    Encrypted boards come back with ciphertext titles + the wrapped board key;
    the client decrypts."""
    if not owner_session and not user_id:
        return []
    rows = pg.execute(
        """
        SELECT b.id::text AS id, b.title, b.topic, b.status, b.revision,
               b.enc, b.wrapped_board_key,
               b.updated_at::text AS updated_at, b.created_at::text AS created_at,
               COUNT(t.id) AS thread_count
        FROM research_boards b
        LEFT JOIN research_threads t ON t.board_id = b.id AND t.status != 'archived'
        WHERE (%s::text IS NOT NULL AND b.owner_session = %s)
           OR (%s::uuid IS NOT NULL AND b.user_id = %s::uuid)
        GROUP BY b.id
        ORDER BY b.updated_at DESC
        """,
        (owner_session, owner_session, user_id, user_id),
    )
    return [dict(r) for r in rows]


def get_threads(board_id: str, key: Optional[bytes] = None) -> List[dict]:
    rows = pg.execute(
        """
        SELECT id::text AS id, title, question, why, origin, depth, status, pinned,
               findings, open_questions, leads, guidance, answer, answer_depth, hints,
               created_at::text AS created_at, updated_at::text AS updated_at
        FROM research_threads
        WHERE board_id = %s AND status != 'archived'
        ORDER BY pinned DESC, (origin = 'seed') DESC, created_at
        """,
        (board_id,),
    )
    return [_dec_thread_row(dict(r), key) for r in rows]


def set_board_status(board_id: str, status: str, intro: Optional[str] = None,
                     key: Optional[bytes] = None, title: Optional[str] = None) -> None:
    """Advance a board's status, optionally writing the intro and/or title the
    discovery pass produced. Both are board content, so both go through
    crypto_blob.enc — a None leaves the stored value untouched."""
    sets = ["status = %s"]
    args: list = [status]
    if intro is not None:
        sets.append("intro = %s")
        args.append(crypto_blob.enc(intro, key))
    if title is not None:
        sets.append("title = %s")
        args.append(crypto_blob.enc(title, key))
    args.append(board_id)
    pg.execute_void(
        f"""
        UPDATE research_boards
        SET {', '.join(sets)}, revision = revision + 1, updated_at = NOW()
        WHERE id = %s
        """,
        tuple(args),
    )


def delete_board(board_id: str) -> bool:
    rows = pg.execute(
        "DELETE FROM research_boards WHERE id = %s RETURNING id", (board_id,)
    )
    return bool(rows)


def insert_thread(board_id: str, *, title: str, question: str, why: str = "",
                  origin: str = "auto", pinned: bool = False,
                  status: str = "active", guidance: Optional[str] = None,
                  hints: Optional[List[str]] = None,
                  key: Optional[bytes] = None) -> dict:
    rows = pg.execute(
        """
        INSERT INTO research_threads
            (board_id, title, question, why, origin, pinned, status, guidance, hints)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        RETURNING id::text AS id, origin, depth, status, pinned,
                  findings, open_questions, leads, created_at::text AS created_at,
                  updated_at::text AS updated_at
        """,
        (
            board_id,
            crypto_blob.enc(title, key),
            crypto_blob.enc(question, key),
            crypto_blob.enc(why, key) if why else why,
            origin,
            pinned,
            status,
            crypto_blob.enc(guidance, key) if guidance else guidance,
            _enc_jsonb(list(hints or []), key),
        ),
    )
    pg.execute_void(
        "UPDATE research_boards SET revision = revision + 1, updated_at = NOW() WHERE id = %s",
        (board_id,),
    )
    # Callers (handlers' progress events, the seed route's response) work with
    # the plaintext they passed in, regardless of what hit the disk.
    return {**dict(rows[0]), "title": title, "question": question, "why": why}


def insert_seed_thread(board_id: str, text: str, key: Optional[bytes] = None) -> dict:
    """User-seeded thread: plain INSERT (no LLM), pinned on top, depth 0.

    A running dig naturally picks it up next (shallowest-first)."""
    question = " ".join((text or "").split()).strip()
    title = question if len(question) <= 70 else question[:70].rsplit(" ", 1)[0] + "…"
    return insert_thread(
        board_id, title=title or "Egen tråd", question=question,
        origin="seed", pinned=True, key=key,
    )


def activate_thread(thread_id: str, board_id: str,
                    guidance: Optional[str] = None,
                    key: Optional[bytes] = None) -> bool:
    """Approve a proposed thread (optionally with user guidance for its trips).
    Returns False if the thread wasn't a pending proposal on this board.
    Caller bumps the board revision once per batch."""
    rows = pg.execute(
        """
        UPDATE research_threads
        SET status = 'active', guidance = %s, updated_at = NOW()
        WHERE id = %s AND board_id = %s AND status = 'proposed'
        RETURNING id
        """,
        (crypto_blob.enc(guidance, key) if guidance else None, thread_id, board_id),
    )
    return bool(rows)


def archive_thread(thread_id: str, board_id: str) -> bool:
    """Dismiss a thread (status-only write — works without the board key)."""
    rows = pg.execute(
        """
        UPDATE research_threads
        SET status = 'archived', updated_at = NOW()
        WHERE id = %s AND board_id = %s AND status != 'archived'
        RETURNING id
        """,
        (thread_id, board_id),
    )
    if rows:
        bump_revision(board_id)
    return bool(rows)


def bump_revision(board_id: str) -> None:
    pg.execute_void(
        "UPDATE research_boards SET revision = revision + 1, updated_at = NOW() WHERE id = %s",
        (board_id,),
    )


def count_proposed(board_id: str) -> int:
    rows = pg.execute(
        "SELECT COUNT(*) AS n FROM research_threads WHERE board_id = %s AND status = 'proposed'",
        (board_id,),
    )
    return int(rows[0]["n"]) if rows else 0


def set_thread_answer(thread_id: str, board_id: str, answer: str, depth: int,
                      key: Optional[bytes] = None) -> None:
    pg.execute_void(
        """
        UPDATE research_threads
        SET answer = %s, answer_depth = %s, updated_at = NOW()
        WHERE id = %s
        """,
        (crypto_blob.enc(answer, key), depth, thread_id),
    )
    bump_revision(board_id)


def set_board_report(board_id: str, report: str, key: Optional[bytes] = None) -> None:
    pg.execute_void(
        """
        UPDATE research_boards
        SET report = %s, report_generated_at = NOW(),
            revision = revision + 1, updated_at = NOW()
        WHERE id = %s
        """,
        (crypto_blob.enc(report, key), board_id),
    )


# ---------------------------------------------------------------------------
# Deterministic dedup + merge  (guide §7: never ask the LLM not to repeat)
# ---------------------------------------------------------------------------

_norm_re = re.compile(r"[^\wåäöÅÄÖ]+", re.UNICODE)


def _norm(text: str) -> str:
    return _norm_re.sub(" ", (text or "").lower()).strip()


def merge_research(thread: dict, res: ThreadResearch) -> dict:
    """Merge a trip's result into a thread row's JSONB fields, dedup + caps.

    Returns {"findings": [...], "open_questions": [...], "leads": [...],
    "added": n} ready to persist."""
    findings = list(thread.get("findings") or [])
    known = {_norm(f.get("label", "")) for f in findings}
    added = 0
    for f in res.findings:
        key = _norm(f.label)
        if not key or key in known:
            continue
        known.add(key)
        findings.append(f.model_dump())
        added += 1
    findings = findings[:_MAX_FINDINGS]

    questions = list(thread.get("open_questions") or [])
    q_known = {_norm(q) for q in questions}
    for q in res.open_questions:
        key = _norm(q)
        if key and key not in q_known:
            q_known.add(key)
            questions.append(q.strip())
    questions = questions[:_MAX_QUESTIONS]

    # Leads are replaced rather than accumulated: old leads either got followed
    # (this trip) or superseded by fresher ones; dedup by (kind, target).
    leads: List[dict] = []
    l_known = set()
    for l in res.leads:
        key = (l.kind, _norm(l.target))
        if key in l_known:
            continue
        l_known.add(key)
        leads.append(l.model_dump())
    if not leads:
        leads = list(thread.get("leads") or [])
    leads = leads[:_MAX_LEADS]

    return {
        "findings": findings,
        "open_questions": questions,
        "leads": leads,
        "added": added,
    }


# ---------------------------------------------------------------------------
# Discovery — topic-seeded threads, grounded in real search results
# ---------------------------------------------------------------------------


def _search_material(query: str, seen_ids: set, *,
                     debates_limit: int = 4, talks_limit: int = 14) -> str:
    """Direct tool calls (no agent loop) that ground discovery in material
    that actually exists in the corpus. Hits whose ids are already in
    ``seen_ids`` are skipped (and new ids added), so repeated calls across
    scout rounds accumulate without duplication.

    Speeches come first and are formatted party-first (``[parti] Talare: …``)
    so the salient structure the LLM sees is *who said what*, not *when* — this
    is what steers discovery toward party/issue threads instead of comparing
    individual debates by date. ``debates_limit=0`` skips the debate summaries
    (they carry no party attribution)."""
    from backend.services.llm_tools import arango_search, vector_search_debates

    parts: List[str] = []
    # Speeches (anföranden): named speaker + party — the substance for positions.
    try:
        _tool_structured_result.set(None)
        arango_search(query=query, return_snippets=True, limit=talks_limit)
        structured = _tool_structured_result.get()
        if isinstance(structured, SearchHitsResult) and structured.response.hits:
            lines = ["ANFÖRANDEN (parti — talare: utdrag [id]):"]
            for h in structured.response.hits:
                if h.key in seen_ids:
                    continue
                seen_ids.add(h.key)
                snip = (h.snippet or h.text or "").replace("\n", " ")[:260]
                party = h.party or "okänt parti"
                who = h.speaker or "Okänd talare"
                dt = f", {h.date}" if h.date else ""
                lines.append(f"- [{party}] {who}: {snip} [{h.key}{dt}]")
            if len(lines) > 1:
                parts.append("\n".join(lines))
    except Exception:
        log.exception("discovery: arango_search failed")
    # Debate summaries: topical context only (no party attribution).
    if debates_limit > 0:
        try:
            _tool_structured_result.set(None)
            vector_search_debates(query, limit=debates_limit)
            structured = _tool_structured_result.get()
            if structured is not None and getattr(structured, "hits", None):
                lines = ["DEBATTER (sammanfattning [id]):"]
                for h in structured.hits:
                    if h.key in seen_ids:
                        continue
                    seen_ids.add(h.key)
                    lines.append(f"- {(h.snippet or '')[:260]} [{h.key}]")
                if len(lines) > 1:
                    parts.append("\n".join(lines))
        except Exception:
            log.exception("discovery: vector_search_debates failed")
    return "\n\n".join(parts)


def _grounding_material(topic: str) -> str:
    return _search_material(topic, set())[:6000]


# The eight Riksdag parties, used to seed party-scoped scout queries so a
# "party positions" topic gets grounding material from every party's benches.
_RIKSDAG_PARTIES = [
    "Socialdemokraterna", "Moderaterna", "Sverigedemokraterna", "Centerpartiet",
    "Vänsterpartiet", "Kristdemokraterna", "Liberalerna", "Miljöpartiet",
]

_SCOUT_QUERY_SYSTEM = load_prompt("research/scout_query")


def scout_material(fast_llm, topic: str, rounds: int = RESEARCH_SCOUT_ROUNDS,
                   on_event=None, is_cancelled=None) -> str:
    """Multi-round grounding for the scout phase. Round 1 searches the raw
    topic; each later round asks the fast model for 2-4 new angles and searches
    those. Failed LLM calls skip the round rather than abort the scout."""
    seen_ids: set = set()
    searched: List[str] = [topic]
    if on_event:
        on_event(topic)
    material = _search_material(topic, seen_ids)

    # Deterministic party-scoped pass: guarantees each party's benches are
    # searched even if the model never thinks to, which is what a "party
    # positions" topic needs. Speeches only (debates carry no party), bounded
    # by the material cap so a thin corpus stops it early.
    for party in _RIKSDAG_PARTIES:
        if is_cancelled and is_cancelled():
            break
        if len(material) >= RESEARCH_SCOUT_MATERIAL_CHARS:
            break
        q = f"{party} {topic}"
        searched.append(q)
        if on_event:
            on_event(q)
        extra = _search_material(q, seen_ids, talks_limit=6, debates_limit=0)
        if extra:
            material = f"{material}\n\n{extra}" if material else extra

    for _ in range(max(0, rounds - 1)):
        if is_cancelled and is_cancelled():
            break
        if len(material) >= RESEARCH_SCOUT_MATERIAL_CHARS:
            break
        prompt = (
            f"ÄMNE: {topic}\n\n"
            f"REDAN SÖKT: {'; '.join(searched)}\n\n"
            f"MATERIAL HITTILLS:\n{material[:5000]}\n\n"
            "Föreslå 2-4 NYA sökfrågor som täcker andra vinklar på ämnet."
        )
        try:
            res = fast_llm.generate(
                messages=[
                    {"role": "system", "content": _SCOUT_QUERY_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                format=ScoutQueries,
                think=False,
                max_tokens=300,
            )
        except Exception:
            log.exception("scout: query-proposal LLM call failed")
            continue
        parsed = getattr(res, "parsed", None) if not isinstance(res, str) else None
        queries = [q.strip() for q in (parsed.queries if isinstance(parsed, ScoutQueries) else []) if q.strip()]
        known = {_norm(s) for s in searched}
        queries = [q for q in queries if _norm(q) not in known][:4]
        if not queries:
            continue
        for q in queries:
            if is_cancelled and is_cancelled():
                break
            searched.append(q)
            if on_event:
                on_event(q)
            extra = _search_material(q, seen_ids)
            if extra:
                material = f"{material}\n\n{extra}" if material else extra
            if len(material) >= RESEARCH_SCOUT_MATERIAL_CHARS:
                break
    return material[:RESEARCH_SCOUT_MATERIAL_CHARS]


def discover_threads(llm, board: dict, max_threads: int = RESEARCH_MAX_THREADS,
                     material: Optional[str] = None) -> BoardSeeds:
    """Propose up to ``max_threads`` open threads about the board topic,
    grounded in real search material (a cheap single pass by default; the
    scout job passes in richer multi-round material). Falls back to a single
    thread made from the raw topic if the LLM proposes nothing usable."""
    topic = board["topic"]
    if material is None:
        material = _grounding_material(topic)
    prompt_lines = [f"ÄMNE ATT UTFORSKA: {topic}", ""]
    if material:
        prompt_lines += ["UNDERLAG FRÅN DATABASEN:", material, ""]
    prompt_lines.append(
        "Ge först en title: en kort BESKRIVANDE rubrik för hela utforskningen på "
        "3-8 ord (högst 60 tecken), utan avslutande punkt. Den visas som sidans "
        "rubrik i stället för användarens råa ämnestext. Använd vanliga svenska ord "
        "som beskriver ämnet — hitta inte på nya sammansatta ord, och skriv ingen "
        "slagordsrubrik.\n\n"
        f"Föreslå sedan upp till {max_threads} ÖPPNA trådar att gräva i kring ämnet. "
        "Användaren väljer själv vilka som ska grävas, så gör dem varierade och utan "
        "överlapp — olika partier och delfrågor. För varje:\n"
        "- title: kort rubrik\n"
        "- question: den öppna fråga reportern ska utforska (inget facit), formulerad "
        "så att svaret blir konkreta ståndpunkter belagda med citat\n"
        "- why: varför tråden är intressant och vad den kan visa\n"
        "- hints: 2-5 konkreta sökord, partinamn eller personnamn ur underlaget ovan\n"
        "Ge också en kort intro (en mening om vad materialet visar om ämnet — inte om "
        "materialets omfattning). Svara som JSON enligt schemat."
    )
    res = llm.generate(
        messages=[
            {"role": "system", "content": _DISCOVER_SYSTEM},
            {"role": "user", "content": "\n".join(prompt_lines)},
        ],
        format=BoardSeeds,
        think=False,
        max_tokens=max(1400, 250 * max_threads + 200),
    )
    # A dead provider (bad key, unknown model, network) must surface as a
    # failure. Falling back here would hand the user a board that looks
    # finished but only echoes their own topic back — and now that users can
    # bring their own key, a typo makes that the common case. An unparseable
    # *response* is different: the model is alive, just unhelpful, and still
    # degrades to the fallback thread below.
    if _llm_call_failed(res):
        raise RuntimeError(
            "AI-modellen svarade inte — kontrollera modellval och API-nyckel "
            "under AI-inställningar."
        )
    seeds = getattr(res, "parsed", None) if not isinstance(res, str) else None
    if not isinstance(seeds, BoardSeeds) or not seeds.threads:
        log.warning("discovery: falling back to single thread from raw topic")
        seeds = BoardSeeds(
            title="",  # keeps the placeholder title the board was created with
            intro="",
            threads=[ThreadSeed(title=topic[:70], question=topic, why="Användarens ämne.")],
        )
    seeds.threads = seeds.threads[:max_threads]
    return seeds


_FOLLOWUP_SYSTEM = load_prompt("research/followup")


def propose_followups(fast_llm, board: dict, threads: List[dict],
                      key: Optional[bytes] = None) -> List[ThreadSeed]:
    """After a dig round: turn accumulated open questions + leads into new
    *proposed* threads for the user to approve. Deterministic dedup against
    every existing thread (including archived, so dismissed proposals don't
    come back). Returns [] when enough proposals are already pending."""
    pending = sum(1 for t in threads if t.get("status") == "proposed")
    if pending >= RESEARCH_MAX_PROPOSED:
        return []
    active = [t for t in threads if t.get("status") == "active"]
    if not active:
        return []

    lines: List[str] = [f"ÄMNE: {board.get('topic') or ''}"]
    if board.get("intro"):
        lines.append(f"INTRO: {board['intro']}")
    lines.append("")
    for t in active:
        lines.append(f"TRÅD: {t.get('title') or ''}")
        for q in (t.get("open_questions") or [])[:6]:
            lines.append(f"  Obesvarad fråga: {q}")
        for l in (t.get("leads") or [])[:6]:
            lead_txt = l.get("lead") or l.get("target") or ""
            lines.append(f"  Spår ({l.get('kind')}): {lead_txt}")
        lines.append("")

    # Every thread ever created on the board (any status) blocks re-proposals.
    rows = pg.execute(
        "SELECT title, question FROM research_threads WHERE board_id = %s",
        (board["id"],),
    )
    existing_norms = set()
    existing_titles: List[str] = []
    for r in rows:
        title = crypto_blob.dec(r["title"], key) or ""
        question = crypto_blob.dec(r["question"], key) or ""
        existing_norms.update({_norm(title), _norm(question)})
        if title:
            existing_titles.append(title)
    existing_norms.discard("")

    lines.append("TRÅDAR SOM REDAN FINNS (föreslå INTE dessa igen):")
    lines += [f"- {t}" for t in existing_titles[:30]]
    lines.append("")
    lines.append(
        f"Föreslå upp till {RESEARCH_FOLLOWUP_COUNT} helt NYA trådar utifrån frågorna "
        "och spåren ovan. För varje: title, question, why samt hints "
        "(2-5 konkreta sökord, personnamn eller debatt-id ur underlaget)."
    )
    try:
        res = fast_llm.generate(
            messages=[
                {"role": "system", "content": _FOLLOWUP_SYSTEM},
                {"role": "user", "content": "\n".join(lines)},
            ],
            format=BoardSeeds,
            think=False,
            max_tokens=250 * RESEARCH_FOLLOWUP_COUNT + 200,
        )
    except Exception:
        log.exception("followups: LLM call failed")
        return []
    parsed = getattr(res, "parsed", None) if not isinstance(res, str) else None
    if not isinstance(parsed, BoardSeeds):
        return []
    out: List[ThreadSeed] = []
    for seed in parsed.threads:
        if _norm(seed.title) in existing_norms or _norm(seed.question) in existing_norms:
            continue
        existing_norms.update({_norm(seed.title), _norm(seed.question)})
        out.append(seed)
    return out[: max(0, min(RESEARCH_FOLLOWUP_COUNT, RESEARCH_MAX_PROPOSED - pending))]


# ---------------------------------------------------------------------------
# Deepen — ONE trip, merged and saved immediately
# ---------------------------------------------------------------------------


def pick_next_thread(board_id: str, target_depth: int,
                     key: Optional[bytes] = None) -> Optional[dict]:
    """Shallowest active thread under the depth ceiling (greedy breadth-leveling)."""
    rows = pg.execute(
        """
        SELECT id::text AS id, title, question, why, origin, depth, findings,
               open_questions, leads, guidance, answer_depth, hints
        FROM research_threads
        WHERE board_id = %s AND status = 'active' AND depth < %s
        ORDER BY depth, created_at LIMIT 1
        """,
        (board_id, target_depth),
    )
    return _dec_thread_row(dict(rows[0]), key) if rows else None


def get_thread(thread_id: str, key: Optional[bytes] = None) -> Optional[dict]:
    rows = pg.execute(
        """
        SELECT id::text AS id, board_id::text AS board_id, title, question, why,
               origin, depth, status, findings, open_questions, leads,
               guidance, answer, answer_depth, hints
        FROM research_threads WHERE id = %s
        """,
        (thread_id,),
    )
    return _dec_thread_row(dict(rows[0]), key) if rows else None


def deepen_step(
    smart_llm,
    fast_llm,
    board_id: str,
    *,
    thread_id: Optional[str] = None,
    lead: Optional[dict] = None,
    hints: Optional[List[str]] = None,
    on_event=None,
    key: Optional[bytes] = None,
) -> bool:
    """Run ONE research trip and persist the merged result.

    Target = explicit ``thread_id``, else the shallowest active thread under
    the board's target depth. Returns False when there is nothing to do.
    """
    board = get_board(board_id, key=key)
    if board is None:
        return False
    if thread_id:
        thread = get_thread(thread_id, key=key)
    else:
        thread = pick_next_thread(board_id, int(board["target_depth"]), key=key)
    if thread is None:
        return False

    known_labels = [f.get("label", "") for f in (thread.get("findings") or [])]
    trip_hints = list(hints or [])
    question = thread.get("question") or ""
    if thread.get("guidance"):
        question = f"{question}\nAnvändarens medskick: {thread['guidance']}"
    if lead:
        kind = lead.get("kind")
        target = lead.get("target") or ""
        if kind == "search" and target:
            question = f"{question}\nFölj spåret: sök '{target}'. {lead.get('lead') or ''}"
        elif target:
            trip_hints.append(target)
            if lead.get("lead"):
                question = f"{question}\nFölj spåret: {lead['lead']}"
    if not trip_hints:
        # Seed the trip from the thread's stored leads (the previous trip's
        # direction proposals) — this is what makes deepening feel like digging.
        trip_hints = [
            l.get("target") for l in (thread.get("leads") or []) if l.get("target")
        ][:6]
    if not trip_hints and int(thread.get("depth") or 0) == 0:
        # Fresh thread with no trips yet: fall back to the discovery hints
        # persisted on the row (proposals are dug in a later job than the one
        # that created them, so hints can't ride along in-process).
        trip_hints = [h for h in (thread.get("hints") or []) if h][:6]

    res = research_trip(
        smart_llm,
        fast_llm,
        title=thread.get("title") or "Tråd",
        question=question,
        hints=trip_hints,
        known_labels=known_labels,
        on_event=on_event,
    )
    merged = merge_research(thread, res)
    pg.execute_void(
        """
        UPDATE research_threads
        SET findings = %s::jsonb, open_questions = %s::jsonb, leads = %s::jsonb,
            depth = depth + 1, updated_at = NOW()
        WHERE id = %s
        """,
        (
            _enc_jsonb(merged["findings"], key),
            _enc_jsonb(merged["open_questions"], key),
            _enc_jsonb(merged["leads"], key),
            thread["id"],
        ),
    )
    pg.execute_void(
        "UPDATE research_boards SET revision = revision + 1, updated_at = NOW() WHERE id = %s",
        (board_id,),
    )
    log.info(
        "deepen: thread %s +%d findings (depth %d -> %d)",
        thread["id"], merged["added"], thread["depth"], thread["depth"] + 1,
    )
    return True
