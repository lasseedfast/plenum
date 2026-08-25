"""Generic fetch → adapt → upsert pipeline.

Country-neutral: every source-specific decision comes from `sources:` in
parliament.yaml and from the adapter named there.
"""
from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import requests

from bootstrap import DATA_DIR
from parliament import PARLIAMENT
from postgres_client import pg

# Rows per INSERT. Large enough that round-trips are not the bottleneck, small
# enough that one bad batch is cheap to retry.
BATCH = 500


# ── fetching ──────────────────────────────────────────────────────────────────


def source_config(name: str) -> dict:
    sources = PARLIAMENT.sources
    if name not in sources:
        available = [k for k in sources if k != "adapter"]
        raise ValueError(f"No source {name!r} in parliament.yaml; have {available}")
    return sources[name]


def dest_dir(name: str) -> Path:
    return DATA_DIR / source_config(name).get("dest_dir", name)


def fetch(name: str, ranges: list[str] | None = None) -> list[Path]:
    """Download a source's bulk archives and unpack them under DATA_DIR.

    Archives already unpacked are skipped, so this is safe to re-run and cheap to
    resume after an interruption — which matters when a full download is tens of GB.
    """
    cfg = source_config(name)
    kind = cfg.get("kind", "zip-dataset")
    target = dest_dir(name)
    target.mkdir(parents=True, exist_ok=True)

    if kind == "json":
        out = target / f"{name}.json"
        out.write_bytes(requests.get(cfg["url"], timeout=120).content)
        print(f"  fetched {out.relative_to(DATA_DIR)}")
        return [out]

    if kind != "zip-dataset":
        raise ValueError(f"Unsupported source kind {kind!r} for {name}")

    wanted = ranges or cfg.get("ranges")
    if not wanted:
        raise ValueError(
            f"Source {name!r} has no `ranges:` in parliament.yaml and none was given. "
            f"Pass --range, or list them in the config."
        )

    written = []
    for rng in wanted:
        folder = target / rng
        if folder.exists() and any(folder.iterdir()):
            print(f"  {rng}: already present, skipping")
            continue
        url = cfg["url_template"].format(range=rng)
        print(f"  {rng}: downloading {url}")
        resp = requests.get(url, timeout=1800)
        if resp.status_code != 200:
            print(f"  {rng}: HTTP {resp.status_code}, skipping")
            continue
        folder.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            zf.extractall(folder)
        written.append(folder)
        print(f"  {rng}: unpacked {len(list(folder.rglob('*.json')))} files")
    return written


def read_records(name: str) -> Iterator[dict]:
    """Yield every source record on disk for a source."""
    root = dest_dir(name)
    if not root.exists():
        raise FileNotFoundError(f"No data at {root}. Run `ingest.cli fetch --source {name}` first.")
    for path in sorted(root.rglob("*.json")):
        try:
            # utf-8-sig: the archives carry a byte-order mark.
            with open(path, encoding="utf-8-sig") as fh:
                payload = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  skipping {path.name}: {exc}")
            continue
        # A people listing is one file holding many records.
        if isinstance(payload, dict) and "personlista" in payload:
            yield from payload["personlista"].get("person", [])
        else:
            yield payload


# ── loading ───────────────────────────────────────────────────────────────────


def _insert(table: str, columns: list[str], rows: list[tuple], conflict: str) -> int:
    if not rows:
        return 0
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES %s "
        f"ON CONFLICT ({conflict}) DO NOTHING"
    )
    pg.execute_values(sql, rows)
    return len(rows)


def _flush(table: str, columns: list[str], conflict: str, buffer: list[dict]) -> int:
    """Insert and clear a buffer. Argument order matches `_target()` unpacking."""
    rows = [tuple(r.get(c) for c in columns) for r in buffer]
    n = _insert(table, columns, rows, conflict)
    buffer.clear()
    return n


