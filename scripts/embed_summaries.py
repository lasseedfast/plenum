"""
Backfill summary_embedding for all speeches that have a summary but no embedding yet.

Run once after applying the migration add_summary_embedding.sql, and again
whenever needed to catch any speeches the summarize_and_tag script missed.

Usage:
    python scripts/embed_summaries.py
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


def embed_summaries() -> int:
    missing = pg.execute(
        """
        SELECT id, summary
        FROM speeches
        WHERE summary IS NOT NULL AND summary != ''
          AND summary_embedding IS NULL
        ORDER BY id
        """
    )
    total = len(missing)
    logger.info(f"Found {total} speeches needing summary embeddings")

    if not total:
        return 0

    processed = 0
    for i in range(0, total, EMBED_BATCH):
        batch = missing[i : i + EMBED_BATCH]
        texts = [row["summary"] for row in batch]
        embeddings = pg.make_embeddings(texts)
        params = [(emb, row["id"]) for row, emb in zip(batch, embeddings, strict=False)]
        pg.execute_many(
            "UPDATE speeches SET summary_embedding = %s WHERE id = %s",
            params,
        )
        processed += len(batch)
        if processed % LOG_INTERVAL == 0 or processed == total:
            logger.info(f"  {processed}/{total} embeddings written")

    logger.info(f"Done. Embedded {processed} summaries.")
    return processed


if __name__ == "__main__":
    embed_summaries()
