"""Command-line entry point for ingesting a parliament's open data.

    python -m ingest.cli fetch --source documents --range 2022-2025
    python -m ingest.cli load  --source documents
    python -m ingest.cli sync  --source all

`fetch` downloads, `load` adapts and upserts what is on disk, `sync` does both.
Everything is resumable: archives already unpacked are skipped, and inserts use
ON CONFLICT DO NOTHING, so re-running after an interruption is safe.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest import pipeline  # noqa: E402
from ingest.adapters import load_adapter  # noqa: E402
from parliament import PARLIAMENT  # noqa: E402

SOURCES = ("speeches", "documents", "people")


def _adapter():
    module_path = PARLIAMENT.sources.get("adapter")
    if not module_path:
        raise SystemExit("parliament.yaml has no `sources.adapter`. See docs/PORTING.md.")
    return load_adapter(module_path)


def _resolve(source: str) -> list[str]:
    if source == "all":
        return [s for s in SOURCES if s in PARLIAMENT.sources]
    if source not in SOURCES:
        raise SystemExit(f"Unknown source {source!r}; expected one of {SOURCES + ('all',)}")
    return [source]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ingest", description=__doc__)
    parser.add_argument("command", choices=("fetch", "load", "sync"))
    parser.add_argument("--source", default="all",
                        help=f"one of {', '.join(SOURCES)}, or 'all' (default)")
    parser.add_argument("--range", dest="ranges", action="append",
                        help="archive range to fetch, e.g. 2022-2025. Repeatable. "
                             "Defaults to the ranges in parliament.yaml.")
    parser.add_argument("--limit", type=int,
                        help="stop after this many records — useful for a smoke test")
    args = parser.parse_args(argv)

    adapter = _adapter()
    print(f"parliament: {PARLIAMENT.meta.get('name')}  adapter: {PARLIAMENT.sources['adapter']}")

    exit_code = 0
    for source in _resolve(args.source):
        print(f"\n── {source} ──")
        try:
            if args.command in ("fetch", "sync"):
                pipeline.fetch(source, args.ranges)
            if args.command in ("load", "sync"):
                counts = pipeline.load(source, adapter.adapt, limit=args.limit)
                print(f"  read {counts['read']}, wrote {counts['written']}, "
                      f"skipped {counts['skipped']}")
        except Exception as exc:
            # One failing source should not abandon the others; a daily sync that
            # aborts because one archive moved is worse than a partial one.
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            exit_code = 1

    if args.command in ("load", "sync"):
        print("\nNext: python scripts/make_embeddings.py")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
