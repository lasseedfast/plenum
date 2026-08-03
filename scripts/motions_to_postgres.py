"""
Läser in motioner från JSON-filer (dokumentstatus-kuvert) till PostgreSQL.

Används av sync_motions.py (update_folder) och kan köras direkt för att
(om)ladda alla mappar i motioner/:

    python scripts/motions_to_postgres.py
"""
from pathlib import Path

import json
import logging
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bootstrap  # noqa: E402,F401  — sets cwd and sys.path to the project root

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
    """Parsar en documents-JSON till en dict med rader för documents + document_authors."""
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    ds = data["dokumentstatus"]
    dok = ds["dokument"]

    doc_id = _clean(dok.get("doc_id"))
    if not doc_id:
        return None
    if _clean(dok.get("doktyp")) not in (None, "mot"):
        return None  # defensivt; mot-arkiven ska bara innehålla motioner

    session_label = _clean(dok.get("session_label")) or ""
    try:
        year = int(session_label[:4])
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
        name = clean_speaker_name(_clean(x.get("name")) or "")
        party = _clean(x.get("party"))
        authors.append(
            (doc_id, i, _clean(x.get("person_id")), name, party, _clean(x.get("role")))
        )
        if name:
            author_names.append(name)
        if party and party not in parties:
            parties.append(party)

    proposals_raw = _as_list((ds.get("dokforslag") or {}).get("proposals_raw"))
    # Condensed, high-signal yrkanden: one row per <proposals_raw> for FTS + embeddings.
    yrkanden = []
    lydelser = []
    for i, f in enumerate(proposals_raw):
        if not isinstance(f, dict):
            continue
        text = _clean(f.get("text"))
        if not text:
            continue
        lydelser.append(text)
        yrkanden.append((
            f"{doc_id}:{i}", doc_id, i, _clean(f.get("number")), text,
            _clean(f.get("committee_recommendation")), _clean(f.get("chamber_decision")), _clean(f.get("handled_in")),
        ))
    proposals_text = " ".join(lydelser) or None

    attachments = _as_list((ds.get("dokbilaga") or {}).get("bilaga"))
    url_pdf = None
    for b in attachments:
        if isinstance(b, dict) and (b.get("filtyp") or "").lower() == "pdf":
            url_pdf = _clean(b.get("fil_url"))
            break

    return {
        "doc_id": doc_id,
        "source_record_id": _clean(dok.get("source_record_id")),
        "session_label": session_label or None,
        "designation": _clean(dok.get("designation")),
        "subtype": _clean(dok.get("subtype")),
        "committee": _clean(dok.get("committee")),
        "status": _clean(dok.get("status")),
        "date": _parse_date(_clean(dok.get("date")) or ""),
        "source_updated_at": _clean(dok.get("source_updated_at")),
        "published_at": _clean(dok.get("published_at")),
        "year": year,
        "title": _clean(dok.get("title")),
        "subtitle": _clean(dok.get("subtitel")) or _clean(dok.get("subtitle")),
        "text": text,
        "proposals_text": proposals_text,
        "has_text": has_text,
        "url_text": _clean(dok.get("url_text")),
        "url_html": _clean(dok.get("url_html")),
        "url_pdf": url_pdf,
        "parties": parties,
        "author_names": author_names,
        "proposals_raw": json.dumps(proposals_raw, ensure_ascii=False) if proposals_raw else None,
        "attachments": json.dumps(attachments, ensure_ascii=False) if attachments else None,
        "num_proposals": len(proposals_raw),
        "authors": authors,
        "yrkanden": yrkanden,
    }


def process_folder(folder_path: str, already_processed: set[str] = frozenset()) -> list[dict]:
    """Parsar JSON-filer i folder_path, returnerar documents-dicts (hoppar över kända ID:n)."""
    docs = []
    for file in os.listdir(folder_path):
        if not file.endswith(".json"):
            continue
        # Filnamnet är doc_id (gemener) — hoppa över kända utan att parsa
        if file[:-5].upper() in already_processed:
            continue
        try:
            doc = parse_file(os.path.join(folder_path, file))
            if doc is None or doc["doc_id"] in already_processed:
                continue
            docs.append(doc)
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            logging.warning(f"Skipping {file}: {e}")
    return docs


_UPSERT_SQL = """
INSERT INTO documents (
    doc_id, source_record_id, session_label, designation, subtype, committee, status,
    date, source_updated_at, published_at, session_year,
    title, subtitle, text, proposals_text, has_text,
    url_text, url_html, url_pdf,
    parties, author_names, proposals_raw, attachments, num_proposals
) VALUES %s
ON CONFLICT (doc_id) DO NOTHING
"""
# Framtida förbättring: DO UPDATE ... WHERE EXCLUDED.source_updated_at > documents.source_updated_at
# för att plocka upp utskottsutfall på redan inlästa motioner.

_AUTHORS_SQL = """
INSERT INTO document_authors (doc_id, ordinal, person_id, name, party, role)
VALUES %s
ON CONFLICT (doc_id, ordinal) DO NOTHING
"""

_YRKANDEN_SQL = """
INSERT INTO document_proposals (id, doc_id, ordinal, number, text, committee_recommendation, chamber_decision, handled_in)
VALUES %s
ON CONFLICT (id) DO NOTHING
"""


def _doc_to_row(doc: dict) -> tuple:
    return (
        doc["doc_id"],
        doc["source_record_id"],
        doc["session_label"],
        doc["designation"],
        doc["subtype"],
        doc["committee"],
        doc["status"],
        doc["date"],
        doc["source_updated_at"],
        doc["published_at"],
        doc["year"],
        doc["title"],
        doc["subtitle"],
        doc["text"],
        doc["proposals_text"],
        doc["has_text"],
        doc["url_text"],
        doc["url_html"],
        doc["url_pdf"],
        doc["parties"],
        doc["author_names"],
        doc["proposals_raw"],
        doc["attachments"],
        doc["num_proposals"],
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
        rows = pg.execute("SELECT doc_id FROM documents")
        already_processed = {row["doc_id"] for row in rows}

    docs = process_folder(path, already_processed)
    insert_docs(docs)
    return len(docs)


if __name__ == "__main__":
    existing = {row["doc_id"] for row in pg.execute("SELECT doc_id FROM documents")}
    total = 0
    for folder in sorted(os.listdir("motioner")):
        path = str(bootstrap.DATA_DIR / 'motioner' / folder)
        if not os.path.isdir(path):
            continue
        print(f"Processing {folder} …", end=" ", flush=True)
        docs = process_folder(path, already_processed=existing)
        insert_docs(docs)
        existing |= {d["doc_id"] for d in docs}
        total += len(docs)
        print(f"{len(docs)} inserted")
    print(f"\nTotal: {total} new documents inserted")
