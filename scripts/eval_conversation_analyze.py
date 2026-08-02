"""Analyze opt-in TEST conversations logged in Postgres (eval_conversations).

Companion to eval_conversation_export.py: where export renders transcripts,
this script computes efficiency/problem statistics so an LLM (or human) can
quickly find the turns worth reading in full.

Usage:
    python scripts/eval_conversation_analyze.py                     # overall + per-session summary
    python scripts/eval_conversation_analyze.py --problems          # only turns with problem flags
    python scripts/eval_conversation_analyze.py --tools             # tool usage histogram
    python scripts/eval_conversation_analyze.py --turn ROWID        # full detail for one turn
    python scripts/eval_conversation_analyze.py --since 2026-07-01  # restrict any mode by date
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from dotenv import load_dotenv
load_dotenv()

from postgres_client import pg

# Thresholds for flagging a turn as problematic
MAX_ITERATIONS = 20          # ChatService.max_tool_iterations
ITERATION_WARN = 10
DURATION_WARN_S = 120


def _fetch(since: str | None) -> List[Dict[str, Any]]:
    where = "WHERE started_at >= %s" if since else ""
    rows = pg.execute(
        f"SELECT id, session_id, turn_index, doc FROM eval_conversations {where} ORDER BY started_at ASC",
        (since,) if since else None,
    )
    return rows


def analyze_turn(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Compute per-turn metrics and problem flags from a logged doc."""
    events = doc.get("events") or []
    tool_calls = [e for e in events if e.get("type") == "tool_call"]
    tool_results = [e for e in events if e.get("type") == "tool_result"]

    # Repeated identical tool calls (same tool + same args) — a misbehaviour signal
    seen: Counter = Counter()
    for e in tool_calls:
        seen[(e.get("tool"), json.dumps(e.get("args") or {}, sort_keys=True, default=str))] += 1
    repeated = {f"{tool}": n for (tool, _args), n in seen.items() if n > 1}

    error_results = [
        e for e in tool_results if str(e.get("content", "")).lstrip().startswith("ERROR")
    ]
    empty_results = [
        e for e in tool_results if not str(e.get("content", "")).strip()
    ]

    result = doc.get("result") or {}
    answer = result.get("answer") or ""
    iterations = doc.get("iterations") or 0
    duration = doc.get("duration_s") or 0

    flags = []
    if doc.get("error"):
        flags.append(f"exception: {doc['error'].get('type')}: {doc['error'].get('message', '')[:120]}")
    if iterations >= MAX_ITERATIONS:
        flags.append(f"hit max iterations ({iterations})")
    elif iterations >= ITERATION_WARN:
        flags.append(f"high iterations ({iterations})")
    if duration >= DURATION_WARN_S:
        flags.append(f"slow ({duration:.0f}s)")
    if repeated:
        flags.append(f"repeated identical tool calls: {repeated}")
    if error_results:
        flags.append(f"{len(error_results)} tool ERROR result(s): {[e.get('tool') for e in error_results]}")
    if empty_results:
        flags.append(f"{len(empty_results)} empty tool result(s)")
    if not doc.get("error") and not answer.strip():
        flags.append("empty answer")
    if result.get("attribution_warnings"):
        flags.append(f"{len(result['attribution_warnings'])} attribution warning(s)")

    return {
        "session_id": doc.get("session_id"),
        "turn_index": doc.get("turn_index"),
        "started_at": doc.get("started_at"),
        "duration_s": duration,
        "iterations": iterations,
        "n_events": len(events),
        "tools_used": Counter(e.get("tool") for e in tool_calls),
        "answer_chars": len(answer),
        "n_sources": len(result.get("sources") or []),
        "flags": flags,
    }


