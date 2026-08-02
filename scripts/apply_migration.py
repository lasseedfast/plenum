#!/usr/bin/env python3
"""Apply a SQL migration from _postgres/migrations/ using the app's own
connection settings (PG_HOST/PG_DB/PG_USER/… — see _postgres/_postgres.py).

    python scripts/apply_migration.py add_user_settings
    python scripts/apply_migration.py _postgres/migrations/add_user_settings.sql
    python scripts/apply_migration.py add_user_settings --dry-run

The migrations in this repo are written to be idempotent (ADD COLUMN IF NOT
EXISTS, DROP CONSTRAINT IF EXISTS ...), so re-running one is safe. Statements
are executed in file order and each is echoed, so a partial failure tells you
exactly where it stopped.

Splitting is naive — one statement per top-level ';' — which is fine for the
DDL here but would mangle a function body containing semicolons. Comments are
stripped per line rather than per chunk, so a statement preceded by a comment
block is not silently skipped.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "_postgres" / "migrations"

sys.path.insert(0, str(REPO_ROOT))


def resolve(name: str) -> Path:
    """Accept a bare migration name, a filename, or any path to a .sql file."""
    for candidate in (Path(name), MIGRATIONS_DIR / name, MIGRATIONS_DIR / f"{name}.sql"):
        if candidate.is_file():
            return candidate
    available = "\n  ".join(sorted(p.stem for p in MIGRATIONS_DIR.glob("*.sql")))
    raise SystemExit(f"No such migration: {name!r}\n\nAvailable:\n  {available}")


def statements(sql: str) -> list[str]:
    """Split into executable statements, dropping comment-only lines."""
    lines = [ln for ln in sql.splitlines() if not ln.lstrip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("migration", help="Migration name (e.g. add_user_settings) or path")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the statements without executing them")
    args = ap.parse_args()

    path = resolve(args.migration)
    stmts = statements(path.read_text())
    print(f"{path.relative_to(REPO_ROOT)}: {len(stmts)} statement(s)")

    if args.dry_run:
        for i, stmt in enumerate(stmts, 1):
            print(f"\n-- [{i}/{len(stmts)}]\n{stmt};")
        return 0

    from postgres_client import pg

    for i, stmt in enumerate(stmts, 1):
        first_line = stmt.splitlines()[0][:90]
        print(f"  [{i}/{len(stmts)}] {first_line}")
        pg.execute_void(stmt)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
