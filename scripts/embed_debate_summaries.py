"""
Backfill summary_embedding for all debates that have a summary but no embedding yet.

Run once after applying add_debate_summary_embedding.sql, and again whenever
needed to catch debates added by debates.py since the last run.

Usage:
    python scripts/embed_debate_summaries.py
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bootstrap  # noqa: E402,F401  — sets cwd and sys.path to the project root
from postgres_client import pg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

EMBED_BATCH = 20
LOG_INTERVAL = 200


def embed_debate_summaries() -> int:
    missing = pg.execute(
        """
        SELECT debate, summary
        FROM debates
        WHERE summary IS NOT NULL AND summary != ''
          AND summary_embedding IS NULL
        ORDER BY debate
        """
    )
    total = len(missing)
    logger.info(f"Found {total} debates needing summary embeddings")

    if not total:
        return 0

    processed = 0
    for i in range(0, total, EMBED_BATCH):
        batch = missing[i : i + EMBED_BATCH]
        texts = [row["summary"] for row in batch]
        embeddings = pg.make_embeddings(texts)
        params = [(emb, row["debate"]) for row, emb in zip(batch, embeddings, strict=False)]
        pg.execute_many(
            "UPDATE debates SET summary_embedding = %s WHERE debate = %s",
            params,
        )
        processed += len(batch)
        if processed % LOG_INTERVAL == 0 or processed == total:
            logger.info(f"  {processed}/{total} embeddings written")

    logger.info(f"Done. Embedded {processed} debate summaries.")
    return processed


if __name__ == "__main__":
    embed_debate_summaries()
