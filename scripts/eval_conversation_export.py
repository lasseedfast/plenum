"""Export opt-in TEST conversations logged in Postgres for LLM analysis.

Reads the eval_conversations table (written by
backend/services/eval_conversation_logger.py when a conversation starts
with "TEST ") and renders sessions either as Markdown designed to be
pasted into an analysis LLM, or as raw JSON for programmatic judging.

Usage:
    python scripts/eval_conversation_export.py --list
    python scripts/eval_conversation_export.py --session-id SID
    python scripts/eval_conversation_export.py --session-id SID --format json --out out.json
    python scripts/eval_conversation_export.py --key ROWID
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402  — after sys.path setup above

load_dotenv()

from postgres_client import pg  # noqa: E402


def list_sessions() -> list[dict[str, Any]]:
    return pg.execute(
        """
        SELECT session_id,
               COUNT(*)             AS turns,
               MAX(started_at)      AS last,
               SUM(has_error::int)  AS errors
        FROM eval_conversations
        GROUP BY session_id
        ORDER BY last DESC
        """
    )


def fetch_turns(session_id: str) -> list[dict[str, Any]]:
    rows = pg.execute(
        "SELECT doc FROM eval_conversations WHERE session_id = %s ORDER BY started_at ASC",
        (session_id,),
    )
    return [r["doc"] for r in rows]


def fetch_by_key(key: str) -> list[dict[str, Any]]:
    rows = pg.execute("SELECT doc FROM eval_conversations WHERE id = %s", (int(key),))
    return [r["doc"] for r in rows]


def _preview(text: Any, limit: int = 400) -> str:
    s = str(text or "").replace("\n", " ").strip()
    return s if len(s) <= limit else s[:limit] + " (…)"


def _render_event(e: dict[str, Any]) -> str:
    t_s = e.get("t_ms", 0) / 1000
    etype = e.get("type")
    if etype == "tool_call":
        args = json.dumps(e.get("args") or {}, ensure_ascii=False)
        return f"- `{t_s:7.1f}s` **tool_call #{e.get('iteration', '?')}** `{e.get('tool')}` args: {_preview(args)}"
    if etype == "tool_result":
        return (
            f"- `{t_s:7.1f}s` tool_result `{e.get('tool')}` "
            f"({e.get('chars', '?')} chars): {_preview(e.get('content'), 300)}"
        )
    if etype == "status":
        return f"- `{t_s:7.1f}s` status: {_preview(e.get('message'))}"
    if etype == "insight":
        return f"- `{t_s:7.1f}s` insight: {_preview(e.get('message'))}"
    if etype == "search_card":
        ids = [r.get("_id") if isinstance(r, dict) else r for r in (e.get("results") or [])]
        return (
            f"- `{t_s:7.1f}s` search_card query={_preview(e.get('query'), 120)!r} "
            f"total={e.get('total')} hits={ids}"
        )
    if etype == "stats_card":
        return f"- `{t_s:7.1f}s` stats_card rows={len(e.get('rows') or [])}"
    if etype == "tool_speakers":
        return f"- `{t_s:7.1f}s` tool_speakers: {e.get('person_ids')}"
    return f"- `{t_s:7.1f}s` {etype}: {_preview({k: v for k, v in e.items() if k not in ('i', 't_ms', 'type')})}"


def render_markdown(turns: list[dict[str, Any]]) -> str:
    if not turns:
        return "No logged turns found."
    lines: list[str] = [f"# Eval conversation `{turns[0].get('session_id')}`", ""]
    for doc in turns:
        lines.append(f"## Turn {doc.get('turn_index')} — {doc.get('started_at')}")
        lines.append(
            f"*duration: {doc.get('duration_s')}s · iterations: {doc.get('iterations')} · "
            f"stream: {doc.get('stream')} · provider: "
            f"{json.dumps((doc.get('request') or {}).get('provider'), ensure_ascii=False)}*"
        )
        lines.append("")
        messages = (doc.get("request") or {}).get("messages") or []
        user_msgs = [m for m in messages if m.get("role") == "user"]
        if user_msgs:
            lines.append(f"**User:** {user_msgs[-1].get('content')}")
            lines.append("")
        lines.append("### Timeline")
        for e in doc.get("events") or []:
            lines.append(_render_event(e))
        lines.append("")
        result = doc.get("result")
        if result:
            lines.append("### Answer")
            lines.append(result.get("answer") or "*(empty)*")
            lines.append("")
            sources = result.get("sources") or []
            if sources:
                lines.append("### Sources")
                for n, s in enumerate(sources, 1):
                    lines.append(
                        f"{n}. `{s.get('_id')}` {s.get('speaker') or '?'} "
                        f"({s.get('party') or '?'}, {s.get('date') or '?'}): "
                        f"{_preview(s.get('snippet'), 200)}"
                    )
                lines.append("")
            warnings = result.get("attribution_warnings") or []
            if warnings:
                lines.append("### Attribution warnings")
                for w in warnings:
                    lines.append(f"- {json.dumps(w, ensure_ascii=False)}")
                lines.append("")
        error = doc.get("error")
        if error:
            lines.append("### ERROR")
            lines.append(f"**{error.get('type')}**: {error.get('message')}")
            lines.append("```")
            lines.append((error.get("traceback") or "").strip())
            lines.append("```")
            lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="List logged sessions.")
    parser.add_argument("--session-id", help="Export all turns for a session.")
    parser.add_argument("--key", help="Export a single turn doc by _key.")
    parser.add_argument("--format", choices=["md", "json"], default="md")
    parser.add_argument("--out", help="Write output to file instead of stdout.")
    args = parser.parse_args()

    if args.list:
        sessions = list_sessions()
        if not sessions:
            print("No logged conversations.")
            return
        print(f"{'session_id':<40} {'turns':>5} {'errors':>6}  last activity")
        for s in sessions:
            print(f"{s['session_id']:<40} {s['turns']:>5} {s['errors']:>6}  {s['last']}")
        return

    if args.session_id:
        turns = fetch_turns(args.session_id)
    elif args.key:
        turns = fetch_by_key(args.key)
    else:
        parser.error("one of --list, --session-id or --key is required")
        return

    if args.format == "json":
        output = json.dumps(turns, ensure_ascii=False, indent=2, default=str)
    else:
        output = render_markdown(turns)

    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        print(f"Wrote {len(turns)} turn(s) to {args.out}")
    else:
        print(output)


if __name__ == "__main__":
    main()
