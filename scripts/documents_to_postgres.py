"""
Läser in anföranden från JSON-filer till PostgreSQL.

Ersätter scripts/documents_to_arango.py.

Används av sync_talks.py (update_folder) och kan köras direkt för att
(om)ladda alla mappar i speeches/:

    python scripts/documents_to_postgres.py
"""
from pathlib import Path

import json
import logging
import os
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bootstrap  # noqa: E402,F401  — sets cwd and sys.path to the project root

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
            source_doc_id = doc.get("dok_id", "")
            sequence = doc.get("sequence", "")
            speech_id = f"{source_doc_id}-{sequence}"  # unique per speech, e.g. "GH09116-16"
            if speech_id in already_processed:
                continue
            doc["year"] = int(doc.get("dok_rm", "0000")[:4])
            doc.pop("dok_rm", None)
            doc["text"] = clean_text(doc.get("text", ""))
            doc["speaker_name"] = clean_speaker_name(doc.get("speaker_name", ""))
            doc["id"] = speech_id
            doc["dok_id"] = source_doc_id
            doc["source_speech_id"] = doc.get("source_speech_id", "")  # UUID, kept for reference
            doc["date"] = _parse_date(doc.get("source_datetime", ""))
            doc["source_datetime"] = doc.get("source_datetime", "")
            doc["title"] = doc.get("dok_titel", "")
            doc.pop("dok_titel", None)
            doc["sequence"] = int(sequence) if sequence else 0
            doc["source_record_id"] = doc.get("dok_hangar_id", "")
            doc.pop("dok_hangar_id", None)
            doc["is_reply"] = doc.get("is_reply", "N") == "Y"
            doc.pop("source_updated_at", None)
            doc.pop("underrubrik", None)
            year = doc.get("year") or (int(doc["date"][:4]) if doc.get("date") else None)
            doc["year"] = year
            docs.append(doc)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logging.warning(f"Skipping {file}: {e}")
    return docs


_UPSERT_SQL = """
INSERT INTO speeches (
    id, source_speech_id, source_doc_id,
    text, section_title,
    sequence, activity_type,
    speaker_name, party, person_id,
    date, source_datetime, year, year,
    related_doc_id, source_doc_number, source_record_id, title,
    is_reply
) VALUES %s
ON CONFLICT (id) DO NOTHING
"""


def _doc_to_row(doc: dict) -> tuple:
    return (
        doc.get("id"),           # source_speech_id UUID — primary key
        doc.get("source_speech_id"), # same value, kept for reference
        doc.get("dok_id"),       # debate/protocol document id
        doc.get("text"),
        doc.get("section_title"),
        doc.get("sequence"),
        doc.get("activity_type"),
        doc.get("speaker_name"),
        doc.get("party"),
        doc.get("person_id"),
        doc.get("date"),
        doc.get("source_datetime"),
        doc.get("year"),
        doc.get("year"),
        doc.get("related_doc_id"),
        doc.get("source_doc_number"),
        doc.get("source_record_id"),
        doc.get("title"),
        doc.get("is_reply", False),
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
    Returns the number of new speeches inserted.
    """
    if already_processed is None:
        rows = pg.execute("SELECT id FROM speeches")
        already_processed = {row["id"] for row in rows}

    docs = process_folder(path, already_processed)
    insert_docs(docs)
    return len(docs)


if __name__ == "__main__":
    # Load all folders in speeches/ into PostgreSQL
    existing = {row["id"] for row in pg.execute("SELECT id FROM speeches")}
    total = 0
    for folder in sorted(os.listdir("speeches")):
        path = str(bootstrap.DATA_DIR / 'speeches' / folder)
        if not os.path.isdir(path):
            continue
        print(f"Processing {folder} …", end=" ", flush=True)
        docs = process_folder(path, already_processed=existing)
        insert_docs(docs)
        new_ids = {d["id"] for d in docs if d.get("id")}
        existing |= new_ids
        total += len(new_ids)
        print(f"{len(new_ids)} inserted")
    print(f"\nTotal: {total} new speeches inserted")
