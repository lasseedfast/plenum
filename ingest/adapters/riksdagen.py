"""Adapter for the Swedish Riksdag's open data (data.riksdagen.se).

This module is the *only* place that knows what the Riksdag calls its fields.
Everything downstream works in plenum's own column names. Copy this file as the
starting point for another parliament — see docs/PORTING.md.

Two things about this source that will probably bite you elsewhere too:

* Its JSON is converted from XML, so a repeated element arrives as an object when
  there is one of them and as a list when there are several. `_as_list` normalises
  that; forgetting it produces code that works until the day a document has two
  authors.
* Null is serialised as the *string* `"None"`, which is truthy and will happily be
  written to the database as text unless it is stripped.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Iterable, Iterator, Optional

# ── source quirks ─────────────────────────────────────────────────────────────


def _as_list(value: Any) -> list:
    """One child arrives as an object, several as a list, none as null."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _clean(value: Any) -> Any:
    """The API serialises null as the string "None"."""
    if value is None or value == "None":
        return None
    return value


def _parse_date(raw: Optional[str]) -> Optional[date]:
    """Dates arrive as "YYYY-MM-DD HH:MM:SS", sometimes without the time."""
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip()[: len(fmt) + 2], fmt).date()
        except ValueError:
            continue
    return None


def _session_start_year(session_label: Optional[str]) -> Optional[int]:
    """"2022/23" -> 2022."""
    if not session_label:
        return None
    try:
        return int(str(session_label)[:4])
    except ValueError:
        return None


_TITLE_PREFIXES = re.compile(
    r"^(?:statsrådet|ministern|talman(?:nen)?|herr|fru)\s+", re.IGNORECASE
)
# The transcript appends the party in parentheses: "Mikael Dahlqvist (S)". The
# party is stored in its own column, and leaving it here would break joins to the
# member register and show up duplicated in the UI.
_TRAILING_PARTY = re.compile(r"\s*\([A-ZÅÄÖ\-]{1,4}\)\s*$")


def _clean_speaker(name: Optional[str]) -> Optional[str]:
    """Strip honorifics and the trailing party, so names join to the register."""
    if not name:
        return None
    name = _TITLE_PREFIXES.sub("", name.strip())
    return _TRAILING_PARTY.sub("", name).strip() or None


# ── speeches ──────────────────────────────────────────────────────────────────

# Left: plenum column. Right: the Riksdag's field name. Written this way round
# because the column names are the contract and the source names are the detail.
SPEECH_FIELDS: dict[str, str] = {
    "source_speech_id": "anforande_id",
    "text": "anforandetext",
    "section_title": "avsnittsrubrik",
    "sequence": "anforande_nummer",
    "activity_type": "kammaraktivitet",
    "speaker_name": "talare",
    "party": "parti",
    "person_id": "intressent_id",
    "source_datetime": "dok_datum",
    "source_doc_id": "dok_id",
    "related_doc_id": "rel_dok_id",
    "source_doc_number": "dok_nummer",
    "source_record_id": "dok_hangar_id",
    "title": "dok_titel",
}


def adapt_speech(payload: dict) -> Optional[dict]:
    """Turn one source record into a `speeches` row, or None if unusable."""
    doc = payload.get("anforande") if "anforande" in payload else payload
    if not doc:
        return None

    row = {col: _clean(doc.get(src)) for col, src in SPEECH_FIELDS.items()}

    # The primary key is the protocol document plus the position within it. The
    # source's own `anforande_id` is a UUID that changed in an earlier migration,
    # so it is kept for reference but is not the key.
    if not row["source_doc_id"] or row["sequence"] in (None, ""):
        return None
    row["id"] = f"{row['source_doc_id']}-{row['sequence']}"

    row["speaker_name"] = _clean_speaker(row["speaker_name"])
    # Speech text arrives as HTML fragments; the corpus stores plain text so that
    # full-text search and snippet extraction do not have to strip tags per query.
    row["text"] = _html_to_text(row["text"]) if row["text"] else None
    row["date"] = _parse_date(row["source_datetime"])
    row["year"] = _session_start_year(doc.get("dok_rm"))
    row["session_year"] = row["year"]
    # "Y"/"N", not a boolean.
    row["is_reply"] = _clean(doc.get("replik")) == "Y"
    return row


# ── documents ─────────────────────────────────────────────────────────────────

DOCUMENT_FIELDS: dict[str, str] = {
    "doc_id": "dok_id",
    "source_record_id": "hangar_id",
    "session_label": "rm",
    "designation": "beteckning",
    "subtype": "subtyp",
    "committee": "organ",
    "status": "status",
    "source_updated_at": "systemdatum",
    "published_at": "publicerad",
    "title": "titel",
    "subtitle": "undertitel",
    "url_text": "dokument_url_text",
    "url_html": "dokument_url_html",
}