def cmd_summary(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        print("No logged conversations.")
        return
    metrics = [(r["id"], analyze_turn(r["doc"])) for r in rows]
    durations = [m["duration_s"] for _, m in metrics]
    iterations = [m["iterations"] for _, m in metrics]
    flagged = [(rid, m) for rid, m in metrics if m["flags"]]

    print(f"## Overall: {len(metrics)} turns, {len({m['session_id'] for _, m in metrics})} sessions")
    print(f"- duration: avg {sum(durations)/len(durations):.0f}s, max {max(durations):.0f}s")
    print(f"- iterations: avg {sum(iterations)/len(iterations):.1f}, max {max(iterations)}")
    print(f"- flagged turns: {len(flagged)}/{len(metrics)}")
    print()
    print(f"{'id':>5}  {'session':<24} {'turn':>4} {'dur_s':>6} {'iter':>4} {'src':>3}  flags")
    for rid, m in metrics:
        print(
            f"{rid:>5}  {str(m['session_id'])[:24]:<24} {m['turn_index']:>4} "
            f"{m['duration_s']:>6.0f} {m['iterations']:>4} {m['n_sources']:>3}  "
            f"{'; '.join(m['flags']) or '-'}"
        )


def cmd_problems(rows: List[Dict[str, Any]]) -> None:
    found = False
    for r in rows:
        m = analyze_turn(r["doc"])
        if not m["flags"]:
            continue
        found = True
        print(f"### row {r['id']} — session {m['session_id']} turn {m['turn_index']} ({m['started_at']})")
        for f in m["flags"]:
            print(f"- {f}")
        print(f"- tools: {dict(m['tools_used'])}")
        print(f"  → full detail: python scripts/eval_conversation_analyze.py --turn {r['id']}")
        print()
    if not found:
        print("No problem flags found.")


def cmd_tools(rows: List[Dict[str, Any]]) -> None:
    calls: Counter = Counter()
    durations: Dict[str, List[float]] = {}
    for r in rows:
        events = r["doc"].get("events") or []
        for i, e in enumerate(events):
            if e.get("type") != "tool_call":
                continue
            calls[e.get("tool")] += 1
            # elapsed until the matching tool_result (next tool_result event for the same tool)
            for nxt in events[i + 1:]:
                if nxt.get("type") == "tool_result" and nxt.get("tool") == e.get("tool"):
                    durations.setdefault(e.get("tool"), []).append(
                        (nxt.get("t_ms", 0) - e.get("t_ms", 0)) / 1000
                    )
                    break
    print(f"{'tool':<28} {'calls':>6} {'avg_s':>7} {'max_s':>7}")
    for tool, n in calls.most_common():
        ds = durations.get(tool) or [0]
        print(f"{tool:<28} {n:>6} {sum(ds)/len(ds):>7.1f} {max(ds):>7.1f}")


def cmd_turn(row_id: int) -> None:
    rows = pg.execute("SELECT id, doc FROM eval_conversations WHERE id = %s", (row_id,))
    if not rows:
        print(f"No row with id {row_id}.")
        return
    doc = rows[0]["doc"]
    m = analyze_turn(doc)
    print(f"# Row {row_id} — session {m['session_id']} turn {m['turn_index']}")
    print(f"Flags: {'; '.join(m['flags']) or 'none'}")
    print()
    from eval_conversation_export import render_markdown
    print(render_markdown([doc]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--problems", action="store_true", help="Show only flagged turns.")
    parser.add_argument("--tools", action="store_true", help="Tool usage histogram with timings.")
    parser.add_argument("--turn", type=int, metavar="ROWID", help="Full detail for one turn.")
    parser.add_argument("--since", help="Only turns started on/after this date (YYYY-MM-DD).")
    args = parser.parse_args()

    if args.turn is not None:
        cmd_turn(args.turn)
        return
    rows = _fetch(args.since)
    if args.problems:
        cmd_problems(rows)
    elif args.tools:
        cmd_tools(rows)
    else:
        cmd_summary(rows)


if __name__ == "__main__":
    main()
