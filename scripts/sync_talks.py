"""
Synkroniserar nya anföranden från riksdagen.se till PostgreSQL-databasen.

Ersätter den ArangoDB-baserade versionen.

Pipeline (körs dagligen via systemd timer):
  1. Ladda ned årets anföranden från riksdagen.se (ersätter tidigare nerladdning)
  2. Infoga nya anföranden i PostgreSQL (hoppar över redan existerande via ON CONFLICT)
  3. Tilldela debatt-ID:n till anföranden som saknar det
  4. Bygg embeddings för anföranden som saknar chunks
  5. Generera sammanfattningar för datum som saknar summary

Kör manuellt: python scripts/sync_talks.py
"""

import logging
import os
import sys
from datetime import datetime
from io import BytesIO
from urllib.request import urlopen
from zipfile import ZipFile

os.chdir("/home/lasse/riksdagen")
sys.path.append("/home/lasse/riksdagen")

from postgres_client import pg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

SYSTEM_MESSAGE = """Din uppgift är att sammanfatta debatter i Sveriges riksdag.
Du kommer först att få enskilda tal som du ska sammanfatta var för sig, efter det ska du sammanfatta hela debatten.
Sammanfattningarna ska vara på svenska och vara koncisa och informativa.
Det är viktigt att du förstår vad som är kärnan i varje tal och debatt, fokusera därför på de argument och sakförhållanden som framförs.
"""


def get_current_session_year() -> int:
    """
    Returnerar startåret för aktuell riksdagssession.
    Riksdagssessionen löper september–augusti.
    """
    now = datetime.now()
    return now.year if now.month >= 9 else now.year - 1


def download_current_year(session_year: int) -> str:
    """Laddar ned och extraherar ZIP-arkivet för angiven riksdagssession."""
    second_part = str(session_year + 1)[2:]
    url = f"https://data.riksdagen.se/dataset/anforande/anforande-{session_year}{second_part}.json.zip"
    folder_name = f"anforande-{session_year}{second_part}"
    dir_path = os.path.join("talks", folder_name)

    logger.info(f"Downloading {url} → {dir_path}")
    os.makedirs(dir_path, exist_ok=True)

    for f in os.listdir(dir_path):
        os.remove(os.path.join(dir_path, f))

    with urlopen(url) as resp:
        with ZipFile(BytesIO(resp.read())) as zf:
            zf.extractall(dir_path)

    count = len(os.listdir(dir_path))
    logger.info(f"Extracted {count} files to {dir_path}")
    return dir_path


def get_unsummarized_dates() -> list[str]:
    """Hämtar datum som har anföranden utan sammanfattning."""
    rows = pg.execute(
        "SELECT DISTINCT datum::text AS datum FROM talks WHERE summary IS NULL ORDER BY datum"
    )
    dates = sorted(row["datum"] for row in rows if row.get("datum"))
    logger.info(f"Found {len(dates)} dates with unsummarized talks")
    return dates


def sync() -> None:
    """Kör hela sync-pipelinen."""
    logger.info("=== Starting daily riksdagen sync ===")

    # --- Steg 1: Ladda ned ---
    session_year = get_current_session_year()
    logger.info(f"Current session year: {session_year}/{session_year + 1}")
    dir_path = download_current_year(session_year)

    # --- Steg 2: Infoga nya anföranden ---
    logger.info("Stage 2: Inserting new talks into PostgreSQL...")
    from scripts.documents_to_postgres import update_folder

    new_talks = update_folder(os.path.abspath(dir_path))
    logger.info(f"Stage 2 complete: {new_talks} new talks inserted")

    # --- Steg 3: Tilldela debatt-ID:n ---
    logger.info("Stage 3: Assigning debate IDs to talks missing them...")
    from scripts.debates import make_debate_ids

    make_debate_ids()
    logger.info("Stage 3 complete")

    # --- Steg 4: Chunk + bygg embeddings ---
    logger.info("Stage 4: Chunking and embedding new talks...")
    from scripts.make_embeddings import make_embeddings

    total_chunks = make_embeddings()
    logger.info(f"Stage 4 complete: {total_chunks} chunks created")

    # --- Steg 5: Generera sammanfattningar ---
    new_dates = get_unsummarized_dates()
    if new_dates:
        logger.info(f"Stage 5: Generating summaries for {len(new_dates)} dates...")
        from scripts.debates import process_debate_date

        for date in new_dates:
            process_debate_date(date, SYSTEM_MESSAGE)
        logger.info(f"Stage 5 complete: summaries generated for {len(new_dates)} dates")
    else:
        logger.info("Stage 5: No unsummarized dates, skipping")

    logger.info("=== Sync complete ===")


if __name__ == "__main__":
    sync()
