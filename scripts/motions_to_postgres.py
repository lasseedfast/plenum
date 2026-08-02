"""
Läser in motioner från JSON-filer (dokumentstatus-kuvert) till PostgreSQL.

Används av sync_motions.py (update_folder) och kan köras direkt för att
(om)ladda alla mappar i motioner/:

    python scripts/motions_to_postgres.py
"""

import json
import logging
import os
import sys

os.chdir("/home/lasse/riksdagen")
sys.path.append("/home/lasse/riksdagen")

from bs4 import BeautifulSoup
from postgres_client import pg
from scripts.documents_to_postgres import _parse_date, clean_speaker_name

logging.basicConfig(level=logging.WARNING, format="%(asctime)s - %(levelname)s - %(message)s")

# Rader kortare än så räknas som textlösa (inskannad PDF med stubb-HTML)
MIN_TEXT_LEN = 200


def _as_list(v) -> list:
    """Riksdagens XML→JSON: ett barn blir dict, flera blir list, inget blir None."""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _clean(v):
    """API:et serialiserar null som strängen 'None'."""
    if v is None or v == "None":
        return None
    return v


def html_to_text(html: str) -> str:
    """Extraherar ren text ur dokumentets html-fält (inleds med stort <style>-block)."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["style", "script"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [line.strip() for line in text.split("\n")]
    out = []
    for line in lines:
        if line:
            out.append(line)
        elif out and out[-1] != "":
            out.append("")
    return "\n".join(out).strip()


def parse_file(path: str) -> dict | None:
    """Parsar en motions-JSON till en dict med rader för motions + motion_authors."""
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    ds = data["dokumentstatus"]
    dok = ds["dokument"]

    dok_id = _clean(dok.get("dok_id"))
    if not dok_id:
        return None
    if _clean(dok.get("doktyp")) not in (None, "mot"):
        return None  # defensivt; mot-arkiven ska bara innehålla motioner

    rm = _clean(dok.get("rm")) or ""
    try:
        year = int(rm[:4])
    except (ValueError, TypeError):
        year = None

    text = html_to_text(_clean(dok.get("html")) or "")
    has_text = len(text) >= MIN_TEXT_LEN

    authors = []
    parties: list[str] = []
    author_names: list[str] = []
    intressenter = _as_list((ds.get("dokintressent") or {}).get("intressent"))
    for i, x in enumerate(intressenter):
        if not isinstance(x, dict):
            continue
        namn = clean_speaker_name(_clean(x.get("namn")) or "")
        partibet = _clean(x.get("partibet"))
        authors.append(
            (dok_id, i, _clean(x.get("intressent_id")), namn, partibet, _clean(x.get("roll")))
        )
        if namn:
            author_names.append(namn)
        if partibet and partibet not in parties:
            parties.append(partibet)

    forslag = _as_list((ds.get("dokforslag") or {}).get("forslag"))
    # Condensed, high-signal yrkanden: one row per <forslag> for FTS + embeddings.
    yrkanden = []
    lydelser = []
    for i, f in enumerate(forslag):
        if not isinstance(f, dict):
            continue
        lydelse = _clean(f.get("lydelse"))
        if not lydelse:
            continue
        lydelser.append(lydelse)
        yrkanden.append((
            f"{dok_id}:{i}", dok_id, i, _clean(f.get("nummer")), lydelse,
            _clean(f.get("utskottet")), _clean(f.get("kammaren")), _clean(f.get("behandlas_i")),
        ))
    forslag_text = " ".join(lydelser) or None

    bilagor = _as_list((ds.get("dokbilaga") or {}).get("bilaga"))
    pdf_url = None
    for b in bilagor:
        if isinstance(b, dict) and (b.get("filtyp") or "").lower() == "pdf":
            pdf_url = _clean(b.get("fil_url"))
            break

    return {
        "dok_id": dok_id,
        "hangar_id": _clean(dok.get("hangar_id")),
        "rm": rm or None,
        "beteckning": _clean(dok.get("beteckning")),
        "subtyp": _clean(dok.get("subtyp")),
        "organ": _clean(dok.get("organ")),
        "status": _clean(dok.get("status")),
        "datum": _parse_date(_clean(dok.get("datum")) or ""),
        "systemdatum": _clean(dok.get("systemdatum")),
        "publicerad": _clean(dok.get("publicerad")),
        "year": year,
        "titel": _clean(dok.get("titel")),
        "undertitel": _clean(dok.get("subtitel")) or _clean(dok.get("undertitel")),
        "text": text,
        "forslag_text": forslag_text,
        "has_text": has_text,
        "dokument_url_text": _clean(dok.get("dokument_url_text")),
        "dokument_url_html": _clean(dok.get("dokument_url_html")),
        "pdf_url": pdf_url,
        "parties": parties,
        "author_names": author_names,
        "forslag": json.dumps(forslag, ensure_ascii=False) if forslag else None,
        "bilagor": json.dumps(bilagor, ensure_ascii=False) if bilagor else None,
        "num_yrkanden": len(forslag),
        "authors": authors,
        "yrkanden": yrkanden,
    }


def process_folder(folder_path: str, already_processed: set[str] = frozenset()) -> list[dict]:
    """Parsar JSON-filer i folder_path, returnerar motions-dicts (hoppar över kända ID:n)."""
    docs = []
    for file in os.listdir(folder_path):
        if not file.endswith(".json"):
            continue
        # Filnamnet är dok_id (gemener) — hoppa över kända utan att parsa
        if file[:-5].upper() in already_processed:
            continue
        try:
            doc = parse_file(os.path.join(folder_path, file))
            if doc is None or doc["dok_id"] in already_processed:
                continue
            docs.append(doc)
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            logging.warning(f"Skipping {file}: {e}")
    return docs


_UPSERT_SQL = """
INSERT INTO motions (
    dok_id, hangar_id, rm, beteckning, subtyp, organ, status,
    datum, systemdatum, publicerad, year,
    titel, undertitel, text, forslag_text, has_text,
    dokument_url_text, dokument_url_html, pdf_url,
    parties, author_names, forslag, bilagor, num_yrkanden
) VALUES %s
ON CONFLICT (dok_id) DO NOTHING
"""
# Framtida förbättring: DO UPDATE ... WHERE EXCLUDED.systemdatum > motions.systemdatum
# för att plocka upp utskottsutfall på redan inlästa motioner.

_AUTHORS_SQL = """
INSERT INTO motion_authors (dok_id, ordinal, intressent_id, namn, partibet, roll)
VALUES %s
ON CONFLICT (dok_id, ordinal) DO NOTHING
"""

_YRKANDEN_SQL = """
INSERT INTO motion_yrkanden (id, dok_id, ordinal, nummer, lydelse, utskottet, kammaren, behandlas_i)
VALUES %s
ON CONFLICT (id) DO NOTHING
"""


def _doc_to_row(doc: dict) -> tuple:
    return (
        doc["dok_id"],
        doc["hangar_id"],
        doc["rm"],
        doc["beteckning"],
        doc["subtyp"],
        doc["organ"],
        doc["status"],
        doc["datum"],
        doc["systemdatum"],
        doc["publicerad"],
        doc["year"],
        doc["titel"],
        doc["undertitel"],
        doc["text"],
        doc["forslag_text"],
        doc["has_text"],
        doc["dokument_url_text"],
        doc["dokument_url_html"],
        doc["pdf_url"],
        doc["parties"],
        doc["author_names"],
        doc["forslag"],
        doc["bilagor"],
        doc["num_yrkanden"],
    )


def insert_docs(docs: list[dict]) -> None:
    if not docs:
        return
    rows = [_doc_to_row(d) for d in docs]
    pg.execute_values(_UPSERT_SQL, rows)
    author_rows = [row for d in docs for row in d["authors"]]
    if author_rows:
        pg.execute_values(_AUTHORS_SQL, author_rows)
    yrkande_rows = [row for d in docs for row in d["yrkanden"]]
    if yrkande_rows:
        pg.execute_values(_YRKANDEN_SQL, yrkande_rows)


def update_folder(path: str, already_processed: set[str] = None) -> int:
    """
    Upsertar motioner från JSON-filer i path till PostgreSQL.
    Returnerar antalet nya motioner.
    """
    if already_processed is None:
        rows = pg.execute("SELECT dok_id FROM motions")
        already_processed = {row["dok_id"] for row in rows}

    docs = process_folder(path, already_processed)
    insert_docs(docs)
    return len(docs)


if __name__ == "__main__":
    existing = {row["dok_id"] for row in pg.execute("SELECT dok_id FROM motions")}
    total = 0
    for folder in sorted(os.listdir("motioner")):
        path = os.path.join("/home/lasse/riksdagen/motioner", folder)
        if not os.path.isdir(path):
            continue
        print(f"Processing {folder} …", end=" ", flush=True)
        docs = process_folder(path, already_processed=existing)
        insert_docs(docs)
        existing |= {d["dok_id"] for d in docs}
        total += len(docs)
        print(f"{len(docs)} inserted")
    print(f"\nTotal: {total} new motions inserted")
