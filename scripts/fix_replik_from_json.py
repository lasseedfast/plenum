"""
Fix talks.replik by reading the ground-truth value from the JSON source files.

Background:
  documents_to_postgres.py uses ON CONFLICT DO NOTHING, so talks ingested before
  replik was handled correctly were never updated. This script does the corrective
  pass: reads every JSON file, finds talks where the stored replik differs from the
  file, updates the DB, then re-assigns debate IDs for the affected dates so that
  proper multi-speaker debates are created.

Steps:
  1. Scan all talks/ subfolders and build {talk_id → replik_bool} from JSON files.
  2. Query DB for talks where replik is currently False.
  3. Update any talk whose JSON says True.
  4. For each affected date, clear talks.debate and re-run make_debate_ids().
  5. Delete stale single-talk debate rows whose IDs no longer exist in talks.debate.

Usage:
  python scripts/fix_replik_from_json.py
"""

import logging
import os
import sys
from collections import defaultdict

os.chdir("/home/lasse/riksdagen")
sys.path.append("/home/lasse/riksdagen")

from postgres_client import pg
from scripts.documents_to_postgres import process_folder
from scripts.debates import assign_debate_ids

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

TALKS_DIR = "/home/lasse/riksdagen/talks"
BATCH_SIZE = 500


def build_replik_map() -> dict[str, bool]:
    """Return {talk_id: replik} for every JSON file in talks/."""
    replik_map: dict[str, bool] = {}
    folders = sorted(os.listdir(TALKS_DIR))
    for i, folder in enumerate(folders):
        path = os.path.join(TALKS_DIR, folder)
        if not os.path.isdir(path):
            continue
        docs = process_folder(path)
        for doc in docs:
            tid = doc.get("id")
            if tid:
                replik_map[tid] = bool(doc.get("replik", False))
        if (i + 1) % 5 == 0 or (i + 1) == len(folders):
            logger.info(f"  Scanned {i+1}/{len(folders)} folders ({len(replik_map):,} talks)")
    return replik_map


def fix_replik(replik_map: dict[str, bool]) -> set[str]:
    """
    Update talks where DB replik=False but JSON says True.
    Returns the set of affected talk IDs.
    """
    # Only need to check talks where DB currently says False
    db_false = pg.execute("SELECT id FROM talks WHERE replik = false")
    db_false_ids = {row["id"] for row in db_false}
    logger.info(f"Talks with replik=False in DB: {len(db_false_ids):,}")

    to_fix = [tid for tid in db_false_ids if replik_map.get(tid) is True]
    logger.info(f"Talks to correct (JSON says True, DB says False): {len(to_fix):,}")

    if not to_fix:
        return set()

    for i in range(0, len(to_fix), BATCH_SIZE):
        batch = to_fix[i : i + BATCH_SIZE]
        pg.execute_many(
            "UPDATE talks SET replik = true WHERE id = %s",
            [(tid,) for tid in batch],
        )
        if (i + BATCH_SIZE) % 5000 == 0 or i + BATCH_SIZE >= len(to_fix):
            logger.info(f"  Updated {min(i+BATCH_SIZE, len(to_fix)):,}/{len(to_fix):,}")

    return set(to_fix)


def get_affected_dates(fixed_ids: set[str]) -> list[str]:
    """Return distinct dates for the talks whose replik was corrected."""
    if not fixed_ids:
        return []
    rows = pg.execute(
        "SELECT DISTINCT datum::text AS datum FROM talks WHERE id = ANY(%s::text[])",
        (list(fixed_ids),),
    )
    return [row["datum"] for row in rows if row.get("datum")]


def reassign_debate_ids(dates: list[str]) -> None:
    """
    For each date, clear talks.debate and re-assign using assign_debate_ids().
    This is the same logic as make_debate_ids() but scoped to the given dates.
    """
    logger.info(f"Re-assigning debate IDs for {len(dates):,} dates …")
    for i, date in enumerate(sorted(dates)):
        # Clear existing debate assignments for this date
        pg.execute_void(
            "UPDATE talks SET debate = NULL WHERE datum = %s::date",
            (date,),
        )

        talks = pg.execute(
            """
            SELECT id, replik
            FROM talks
            WHERE datum = %s::date
            ORDER BY anforande_nummer ASC
            """,
            (date,),
        )
        if not talks:
            continue

        updated = assign_debate_ids(list(talks), date)
        pg.execute_many(
            "UPDATE talks SET debate = %s WHERE id = %s",
            [(doc["debate"], doc["id"]) for doc in updated],
        )

        if (i + 1) % 100 == 0 or (i + 1) == len(dates):
            logger.info(f"  {i+1}/{len(dates)} dates processed")


def remove_stale_debate_rows() -> int:
    """
    Delete debate rows whose debate ID no longer appears in talks.debate.
    This cleans up the old one-per-talk debate rows that became invalid after
    debate IDs were re-assigned.
    """
    result = pg.execute(
        """
        DELETE FROM debates
        WHERE debate NOT IN (SELECT DISTINCT debate FROM talks WHERE debate IS NOT NULL)
        RETURNING debate
        """
    )
    count = len(result) if result else 0
    logger.info(f"Removed {count:,} stale debate rows")
    return count


def main() -> None:
    logger.info("=== Step 1: Build replik map from JSON files ===")
    replik_map = build_replik_map()
    logger.info(f"Total talks in JSON files: {len(replik_map):,}")

    logger.info("=== Step 2: Fix replik in DB ===")
    fixed_ids = fix_replik(replik_map)
    if not fixed_ids:
        logger.info("Nothing to fix — replik is already correct.")
        return

    logger.info("=== Step 3: Find affected dates ===")
    dates = get_affected_dates(fixed_ids)
    logger.info(f"Affected dates: {len(dates):,}")

    logger.info("=== Step 4: Re-assign debate IDs ===")
    reassign_debate_ids(dates)

    logger.info("=== Step 5: Remove stale debate rows ===")
    remove_stale_debate_rows()

    logger.info("=== Done! ===")
    logger.info("Next: run 'python scripts/debates.py' (or deploy_debate_embeddings.sh)")
    logger.info("to generate summaries for the newly grouped multi-talk debates.")


if __name__ == "__main__":
    main()
