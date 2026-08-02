"""
Synkroniserar nya motioner från riksdagen.se till PostgreSQL-databasen.

Pipeline (körs dagligen via systemd timer, se etc/riksdagen-motions-sync.*):
  1. Ladda ned arkivet för aktuell fyraårsperiod (ersätter tidigare nerladdning)
  2. Infoga nya motioner i PostgreSQL (hoppar över redan existerande via ON CONFLICT)
  3. Bygg embeddings för motioner som saknar chunks

Alternativ till steg 1 om den dagliga nerladdningen (~50-110 MB) blir ett problem:
dokumentlista-API:et (https://data.riksdagen.se/dokumentlista/?doktyp=mot&sort=systemdatum
&sortorder=desc&utformat=json) kan pagineras tills redan inlästa dok_id påträffas.

Kör manuellt: python scripts/sync_motions.py
"""
from pathlib import Path

import logging
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bootstrap  # noqa: E402,F401  — sets cwd and sys.path to the project root

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def sync() -> None:
    """Kör hela sync-pipelinen för motioner."""
    logger.info("=== Starting daily motions sync ===")

    # --- Steg 1: Ladda ned aktuell period ---
    from scripts.download_motions import download_range, get_current_range
    from scripts.sync_talks import get_current_session_year

    current_range = get_current_range(get_current_session_year())
    logger.info(f"Current range: {current_range}")
    dir_path = download_range(current_range, force=True)

    # --- Steg 2: Infoga nya motioner ---
    logger.info("Stage 2: Inserting new motions into PostgreSQL...")
    from scripts.motions_to_postgres import update_folder

    new_motions = update_folder(os.path.abspath(dir_path))
    logger.info(f"Stage 2 complete: {new_motions} new motions inserted")

    # --- Steg 3: Chunk + bygg embeddings (fulltext + yrkanden) ---
    logger.info("Stage 3: Chunking and embedding new motions...")
    from scripts.make_embeddings import make_motion_embeddings, make_yrkande_embeddings

    total_chunks = make_motion_embeddings()
    total_yrkanden = make_yrkande_embeddings()
    logger.info(f"Stage 3 complete: {total_chunks} chunks + {total_yrkanden} yrkanden embedded")

    logger.info("=== Motions sync complete ===")


if __name__ == "__main__":
    sync()