SPEECH_COLUMNS = [
    "id", "source_speech_id", "source_doc_id", "text", "section_title", "sequence",
    "activity_type", "speaker_name", "party", "person_id", "date", "source_datetime",
    "year", "session_year", "related_doc_id", "source_doc_number", "source_record_id",
    "title", "is_reply",
]
DOCUMENT_COLUMNS = [
    "doc_id", "doc_type", "source_record_id", "session_label", "designation", "subtype",
    "committee", "status", "date", "source_updated_at", "published_at", "session_year",
    "title", "subtitle", "text", "proposals_text", "has_text", "url_text", "url_html",
    "url_pdf", "parties", "author_names", "proposals_raw", "attachments", "num_proposals",
]
AUTHOR_COLUMNS = ["doc_id", "ordinal", "person_id", "name", "party", "role"]
PROPOSAL_COLUMNS = [
    "id", "doc_id", "ordinal", "number", "text",
    "committee_recommendation", "chamber_decision", "handled_in",
]
PERSON_COLUMNS = [
    "person_id", "source_record_id", "source_record_guid", "source_id", "birth_year",
    "gender", "last_name", "first_name", "sort_name", "home_town", "party",
    "constituency", "status", "source_url", "image_url_small", "image_url_medium",
    "image_url_large", "assignments", "contact_details", "name", "active",
]


def _json_columns(rows: list[dict], columns: Iterator[str]) -> None:
    """psycopg2 cannot adapt dicts/lists destined for JSONB; serialise them."""
    for row in rows:
        for col in columns:
            if isinstance(row.get(col), (dict, list)):
                row[col] = json.dumps(row[col], ensure_ascii=False)


def load(name: str, adapt: Callable[[str, dict], dict | None], limit: int | None = None) -> dict:
    """Adapt every record on disk for a source and upsert it."""
    counts = {"read": 0, "written": 0, "skipped": 0}
    buf: list[dict] = []
    authors: list[dict] = []
    proposals: list[dict] = []

    for payload in read_records(name):
        if limit and counts["read"] >= limit:
            break
        counts["read"] += 1
        row = adapt(name, payload)
        if row is None:
            counts["skipped"] += 1
            continue

        if name == "documents":
            _json_columns([row["document"]], ("proposals_raw", "attachments"))
            buf.append(row["document"])
            authors.extend(row["authors"])
            proposals.extend(row["proposals"])
        else:
            if name == "people":
                _json_columns([row], ("assignments", "contact_details"))
            buf.append(row)

        if len(buf) >= BATCH:
            counts["written"] += _flush(*_target(name), buf)
            if authors:
                _flush("document_authors", AUTHOR_COLUMNS, "doc_id, ordinal", authors)
            if proposals:
                _flush("document_proposals", PROPOSAL_COLUMNS, "id", proposals)

    counts["written"] += _flush(*_target(name), buf)
    if authors:
        _flush("document_authors", AUTHOR_COLUMNS, "doc_id, ordinal", authors)
    if proposals:
        _flush("document_proposals", PROPOSAL_COLUMNS, "id", proposals)
    return counts


def refresh_person_stats() -> None:
    """Rebuild the per-person speech aggregates the name search ranks on.

    Cheap next to a load, and skipping it leaves newly ingested speeches out of
    the ranking until something else rebuilds the view. Reported rather than
    raised: a sync that has already written its rows should not exit non-zero
    because an optional view is missing.
    """
    try:
        pg.execute_void("REFRESH MATERIALIZED VIEW CONCURRENTLY person_speech_stats")
        print("  refreshed person_speech_stats")
    except Exception as exc:
        print(f"  person_speech_stats not refreshed: {type(exc).__name__}: {exc}")


def _target(name: str) -> tuple[str, list[str], str]:
    return {
        "speeches": ("speeches", SPEECH_COLUMNS, "id"),
        "documents": ("documents", DOCUMENT_COLUMNS, "doc_id"),
        "people": ("people", PERSON_COLUMNS, "person_id"),
    }[name]
