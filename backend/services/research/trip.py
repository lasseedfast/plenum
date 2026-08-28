"""One bounded research trip: a short tool loop + forced structured synthesis.

A trip investigates ONE thread question with the existing chat tool registry,
keeps every tool result hard-truncated (bounded context regardless of corpus
size), and distils what it found into a `ThreadResearch` (findings with
verbatim quotes + open questions + leads). It deliberately does NOT conclude —
the board accumulates material; reading it is the user's job.

Grounding is deterministic, not self-policed: every talk id, speaker, party
and date seen in tool results is collected into a *seen-map*, findings whose
`source_id` was never seen are dropped, and surviving findings are enriched
with speaker/party/date from the map (never from the model).
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable

from backend.services.llm_tools import (
    HitsResponse,
    SearchHitsResult,
    _fast_llm_var,
    _tool_structured_result,
)
from backend.services.provenance import normalize_talk_id
from backend.services.research.models import ResearchLead, ThreadResearch
from packages.llm import get_tools
from packages.llm.tools import TOOL_REGISTRY
from prompts_loader import load_prompt

log = logging.getLogger("riksdagen.research.trip")

# Tools a trip may use. share_insight/lookup_source are chat-turn plumbing;
# fetch_speeches dumps raw text — trips use read_documents_for instead.
# The motion tools are included because the research prompts promise motions as a
# source: without them a trip cannot reach a single one. Motion hits carry
# metadata kind="motion" and a bare doc_id key, so _collect_seen and _ground
# accept them exactly like speech hits.
RESEARCH_TOOLS = [
    "search_speeches",
    "vector_search",
    "vector_search_debates",
    "fetch_debate",
    "database_query",
    "read_documents_for",
    "search_documents",
    "vector_search_documents",
    "fetch_document",
]

RESEARCH_TOOL_RESULT_CHARS = int(os.getenv("RESEARCH_TOOL_RESULT_CHARS", "4000"))
RESEARCH_TRIP_MAX_TURNS = int(os.getenv("RESEARCH_TRIP_MAX_TURNS", "6"))
_FINAL_MAX_TOKENS = 1600

_TRIP_SYSTEM = load_prompt("research/trip")

_FINAL_INSTRUCTION = load_prompt("research/trip_final")


def _compact_result_string(structured, raw_result) -> str:
    """Prefer the structured hits' readable text; fall back to the raw return."""
    if isinstance(structured, SearchHitsResult):
        text = structured.response.to_string()
    elif isinstance(structured, HitsResponse):
        text = structured.to_string()
    elif isinstance(raw_result, str):
        text = raw_result
    else:
        text = json.dumps(raw_result, ensure_ascii=False, default=str)
    if len(text) > RESEARCH_TOOL_RESULT_CHARS:
        text = text[:RESEARCH_TOOL_RESULT_CHARS] + " (...)[truncated]"
    return text


def _collect_seen(structured, seen_talks: dict[str, dict], seen_debates: dict[str, str],
                  seen_persons: dict[str, str]) -> None:
    """Harvest ids + attribution from a structured tool result into the seen-maps."""
    hits = []
    if isinstance(structured, SearchHitsResult):
        hits = structured.response.hits
    elif isinstance(structured, HitsResponse):
        hits = structured.hits
    for h in hits:
        meta = h.metadata or {}
        if meta.get("kind") == "debate":
            if h.key:
                seen_debates[h.key] = (h.snippet or "")[:80]
            continue
        bare = normalize_talk_id(h.key or h.id)
        if bare:
            entry = seen_talks.setdefault(bare, {})
            if h.speaker:
                entry.setdefault("speaker", h.speaker)
            if h.party:
                entry.setdefault("party", h.party)
            if h.date:
                entry.setdefault("date", str(h.date))
        iid = meta.get("person_id")
        if iid and h.speaker:
            seen_persons[str(iid)] = h.speaker


def _ground(res: ThreadResearch, seen_talks: dict[str, dict],
            seen_debates: dict[str, str], seen_persons: dict[str, str]) -> ThreadResearch:
    """Deterministic backstop: drop unseen sources/targets, enrich the rest."""
    findings = []
    for f in res.findings:
        bare = normalize_talk_id((f.source_id or "").strip())
        if not bare or bare not in seen_talks:
            log.info("trip: dropped finding with unseen source_id=%r (%s)", f.source_id, f.label[:60])
            continue
        info = seen_talks[bare]
        f.source_id = bare
        f.speaker = info.get("speaker")
        f.party = info.get("party")
        f.date = info.get("date")
        findings.append(f)
    leads: list[ResearchLead] = []
    for lead in res.leads:
        target = (lead.target or "").strip()
        if not target:
            continue
        if lead.kind == "person":
            if target not in seen_persons:
                log.info("trip: dropped person lead with unseen target=%r", target)
                continue
            lead.label = seen_persons[target]
        elif lead.kind == "debate":
            if target not in seen_debates:
                log.info("trip: dropped debate lead with unseen target=%r", target)
                continue
            lead.label = seen_debates.get(target)
        leads.append(lead)
    return ThreadResearch(findings=findings, open_questions=res.open_questions, leads=leads)


