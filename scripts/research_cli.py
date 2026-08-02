#!/usr/bin/env python3
"""Run deep research synchronously (no job runner) and pretty-print the board.

The Phase-3 test harness and a permanent debugging tool:

    python scripts/research_cli.py --topic "kärnkraftsdebattens utveckling 2010-2024" --trips 3
    python scripts/research_cli.py --board <board_id> --trips 2     # deepen existing
    python scripts/research_cli.py --board <board_id> --show        # just print it

Interactive flow (scout → approve → dig → answer → report):

    python scripts/research_cli.py --topic "..." --scout            # propose threads, wait
    python scripts/research_cli.py --board <id> --activate <thread_id> --guidance "..."
    python scripts/research_cli.py --board <id> --trips 2            # dig activated threads
    python scripts/research_cli.py --board <id> --answer <thread_id> # synthesize + check cites
    python scripts/research_cli.py --board <id> --report            # write the board report

Uses the same engine code as the background job (discover_threads, deepen_step,
synthesize_*) but inline in this process, so stack traces land in your terminal.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services.research import board as board_mod  # noqa: E402
from backend.services.research import synthesis  # noqa: E402
from backend.services.research.handlers import _build_llms_from_env  # noqa: E402


def _print_board(board: dict, threads: list[dict]) -> None:
    print("=" * 78)
    print(f"BOARD {board['id']}  [{board['status']}] rev={board['revision']}")
    print(f"  Ämne: {board['topic']}")
    if board.get("intro"):
        print(f"  Intro: {board['intro']}")
    for t in threads:
        print("-" * 78)
        print(f"  [{t['origin']}/{t.get('status')}] {t['title']}  (djup {t['depth']})  id={t['id']}")
        print(f"    Fråga: {t['question']}")
        if t.get("why"):
            print(f"    Varför: {t['why']}")
        if t.get("guidance"):
            print(f"    Medskick: {t['guidance']}")
        if t.get("answer"):
            print("    SVAR:")
            for line in t["answer"].splitlines():
                print(f"      {line}")
        for f in t.get("findings") or []:
            src = f.get("source_id") or "?"
            who = f.get("speaker") or ""
            party = f" ({f['party']})" if f.get("party") else ""
            date = f.get("date") or ""
            print(f"    • {f.get('label')}")
            if f.get("detail"):
                print(f"      {f['detail']}")
            if f.get("quote"):
                print(f"      ”{f['quote']}”")
            print(f"      — [src:{src}] {who}{party} {date}")
        if t.get("open_questions"):
            print("    Öppna frågor:")
            for q in t["open_questions"]:
                print(f"      ? {q}")
        if t.get("leads"):
            print("    Spår:")
            for l in t["leads"]:
                label = f" ({l['label']})" if l.get("label") else ""
                print(f"      → [{l.get('kind')}] {l.get('target')}{label}: {l.get('lead')}")
    if board.get("report"):
        print("-" * 78)
        print("  RAPPORT:")
        for line in board["report"].splitlines():
            print(f"    {line}")
    print("=" * 78)


def _on_event(ev: dict) -> None:
    if ev.get("phase") == "tool":
        print(f"    [tool] {ev.get('name')} {ev.get('args')}")
    elif ev.get("phase") == "finding":
        print(f"    [fynd] {ev.get('label')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--topic", help="Create a new board for this topic")
    ap.add_argument("--board", help="Existing board id to deepen/show")
    ap.add_argument("--trips", type=int, default=0, help="Number of deepen trips to run")
    ap.add_argument("--show", action="store_true", help="Only print the board")
    ap.add_argument("--scout", action="store_true",
                    help="With --topic: scout + propose threads (status 'proposed'), then wait")
    ap.add_argument("--activate", help="Activate a proposed thread by id")
    ap.add_argument("--guidance", help="Guidance text for --activate")
    ap.add_argument("--answer", help="Synthesize + print the answer for a thread id")
    ap.add_argument("--report", action="store_true", help="Write and print the board report")
    args = ap.parse_args()

    if not args.topic and not args.board:
        ap.error("--topic or --board required")

    smart, fast = _build_llms_from_env()

    if args.topic:
        board = board_mod.create_board(args.topic)
        board_id = board["id"]
        print(f"Created board {board_id}")
        if args.scout:
            print("Scouting the corpus…")
            material = board_mod.scout_material(
                fast, board["topic"], on_event=lambda q: print(f"    [sök] {q}")
            )
            seeds = board_mod.discover_threads(
                fast, board, max_threads=board_mod.RESEARCH_PROPOSAL_COUNT, material=material,
            )
            print(f"  intro: {seeds.intro}")
            for seed in seeds.threads:
                t = board_mod.insert_thread(
                    board_id, title=seed.title, question=seed.question, why=seed.why,
                    origin="auto", status="proposed", hints=seed.hints,
                )
                print(f"  ? [{t['id']}] {seed.title} (hints: {', '.join(seed.hints)})")
            board_mod.set_board_status(board_id, "awaiting", intro=seeds.intro or None)
            print("\nProposals inserted. Activate some with --board <id> --activate <thread_id>.")
            return 0
        print("Discovering threads…")
        seeds = board_mod.discover_threads(fast, board)
        print(f"  intro: {seeds.intro}")
        for seed in seeds.threads:
            t = board_mod.insert_thread(
                board_id, title=seed.title, question=seed.question,
                why=seed.why, origin="auto",
            )
            print(f"  + [{t['id'][:8]}] {seed.title} (hints: {', '.join(seed.hints)})")
        board_mod.set_board_status(board_id, "digging", intro=seeds.intro or None)
    else:
        board_id = args.board
        board = board_mod.get_board(board_id)
        if board is None:
            print(f"No board {board_id}", file=sys.stderr)
            return 1

    if args.activate:
        ok = board_mod.activate_thread(args.activate, board_id, guidance=args.guidance)
        print(f"Activated {args.activate}: {ok}" + (f" (medskick: {args.guidance})" if args.guidance else ""))
        if ok:
            board_mod.bump_revision(board_id)
        if not args.trips:
            return 0

    if args.answer:
        thread = board_mod.get_thread(args.answer)
        if thread is None:
            print(f"No thread {args.answer}", file=sys.stderr)
            return 1
        answer = synthesis.synthesize_thread_answer(smart, thread)
        if not answer:
            print("No answer produced (no findings or LLM error).")
            return 1
        print(answer)
        # Verify every citation id exists in the thread's findings.
        cited = set(synthesis.CITE_RE.findall(answer))
        known = {f.get("source_id") for f in (thread.get("findings") or [])}
        bad = cited - known
        print(f"\n[cites: {len(cited)} used, {len(bad)} ungrounded]")
        assert not bad, f"UNGROUNDED CITATIONS: {bad}"
        board_mod.set_thread_answer(args.answer, board_id, answer, depth=int(thread["depth"]))
        return 0

    if args.report:
        board = board_mod.get_board(board_id)
        threads = [t for t in board_mod.get_threads(board_id) if t["status"] == "active"]
        report = synthesis.synthesize_report(smart, board, threads)
        if not report:
            print("No report produced.")
            return 1
        board_mod.set_board_report(board_id, report)
        _print_board(board_mod.get_board(board_id), board_mod.get_threads(board_id))
        return 0

    if args.show:
        _print_board(board_mod.get_board(board_id), board_mod.get_threads(board_id))
        return 0

    for i in range(args.trips):
        board = board_mod.get_board(board_id)
        nxt = board_mod.pick_next_thread(board_id, int(board["target_depth"]))
        if nxt is None:
            print("All threads at target depth — nothing to do.")
            break
        print(f"\nTrip {i + 1}/{args.trips}: {nxt['title']} (djup {nxt['depth']} → {nxt['depth'] + 1})")
        board_mod.deepen_step(
            smart, fast, board_id, thread_id=nxt["id"], on_event=_on_event
        )

    if args.trips or args.topic:
        board_mod.set_board_status(board_id, "ready")
    _print_board(board_mod.get_board(board_id), board_mod.get_threads(board_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
