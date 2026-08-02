"""Reproducible analysis of a single eval_harness run.

Usage:
    python scripts/analyze_eval_results.py                       # latest finished run
    python scripts/analyze_eval_results.py --run-id <uuid>
    python scripts/analyze_eval_results.py --compare <base> <new>

Prints: verdict distribution, wrong_speaker-by-complexity, bad-pct per tool,
citations-per-paragraph split, good-vs-bad source counts, top llm_events,
and a few sample wrong_speaker paragraphs with rationale + mismatch.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv()

from postgres_client import pg


def _latest_run_id() -> Optional[str]:
    rows = pg.execute(
        "SELECT id::text FROM eval_runs ORDER BY started_at DESC LIMIT 1"
    )
    return rows[0]["id"] if rows else None


def _fmt_row(row: Dict[str, Any], cols: List[str]) -> str:
    return " | ".join(f"{c}={row.get(c)}" for c in cols)


def _print_rows(header: str, rows: List[Dict[str, Any]], cols: Optional[List[str]] = None) -> None:
    print(f"\n--- {header} ---")
    if not rows:
        print("(no rows)")
        return
    if cols is None:
        cols = list(rows[0].keys())
    widths = {c: max(len(str(c)), *(len(str(r.get(c))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c)).ljust(widths[c]) for c in cols))


def run_summary(run_id: str) -> None:
    meta = pg.execute(
        """SELECT id::text, label, config->>'smart_model' AS smart_model,
                  config->>'judge_model' AS judge_model, config->>'git_sha' AS git_sha,
                  started_at::text, finished_at::text, num_questions
           FROM eval_runs WHERE id = %s""",
        (run_id,),
    )
    if not meta:
        print(f"No run found with id {run_id}")
        return
    m = meta[0]
    print(f"\n=== Run {m['id']} ===")
    print(f"label:      {m['label']}")
    print(f"smart:      {m['smart_model']}   judge: {m['judge_model']}")
    print(f"git_sha:    {m['git_sha']}")
    print(f"started:    {m['started_at']}")
    print(f"finished:   {m['finished_at']}")
    print(f"questions:  {m['num_questions']}")


def verdict_distribution(run_id: str) -> None:
    rows = pg.execute(
        """SELECT j.verdict, COUNT(*) AS n,
                  ROUND(100.0*COUNT(*)/SUM(COUNT(*)) OVER (), 1) AS pct
           FROM eval_judgments j
           JOIN eval_questions q ON q.id = j.question_id
           WHERE q.run_id = %s
           GROUP BY j.verdict
           ORDER BY n DESC""",
        (run_id,),
    )
    _print_rows("Verdict distribution", rows, ["verdict", "n", "pct"])


def by_complexity(run_id: str) -> None:
    rows = pg.execute(
        """SELECT q.complexity,
                  COUNT(*) AS paras,
                  ROUND(100.0*COUNT(*) FILTER (WHERE j.verdict='supported')/COUNT(*), 1)     AS supported_pct,
                  ROUND(100.0*COUNT(*) FILTER (WHERE j.verdict='wrong_speaker')/COUNT(*), 1) AS wrong_speaker_pct,
                  ROUND(100.0*COUNT(*) FILTER (WHERE j.verdict='unsupported')/COUNT(*), 1)   AS unsupported_pct,
                  AVG(q.num_iterations)::numeric(10,1)     AS avg_iters,
                  AVG(q.duration_ms/1000.0)::numeric(10,1) AS avg_sec
           FROM eval_judgments j
           JOIN eval_questions q ON q.id = j.question_id
           WHERE q.run_id = %s
           GROUP BY q.complexity
           ORDER BY q.complexity""",
        (run_id,),
    )
    _print_rows(
        "By complexity", rows,
        ["complexity", "paras", "supported_pct", "wrong_speaker_pct", "unsupported_pct", "avg_iters", "avg_sec"],
    )


def tool_bad_pct(run_id: str) -> None:
    rows = pg.execute(
        """SELECT tool, COUNT(*) FILTER (WHERE bad) AS bad, COUNT(*) AS total,
                  ROUND(100.0 * COUNT(*) FILTER (WHERE bad) / COUNT(*), 1) AS bad_pct
           FROM (
             SELECT jsonb_array_elements(q.tool_trace)->>'tool' AS tool,
                    EXISTS (
                      SELECT 1 FROM eval_judgments j
                      WHERE j.question_id = q.id
                        AND j.verdict IN ('wrong_speaker','unsupported','wrong_attribution')
                    ) AS bad
             FROM eval_questions q
             WHERE q.run_id = %s
           ) t
           WHERE tool IS NOT NULL
           GROUP BY tool
           ORDER BY bad_pct DESC""",
        (run_id,),
    )
    _print_rows("Bad-pct by tool", rows, ["tool", "bad", "total", "bad_pct"])


def citations_per_paragraph(run_id: str) -> None:
    rows = pg.execute(
        """SELECT j.verdict,
                  COUNT(*) AS n,
                  AVG(array_length(j.cited_indices, 1))::numeric(10,2) AS avg_cites,
                  COUNT(*) FILTER (WHERE array_length(j.cited_indices, 1) >= 2) AS multi_cite,
                  COUNT(*) FILTER (WHERE array_length(j.cited_indices, 1) = 1)  AS single_cite
           FROM eval_judgments j
           JOIN eval_questions q ON q.id = j.question_id
           WHERE q.run_id = %s
           GROUP BY j.verdict
           ORDER BY n DESC""",
        (run_id,),
    )
    _print_rows(
        "Citations per paragraph",
        rows,
        ["verdict", "n", "avg_cites", "single_cite", "multi_cite"],
    )


def good_vs_bad_sources(run_id: str) -> None:
    rows = pg.execute(
        """SELECT (CASE WHEN bad_count > 0 THEN 'bad' ELSE 'good' END) AS class,
                  COUNT(*) AS qs,
                  AVG(num_iters)::numeric(10,1) AS avg_iters,
                  AVG(n_sources)::numeric(10,1) AS avg_sources
           FROM (
             SELECT q.id, q.num_iterations AS num_iters,
                    jsonb_array_length(q.sources) AS n_sources,
                    (SELECT COUNT(*) FROM eval_judgments j
                     WHERE j.question_id = q.id
                       AND j.verdict IN ('wrong_speaker','unsupported','wrong_attribution')
                    ) AS bad_count
             FROM eval_questions q
             WHERE q.run_id = %s AND q.answer IS NOT NULL
           ) t
           GROUP BY class
           ORDER BY class""",
        (run_id,),
    )
    _print_rows("Good vs bad questions", rows, ["class", "qs", "avg_iters", "avg_sources"])


def latency_summary(run_id: str) -> None:
    rows = pg.execute(
        """SELECT PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY duration_ms)::int AS p50_ms,
                  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_ms)::int AS p95_ms,
                  AVG(num_iterations)::numeric(10,1) AS avg_iters,
                  MAX(num_iterations) AS max_iters,
                  COUNT(*) FILTER (WHERE error IS NOT NULL) AS errors,
                  COUNT(*) AS total
           FROM eval_questions
           WHERE run_id = %s""",
        (run_id,),
    )
    _print_rows("Latency / iterations", rows, ["total", "errors", "avg_iters", "max_iters", "p50_ms", "p95_ms"])


def llm_events(run_id: str) -> None:
    rows = pg.execute(
        """SELECT event_type, COUNT(*) AS n
           FROM llm_events
           WHERE detail->>'eval_run_id' = %s
           GROUP BY event_type
           ORDER BY n DESC
           LIMIT 15""",
        (run_id,),
    )
    _print_rows("LLM events during run", rows, ["event_type", "n"])


def metadata_mismatch_parity(run_id: str) -> None:
    rows = pg.execute(
        """SELECT j.verdict,
                  COUNT(*) AS total,
                  COUNT(*) FILTER (WHERE j.metadata_mismatch IS NOT NULL) AS det_caught,
                  ROUND(100.0 * COUNT(*) FILTER (WHERE j.metadata_mismatch IS NOT NULL) / COUNT(*), 1) AS det_pct
           FROM eval_judgments j
           JOIN eval_questions q ON q.id = j.question_id
           WHERE q.run_id = %s
           GROUP BY j.verdict
           ORDER BY total DESC""",
        (run_id,),
    )
    _print_rows("Deterministic metadata_mismatch parity", rows,
                ["verdict", "total", "det_caught", "det_pct"])


def sample_wrong_speaker(run_id: str, limit: int = 3) -> None:
    rows = pg.execute(
        """SELECT j.paragraph_text, j.cited_indices, j.rationale, j.metadata_mismatch
           FROM eval_judgments j
           JOIN eval_questions q ON q.id = j.question_id
           WHERE q.run_id = %s AND j.verdict = 'wrong_speaker'
           ORDER BY j.metadata_mismatch IS NULL, random()
           LIMIT %s""",
        (run_id, limit),
    )
    print(f"\n--- Sample wrong_speaker paragraphs (n={len(rows)}) ---")
    for i, r in enumerate(rows, 1):
        print(f"\n[{i}] CITED: {r['cited_indices']}")
        print(f"    PARA:     {(r['paragraph_text'] or '')[:300]}")
        print(f"    RATIONALE:{(r['rationale'] or '')[:220]}")
        print(f"    MISMATCH: {r['metadata_mismatch'] or '(none — judge-only catch)'}")


def compare_runs(run_ids: List[str]) -> None:
    placeholders = ", ".join(["%s"] * len(run_ids))
    rows = pg.execute(
        f"""SELECT j.verdict, q.run_id::text AS run_id, COUNT(*) AS n
            FROM eval_judgments j
            JOIN eval_questions q ON q.id = j.question_id
            WHERE q.run_id IN ({placeholders})
            GROUP BY j.verdict, q.run_id""",
        tuple(run_ids),
    )

    # Fetch labels for readable headers
    label_rows = pg.execute(
        f"SELECT id::text, label FROM eval_runs WHERE id IN ({placeholders})",
        tuple(run_ids),
    )
    labels = {r["id"]: r["label"] or r["id"][:8] for r in label_rows}

    by_run: Dict[str, Dict[str, int]] = {rid: {} for rid in run_ids}
    for r in rows:
        by_run[r["run_id"]][r["verdict"]] = r["n"]
    verdicts = sorted({v for d in by_run.values() for v in d.keys()})
    totals = {rid: sum(by_run[rid].values()) or 1 for rid in run_ids}

    # Header
    print("\n--- Verdict comparison ---")
    base_id = run_ids[0]
    header = f"{'verdict':22s}"
    for rid in run_ids:
        col = f"{labels[rid][:14]} n"
        header += f"  {col:>9s}  {'%':>6s}"
    header += f"  {'Δpp vs base':>11s}" * (len(run_ids) - 1)
    print(header)
    print("-" * len(header))

    for v in verdicts:
        line = f"{v:22s}"
        base_pct = 100.0 * by_run[base_id].get(v, 0) / totals[base_id]
        for rid in run_ids:
            n = by_run[rid].get(v, 0)
            pct = 100.0 * n / totals[rid]
            line += f"  {n:9d}  {pct:5.1f}%"
        for rid in run_ids[1:]:
            pct = 100.0 * by_run[rid].get(v, 0) / totals[rid]
            line += f"  {pct - base_pct:+10.1f}"
        print(line)

    # Totals row
    line = f"{'TOTAL':22s}"
    for rid in run_ids:
        line += f"  {totals[rid]:9d}  {'100.0':>6s}"
    print(line)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", help="Run UUID; defaults to most recent run")
    ap.add_argument("--compare", nargs="+", metavar="RUN_ID",
                    help="Compare verdict distributions of two or more runs (first is the base)")
    args = ap.parse_args()

    if args.compare:
        compare_runs(args.compare)
        return

    run_id = args.run_id or _latest_run_id()
    if not run_id:
        print("No runs found.")
        return

    run_summary(run_id)
    verdict_distribution(run_id)
    by_complexity(run_id)
    tool_bad_pct(run_id)
    citations_per_paragraph(run_id)
    good_vs_bad_sources(run_id)
    latency_summary(run_id)
    metadata_mismatch_parity(run_id)
    llm_events(run_id)
    sample_wrong_speaker(run_id)


if __name__ == "__main__":
    main()