def adapt_document(payload: dict) -> Optional[dict]:
    """Turn one `dokumentstatus` record into a `documents` row plus its children.

    Returns a dict with keys `document`, `authors` and `proposals`; the caller
    decides how to persist them.
    """
    status = payload.get("dokumentstatus") or payload
    doc = status.get("dokument")
    if not doc or not doc.get("dok_id"):
        return None

    row = {col: _clean(doc.get(src)) for col, src in DOCUMENT_FIELDS.items()}
    row["doc_type"] = "motion"
    row["date"] = _parse_date(doc.get("datum"))
    row["session_year"] = _session_start_year(row["session_label"])

    authors = []
    for i, person in enumerate(_as_list((status.get("dokintressent") or {}).get("intressent"))):
        authors.append({
            "doc_id": row["doc_id"],
            "ordinal": i,
            "person_id": _clean(person.get("intressent_id")),
            "name": _clean(person.get("namn")),
            "party": _clean(person.get("partibet")),
            "role": _clean(person.get("roll")),
        })

    proposals = []
    for i, item in enumerate(_as_list((status.get("dokforslag") or {}).get("forslag"))):
        proposals.append({
            "id": f"{row['doc_id']}:{i}",
            "doc_id": row["doc_id"],
            "ordinal": i,
            "number": _clean(item.get("nummer")),
            "text": _clean(item.get("lydelse")),
            "committee_recommendation": _clean(item.get("utskottet")),
            "chamber_decision": _clean(item.get("kammaren")),
            "handled_in": _clean(item.get("behandlas_i")),
        })

    # Denormalised so a party filter does not need a join.
    row["parties"] = sorted({a["party"] for a in authors if a["party"]})
    row["author_names"] = [a["name"] for a in authors if a["name"]]
    row["num_proposals"] = len(proposals)
    row["proposals_text"] = "\n".join(p["text"] for p in proposals if p["text"]) or None
    row["proposals_raw"] = (status.get("dokforslag") or {}).get("forslag")
    row["attachments"] = (status.get("dokbilaga") or {}).get("bilaga")

    # `html` is the full text; documents that exist only as scanned PDFs have none,
    # which is why has_text is stored rather than inferred at query time.
    html = _clean(doc.get("html"))
    row["text"] = _html_to_text(html) if html else None
    row["has_text"] = bool(row["text"])
    row["url_pdf"] = _pdf_url(status)

    return {"document": row, "authors": authors, "proposals": proposals}


def _html_to_text(html: str) -> Optional[str]:
    from bs4 import BeautifulSoup

    text = BeautifulSoup(html, "html.parser").get_text("\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip() or None


def _pdf_url(status: dict) -> Optional[str]:
    for attachment in _as_list((status.get("dokbilaga") or {}).get("bilaga")):
        url = _clean(attachment.get("fil_url"))
        if url and url.lower().endswith(".pdf"):
            return url
    return None


# ── people ────────────────────────────────────────────────────────────────────

PERSON_FIELDS: dict[str, str] = {
    "person_id": "intressent_id",
    "source_record_id": "hangar_id",
    "source_record_guid": "hangar_guid",
    "source_id": "sourceid",
    "birth_year": "fodd_ar",
    "gender": "kon",
    "last_name": "efternamn",
    "first_name": "tilltalsnamn",
    "sort_name": "sorteringsnamn",
    "home_town": "iort",
    "party": "parti",
    "constituency": "valkrets",
    "status": "status",
    "source_url": "person_url_xml",
    "image_url_small": "bild_url_80",
    "image_url_medium": "bild_url_192",
    "image_url_large": "bild_url_max",
}


def adapt_person(payload: dict) -> Optional[dict]:
    if not payload.get("intressent_id"):
        return None
    row = {col: _clean(payload.get(src)) for col, src in PERSON_FIELDS.items()}
    row["name"] = " ".join(x for x in (row["first_name"], row["last_name"]) if x) or None
    row["assignments"] = payload.get("personuppdrag")
    row["contact_details"] = payload.get("personuppgift")
    row["active"] = _clean(payload.get("status")) not in (None, "Avgången")
    return row


ADAPTERS = {
    "speeches": adapt_speech,
    "documents": adapt_document,
    "people": adapt_person,
}


def adapt(kind: str, payload: dict) -> Optional[dict]:
    """Adapt one record of the named kind."""
    try:
        return ADAPTERS[kind](payload)
    except KeyError:
        raise ValueError(f"Unknown record kind {kind!r}; expected one of {sorted(ADAPTERS)}")


def adapt_many(kind: str, payloads: Iterable[dict]) -> Iterator[dict]:
    """Adapt a stream, skipping records the source could not supply usably."""
    for payload in payloads:
        row = adapt(kind, payload)
        if row is not None:
            yield row