def research_trip(
    smart_llm,
    fast_llm=None,
    *,
    title: str,
    question: str,
    hints: list[str] | None = None,
    known_labels: list[str] | None = None,
    max_turns: int = RESEARCH_TRIP_MAX_TURNS,
    on_event: Callable[[dict], None] | None = None,
) -> ThreadResearch:
    """Run one bounded research trip and return distilled, grounded notes.

    ``on_event`` is a fire-and-forget callback for live progress:
    ``{"phase": "tool", "name": ..., "args": ...}`` per tool turn and
    ``{"phase": "finding", "label": ..., "detail": ...}`` per distilled finding.
    Callback errors never escape.
    """

    def _emit(ev: dict) -> None:
        if on_event:
            try:
                on_event(ev)
            except Exception:
                log.debug("research on_event callback failed", exc_info=True)

    if fast_llm is not None:
        # read_documents_for picks this up via ContextVar.
        _fast_llm_var.set(fast_llm)

    lines = [f"TRÅD: {title}", f"FRÅGA ATT UTFORSKA: {question}"]
    if hints:
        lines.append("Utgå gärna från: " + ", ".join(str(h) for h in hints[:8]))
    if known_labels:
        lines.append(
            "Du har redan hittat dessa bitar — leta efter NYTT, inte upprepningar:\n"
            + "\n".join(f"- {label}" for label in known_labels[:12])
        )
    lines.append(
        "Gräv nu med verktygen. Börja brett (sök) och gå sedan på djupet med "
        "fetch_debate/read_documents_for. Samla uppslag — dra inga slutsatser."
    )
    messages: list[dict] = [
        {"role": "system", "content": _TRIP_SYSTEM},
        {"role": "user", "content": "\n".join(lines)},
    ]
    schemas = get_tools(specific_tools=RESEARCH_TOOLS)

    seen_talks: dict[str, dict] = {}
    seen_debates: dict[str, str] = {}
    seen_persons: dict[str, str] = {}
    executed: dict[tuple, str] = {}

    for turn in range(max_turns):
        try:
            response = smart_llm.generate(
                messages=list(messages),
                tools=schemas,
                think=(turn == 0),
                auto_execute_tools=False,
            )
        except Exception:
            log.exception("trip: generate failed (turn %d)", turn)
            break
        if isinstance(response, str):
            # _llm swallows API errors and returns a plain string.
            log.warning("trip: LLM error on turn %d: %s", turn, response[:200])
            break
        tool_calls = getattr(response, "tool_calls", None)
        if not tool_calls:
            if response.content:
                messages.append({"role": "assistant", "content": response.content})
            break

        messages.append(
            {
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": (
                                json.dumps(tc.function.arguments)
                                if isinstance(tc.function.arguments, dict)
                                else tc.function.arguments
                            ),
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

        for tc in tool_calls:
            name = tc.function.name
            args = tc.function.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if not isinstance(args, dict):
                args = {}
            args.pop("focus_ids", None)  # chat-turn concept, not used in trips
            _emit({"phase": "tool", "name": name, "args": args})

            key = (name, json.dumps(args, sort_keys=True, default=str))
            if key in executed:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": name,
                        "content": (
                            "You already made this exact call. Cached result:\n"
                            + executed[key][:1200]
                            + "\n\nDo not repeat identical calls — vary the query or use another tool."
                        ),
                    }
                )
                continue

            entry = TOOL_REGISTRY.get(name)
            if entry is None or name not in RESEARCH_TOOLS:
                result_string = f"ERROR: Tool '{name}' not available."
            else:
                _tool_structured_result.set(None)
                try:
                    raw = entry["callable"](**args)
                except Exception as exc:
                    log.warning("trip: tool %s failed: %s", name, exc)
                    raw = f"ERROR: {exc}"
                structured = _tool_structured_result.get()
                _collect_seen(structured, seen_talks, seen_debates, seen_persons)
                result_string = _compact_result_string(structured, raw)
            executed[key] = result_string
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": name,
                    "content": result_string,
                }
            )

    # Forced synthesis with a hard output cap. The format= path converts tool
    # messages to user turns internally, so the transcript survives intact.
    messages.append({"role": "user", "content": _FINAL_INSTRUCTION})
    try:
        final = smart_llm.generate(
            messages=list(messages),
            format=ThreadResearch,
            think=False,
            max_tokens=_FINAL_MAX_TOKENS,
        )
    except Exception:
        log.exception("trip: final synthesis failed for %r", title)
        return ThreadResearch()
    if isinstance(final, str):
        log.warning("trip: synthesis LLM error: %s", final[:200])
        return ThreadResearch()
    parsed = getattr(final, "parsed", None)
    if not isinstance(parsed, ThreadResearch):
        log.warning("trip: synthesis returned no parsed ThreadResearch")
        return ThreadResearch()

    out = _ground(parsed, seen_talks, seen_debates, seen_persons)
    for f in out.findings:
        _emit({"phase": "finding", "label": f.label, "detail": f.detail})
    return out
