"""Job handlers for deep research. Imported by backend/job_runner.py (child)
so the registry is populated there — and by backend/routes/research.py (web)
so spawn_job can validate kinds.

Handlers build their LLMs from server-side env unless the spawning request
supplied a provider override, which reaches them as params["llm"] via the job
spec's stdin secrets channel (see backend/routes/research.py). They must NOT
import backend.services.chat (heavy import-time side effects) — the shared
override model lives in backend.services.llm_override instead.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from packages.llm import LLM

from backend.services import llm_override
from backend.services.research import board as board_mod
from backend.services.research import synthesis
from backend.services.research.jobs import JobContext, register

log = logging.getLogger("riksdagen.research.handlers")

# Model tiering (guide §10): trips + synthesis on the smart model, discovery
# and the read_documents_for reader on the fast model.
SMART_MODEL = os.getenv("LLM_MODEL_SMART", "smart")
FAST_MODEL = os.getenv("LLM_MODEL_FAST", "smart")


def _build_llms_from_env():
    base_url = os.getenv("LLM_DIRECT_URL")
    smart = LLM(model=SMART_MODEL, base_url=base_url, temperature=0.2)
    fast = LLM(model=FAST_MODEL, base_url=base_url, temperature=0.1)
    return smart, fast


def _build_llms(params: dict):
    """(smart, fast) for this job — the user's provider when one was supplied,
    otherwise the server's. params["llm"] arrives via the secrets channel and is
    therefore absent from the persisted jobs row."""
    override = params.get("llm")
    if not override:
        return _build_llms_from_env()
    return llm_override.build_research_llms(override)


def _clean_title(title: str) -> Optional[str]:
    """A board title from the discovery pass, or None to keep the placeholder.

    Models like to answer a "max 60 chars" instruction with a sentence, so trim
    on a word boundary and drop the trailing year rather than trusting it.
    """
    title = " ".join((title or "").split()).strip().strip('"').rstrip(".")
    if len(title) < 3:
        return None
    if len(title) > 70:
        title = title[:70].rsplit(" ", 1)[0] + "…"
    return title


def _emit_trip_events(ctx: JobContext, thread_title: str):
    """Adapter: research_trip on_event -> job_events rows."""

    def on_event(ev: dict) -> None:
        phase = ev.get("phase")
        if phase == "tool":
            ctx.progress(
                current=thread_title,
                message=f"Söker: {ev.get('name')}",
                data={"tool": ev.get("name"), "args": ev.get("args")},
            )
        elif phase == "finding":
            ctx.progress(
                current=thread_title,
                message=f"Fynd: {ev.get('label')}",
                data={"finding": {"label": ev.get("label"), "detail": ev.get("detail")}},
            )

    return on_event


def _sweep(smart, fast, board_id: str, ctx: JobContext, *, done_offset: int = 0) -> int:
    """Deepen threads shallowest-first until all reach target depth, the job is
    cancelled, or the safety cap on trips is hit. Returns trips completed."""
    board = board_mod.get_board(board_id, key=ctx.board_key)
    if board is None:
        raise RuntimeError(f"board {board_id} not found")
    target = int(board["target_depth"])
    threads = [
        t for t in board_mod.get_threads(board_id, key=ctx.board_key)
        if t["status"] == "active"
    ]
    max_trips = max(1, len(threads) * target + 2)
    total = sum(max(0, target - int(t["depth"])) for t in threads)
    ctx.progress(done=done_offset, total=done_offset + total, message="Gräver…")

    trips = 0
    while trips < max_trips:
        if ctx.is_cancelled():
            log.info("sweep: cancelled after %d trips", trips)
            break
        nxt = board_mod.pick_next_thread(board_id, target, key=ctx.board_key)
        if nxt is None:
            break
        ctx.progress(
            done=done_offset + trips,
            current=nxt["title"],
            message=f"Gräver i tråden: {nxt['title']} (djup {nxt['depth'] + 1}/{target})",
        )
        did = board_mod.deepen_step(
            smart, fast, board_id, thread_id=nxt["id"],
            on_event=_emit_trip_events(ctx, nxt["title"]),
            key=ctx.board_key,
        )
        if not did:
            break
        trips += 1
        ctx.progress(done=done_offset + trips)
    return trips


def _finalize_round(smart, fast, board_id: str, ctx: JobContext, *,
                    followups: bool) -> None:
    """End-of-dig bookkeeping: refresh stale thread answers (depth advanced
    past the depth the answer was written at), then optionally turn the round's
    open questions + leads into new *proposed* threads for the user."""
    threads = board_mod.get_threads(board_id, key=ctx.board_key)
    for t in threads:
        if t["status"] != "active" or not (t.get("findings") or []):
            continue
        if int(t["depth"]) <= int(t.get("answer_depth") or 0):
            continue
        if ctx.is_cancelled():
            return
        ctx.progress(current=t["title"], message=f"Sammanställer svar: {t['title']}")
        answer = synthesis.synthesize_thread_answer(smart, t)
        if answer:
            board_mod.set_thread_answer(
                t["id"], board_id, answer, depth=int(t["depth"]), key=ctx.board_key,
            )
    if not followups or ctx.is_cancelled():
        return
    board = board_mod.get_board(board_id, key=ctx.board_key)
    if board is None:
        return
    seeds = board_mod.propose_followups(fast, board, threads, key=ctx.board_key)
    for seed in seeds:
        board_mod.insert_thread(
            board_id, title=seed.title, question=seed.question, why=seed.why,
            origin="auto", status="proposed", hints=seed.hints, key=ctx.board_key,
        )
    if seeds:
        ctx.progress(message=f"{len(seeds)} nya trådförslag att ta ställning till")


@register("research_scout", max_runtime_secs=900)
def run_scout(params: dict, ctx: JobContext) -> None:
    """Scout phase: a few bounded search rounds over the corpus, then thread
    proposals for the user to approve. No digging happens here — the board
    lands in 'awaiting' and waits for the user's selection. A cancel skips
    remaining rounds but still proposes from the material gathered so far."""
    board_id = params["board_id"]
    _smart, fast = _build_llms(params)
    board = board_mod.get_board(board_id, key=ctx.board_key)
    if board is None:
        raise RuntimeError(f"board {board_id} not found")

    try:
        ctx.progress(message="Kartlägger ämnet i databasen…")
        material = board_mod.scout_material(
            fast, board["topic"],
            on_event=lambda q: ctx.progress(message=f"Söker: {q}"),
            is_cancelled=ctx.is_cancelled,
        )
        ctx.progress(message="Formulerar trådförslag…")
        seeds = board_mod.discover_threads(
            fast, board,
            max_threads=board_mod.RESEARCH_PROPOSAL_COUNT,
            material=material,
        )
        for seed in seeds.threads:
            board_mod.insert_thread(
                board_id, title=seed.title, question=seed.question, why=seed.why,
                origin="auto", status="proposed", hints=seed.hints, key=ctx.board_key,
            )
        # The board was created with the raw topic truncated as a placeholder
        # title; replace it now that the LLM has read the material.
        board_mod.set_board_status(board_id, "awaiting", intro=seeds.intro or None,
                                   key=ctx.board_key,
                                   title=_clean_title(seeds.title))
        ctx.set_counts(proposals=len(seeds.threads))
        ctx.progress(
            message=f"{len(seeds.threads)} trådförslag — välj vilka som ska grävas"
        )
    except Exception:
        board_mod.set_board_status(board_id, "failed")
        raise


@register("research_report", max_runtime_secs=900)
def run_report(params: dict, ctx: JobContext) -> None:
    """Write (or rewrite) the single board report from the thread answers.
    The board always returns to 'ready' — a failed report never invalidates
    the researched content."""
    board_id = params["board_id"]
    smart, _fast = _build_llms(params)
    try:
        board = board_mod.get_board(board_id, key=ctx.board_key)
        if board is None:
            raise RuntimeError(f"board {board_id} not found")
        threads = [
            t for t in board_mod.get_threads(board_id, key=ctx.board_key)
            if t["status"] == "active"
        ]
        ctx.progress(message="Skriver rapporten…")
        report = synthesis.synthesize_report(smart, board, threads)
        if not report:
            raise RuntimeError("Rapporten kunde inte skrivas — försök igen")
        board_mod.set_board_report(board_id, report, key=ctx.board_key)
        ctx.progress(message="Rapporten är klar")
    finally:
        board_mod.set_board_status(board_id, "ready")


@register("research_build")
def run_build(params: dict, ctx: JobContext) -> None:
    """Full build: discovery -> insert auto threads -> deepen to target depth."""
    board_id = params["board_id"]
    smart, fast = _build_llms(params)
    board = board_mod.get_board(board_id, key=ctx.board_key)
    if board is None:
        raise RuntimeError(f"board {board_id} not found")

    try:
        ctx.progress(message="Kartlägger ämnet i databasen…")
        seeds = board_mod.discover_threads(fast, board)
        for seed in seeds.threads:
            board_mod.insert_thread(
                board_id, title=seed.title, question=seed.question,
                why=seed.why, origin="auto", key=ctx.board_key,
            )
        # Seed hints from discovery ride along via the thread's first trip
        # (deepen_step falls back to stored leads; for a fresh thread the
        # discovery hints ARE the question context, so run trip 1 explicitly).
        board_mod.set_board_status(board_id, "digging", intro=seeds.intro or None,
                                   key=ctx.board_key,
                                   title=_clean_title(seeds.title))
        threads = board_mod.get_threads(board_id, key=ctx.board_key)
        hint_by_id = {}
        for t, seed in zip(
            [t for t in threads if t["origin"] == "auto"], seeds.threads
        ):
            hint_by_id[t["id"]] = seed.hints
        target = int(board["target_depth"])
        total = len(threads) * target
        ctx.progress(done=0, total=total, message=f"{len(threads)} trådar att gräva i")

        trips = 0
        # First pass with discovery hints, one trip per thread.
        for t in threads:
            if ctx.is_cancelled():
                break
            ctx.progress(
                done=trips, current=t["title"],
                message=f"Gräver i tråden: {t['title']}",
            )
            board_mod.deepen_step(
                smart, fast, board_id, thread_id=t["id"],
                hints=hint_by_id.get(t["id"]),
                on_event=_emit_trip_events(ctx, t["title"]),
                key=ctx.board_key,
            )
            trips += 1
        # Then breadth-level to the ceiling.
        if not ctx.is_cancelled():
            trips += _sweep(smart, fast, board_id, ctx, done_offset=trips)
        _finalize_round(smart, fast, board_id, ctx, followups=False)
        board_mod.set_board_status(board_id, "ready")
        ctx.set_counts(trips=trips, threads=len(threads))
        ctx.progress(done=trips, message="Klart")
    except Exception:
        board_mod.set_board_status(board_id, "failed")
        raise


@register("research_deepen")
def run_deepen(params: dict, ctx: JobContext) -> None:
    """One targeted trip (thread_id and/or lead), optionally followed by a sweep."""
    board_id = params["board_id"]
    thread_id = params.get("thread_id")
    lead = params.get("lead")
    sweep = bool(params.get("sweep"))
    smart, fast = _build_llms(params)

    try:
        board_mod.set_board_status(board_id, "digging")
        trips = 0
        if thread_id or lead:
            thread = board_mod.get_thread(thread_id, key=ctx.board_key) if thread_id else None
            title = (thread or {}).get("title") or "Tråd"
            ctx.progress(current=title, message=f"Gräver vidare: {title}")
            if board_mod.deepen_step(
                smart, fast, board_id, thread_id=thread_id, lead=lead,
                on_event=_emit_trip_events(ctx, title),
                key=ctx.board_key,
            ):
                trips += 1
        if sweep and not ctx.is_cancelled():
            trips += _sweep(smart, fast, board_id, ctx, done_offset=trips)
        # Round wrap-up: refresh stale thread answers; sweeps (a full "round")
        # also surface new proposals for the user's next selection.
        _finalize_round(smart, fast, board_id, ctx, followups=sweep)
        board_mod.set_board_status(board_id, "ready")
        ctx.set_counts(trips=trips)
        ctx.progress(message="Klart")
    except Exception:
        board_mod.set_board_status(board_id, "failed")
        raise
