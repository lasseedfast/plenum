"""
Laddar ned motioner från riksdagens öppna data som ZIP-arkiv med JSON-filer.

Arkiven är uppdelade i fyraårsperioder (förankrade vid 1998; allt före ligger
i 1990-1997) och uppdateras dagligen:

    https://data.riksdagen.se/dataset/dokument/mot-{range}.json.zip

Varje arkiv innehåller en JSON-fil per motion (dokumentstatus-kuvert med
metadata + fulltext som HTML). Extraheras till motioner/mot-{range}/.

Backfill (alla perioder 1990→):  python scripts/download_motions.py
Används av sync_motions.py för daglig uppdatering av aktuell period.
"""

import logging
import os
import sys
from io import BytesIO
from urllib.request import urlopen
from zipfile import ZipFile

os.chdir("/home/lasse/riksdagen")
sys.path.append("/home/lasse/riksdagen")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

RANGES = [
    "1990-1997",
    "1998-2001",
    "2002-2005",
    "2006-2009",
    "2010-2013",
    "2014-2017",
    "2018-2021",
    "2022-2025",
]
URL_TEMPLATE = "https://data.riksdagen.se/dataset/dokument/mot-{r}.json.zip"
MOTIONS_DIR = "motioner"


def get_current_range(session_year: int) -> str:
    """Fyraårsperiod förankrad vid 1998; allt före 1998 ligger i 1990-1997."""
    if session_year < 1998:
        return "1990-1997"
    start = session_year - ((session_year - 1998) % 4)
    return f"{start}-{start + 3}"


def download_range(r: str, force: bool = False) -> str:
    """
    Laddar ned och extraherar arkivet för en period till motioner/mot-{r}/.
    Hoppar över om mappen redan är ifylld, om inte force=True (töms först).
    """
    dir_path = os.path.join(MOTIONS_DIR, f"mot-{r}")

    if os.path.exists(dir_path) and os.listdir(dir_path):
        if not force:
            logger.info(f"Skipping {dir_path}, already exists and is not empty.")
            return dir_path
        for f in os.listdir(dir_path):
            os.remove(os.path.join(dir_path, f))

    os.makedirs(dir_path, exist_ok=True)
    url = URL_TEMPLATE.format(r=r)
    logger.info(f"Downloading {url} → {dir_path}")

    with urlopen(url) as resp:
        with ZipFile(BytesIO(resp.read())) as zf:
            zf.extractall(dir_path)

    count = len(os.listdir(dir_path))
    logger.info(f"Extracted {count} files to {dir_path}")
    return dir_path


def download_all(force: bool = False) -> None:
    for r in RANGES:
        download_range(r, force=force)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        download_range(sys.argv[1], force="--force" in sys.argv)
    else:
        download_all()
