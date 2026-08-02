"""
Läser in anföranden från JSON-filer till PostgreSQL.

Ersätter scripts/documents_to_arango.py.

Används av sync_talks.py (update_folder) och kan köras direkt för att
(om)ladda alla mappar i talks/:

    python scripts/documents_to_postgres.py
"""

import json
import logging
import os
import re
import sys

os.chdir("/home/lasse/riksdagen")
sys.path.append("/home/lasse/riksdagen")

from postgres_client import pg

logging.basicConfig(level=logging.WARNING, format="%(asctime)s - %(levelname)s - %(message)s")


def clean_text(text: str) -> str:
    if text is None:
        return ""
    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = text.replace("</p>", "\n")
    text = re.sub(r"\n+", "\n", text)
    text = text.strip()
    text = re.sub(r"<.*?>", "", text)
    return text


def clean_speaker_name(name: str) -> str:
    if name is None:
        return ""
    name = name.strip()
    name = re.sub(r"\s*\(.*?\)\s*$", "", name)
    return name.strip()


def _parse_date(s: str) -> str | None:
    if not s:
        return None
    return str(s).split(" ")[0][:10] or None


def process_folder(folder_path: str, already_processed: set[str] = frozenset()) -> list[dict]:
    """Parse JSON files in folder_path, return list of talk dicts (skipping known IDs)."""
    docs = []
    for file in os.listdir(folder_path):
        if not file.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder_path, file), "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            doc = data["anforande"]
            dok_id = doc.get("dok_id", "")
            anforande_nummer = doc.get("anforande_nummer", "")
            talk_id = f"{dok_id}-{anforande_nummer}"  # unique per speech, e.g. "GH09116-16"
            if talk_id in already_processed:
                continue
            doc["period"] = int(doc.get("dok_rm", "0000")[:4])
            doc.pop("dok_rm", None)
            doc["anforandetext"] = clean_text(doc.get("anforandetext", ""))
            doc["talare"] = clean_speaker_name(doc.get("talare", ""))
            doc["id"] = talk_id
            doc["dok_id"] = dok_id
            doc["anforande_id"] = doc.get("anforande_id", "")  # UUID, kept for reference
            doc["datum"] = _parse_date(doc.get("dok_datum", ""))
            doc["dok_datum"] = doc.get("dok_datum", "")
            doc["titel"] = doc.get("dok_titel", "")
            doc.pop("dok_titel", None)
            doc["anforande_nummer"] = int(anforande_nummer) if anforande_nummer else 0
            doc["hangar_id"] = doc.get("dok_hangar_id", "")
            doc.pop("dok_hangar_id", None)
            doc["replik"] = doc.get("replik", "N") == "Y"
            doc.pop("systemdatum", None)
            doc.pop("underrubrik", None)
            year = doc.get("period") or (int(doc["datum"][:4]) if doc.get("datum") else None)
            doc["year"] = year
            docs.append(doc)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logging.warning(f"Skipping {file}: {e}")
    return docs


_UPSERT_SQL = """
INSERT INTO talks (
    id, anforande_id, dok_id,
    anforandetext, avsnittsrubrik,
    anforande_nummer, kammaraktivitet,
    talare, parti, intressent_id,
    datum, dok_datum, year, period,
    rel_dok_id, dok_nummer, hangar_id, titel,
    replik
) VALUES %s
ON CONFLICT (id) DO NOTHING
"""


def _doc_to_row(doc: dict) -> tuple:
    return (
        doc.get("id"),           # anforande_id UUID — primary key
        doc.get("anforande_id"), # same value, kept for reference
        doc.get("dok_id"),       # debate/protocol document id
        doc.get("anforandetext"),
        doc.get("avsnittsrubrik"),
        doc.get("anforande_nummer"),
        doc.get("kammaraktivitet"),
        doc.get("talare"),
        doc.get("parti"),
        doc.get("intressent_id"),
        doc.get("datum"),
        doc.get("dok_datum"),
        doc.get("year"),
        doc.get("period"),
        doc.get("rel_dok_id"),
        doc.get("dok_nummer"),
        doc.get("hangar_id"),
        doc.get("titel"),
        doc.get("replik", False),
    )


def insert_docs(docs: list[dict]) -> None:
    if not docs:
        return
    rows = [_doc_to_row(d) for d in docs if d.get("id")]
    if rows:
        pg.execute_values(_UPSERT_SQL, rows)


def update_folder(path: str, already_processed: set[str] = None) -> int:
    """
    Upsert talk documents from JSON files in path into PostgreSQL.
    Returns the number of new talks inserted.
    """
    if already_processed is None:
        rows = pg.execute("SELECT id FROM talks")
        already_processed = {row["id"] for row in rows}

    docs = process_folder(path, already_processed)
    insert_docs(docs)
    return len(docs)


if __name__ == "__main__":
    # Load all folders in talks/ into PostgreSQL
    existing = {row["id"] for row in pg.execute("SELECT id FROM talks")}
    total = 0
    for folder in sorted(os.listdir("talks")):
        path = os.path.join("/home/lasse/riksdagen/talks", folder)
        if not os.path.isdir(path):
            continue
        print(f"Processing {folder} …", end=" ", flush=True)
        docs = process_folder(path, already_processed=existing)
        insert_docs(docs)
        new_ids = {d["id"] for d in docs if d.get("id")}
        existing |= new_ids
        total += len(new_ids)
        print(f"{len(new_ids)} inserted")
    print(f"\nTotal: {total} new talks inserted")
