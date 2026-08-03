"""The Swedish -> English rename, as data.

One map drives three things that would otherwise drift: the SQL migration, the
mechanical code rewrite, and the compatibility shim that rewrites SQL replayed from
saved chat snapshots.

Design notes worth knowing before editing:

* Column renames are **per table**. `dok_id` means different things in different
  tables — in `talks` it identifies the protocol document the speech appeared in, in
  `motions` it is the document's own primary key — so they map to different names.
* Values are never translated. `Bifall` / `Avslag` stay as published; parliament.yaml
  glosses them. A research tool must not silently rewrite the record.
* Concepts with no clean cross-country equivalent (yrkande, riksmöte, replik) get
  neutral column names here while the country's own word lives in
  parliament.yaml `vocabulary:`.
"""
from __future__ import annotations

# --- tables -------------------------------------------------------------------
# "talks" reads as conference talks to everyone outside this project; "motions" is
# valid parliamentary English but the table really holds member-submitted documents
# of which a motion is one type — hence the new doc_type column.
TABLES: dict[str, str] = {
    "talks": "speeches",
    "chunks": "speech_chunks",
    "motions": "documents",
    "motion_chunks": "document_chunks",
    "motion_authors": "document_authors",
    "motion_yrkanden": "document_proposals",
}

# --- columns, per table (keyed by the ORIGINAL table name) --------------------
COLUMNS: dict[str, dict[str, str]] = {
    "people": {
        "intressent_id": "person_id",
        "hangar_id": "source_record_id",
        "hangar_guid": "source_record_guid",
        "sourceid": "source_id",
        "fodd_ar": "birth_year",
        "kon": "gender",
        "efternamn": "last_name",
        # "tilltalsnamn" is the name someone is addressed by, which English has no
        # single word for. "first_name" loses that nuance and is worth the trade.
        "tilltalsnamn": "first_name",
        "sorteringsnamn": "sort_name",
        "iort": "home_town",
        "parti": "party",
        # Meaning varies by country: single-member seat in the UK, multi-member
        # district in Sweden. Nothing in the code assumes either.
        "valkrets": "constituency",
        "person_url_xml": "source_url",
        "bild_url_80": "image_url_small",
        "bild_url_192": "image_url_medium",
        "bild_url_max": "image_url_large",
        "personuppdrag": "assignments",
        "personuppgift": "contact_details",
        "namn": "name",
        "aktiv": "active",
    },
    "talks": {
        "anforande_id": "source_speech_id",
        "anforandetext": "text",
        "avsnittsrubrik": "section_title",
        # Position within the debate. Not called "number" to avoid confusion with
        # the 0-based `ordinal` used in the child tables.
        "anforande_nummer": "sequence",
        "kammaraktivitet": "activity_type",
        # English "Speaker" is the presiding officer, so a bare `speaker` would be
        # ambiguous in a parliamentary schema. The API keeps `speaker`, which is
        # already its public contract.
        "talare": "speaker_name",
        "parti": "party",
        "intressent_id": "person_id",
        "datum": "date",
        "dok_datum": "source_datetime",
        "period": "session_year",
        "rel_dok_id": "related_doc_id",
        "dok_nummer": "source_doc_number",
        "hangar_id": "source_record_id",
        "dok_id": "source_doc_id",
        "titel": "title",
        "debate": "debate_id",
        # A short right-of-reply intervention. `is_reply` loses the procedural
        # standing the Swedish term carries; nothing downstream depends on it.
        "replik": "is_reply",
        "debateurl": "url_video",
        "audiofileurl": "url_audio_file",
        "startpos": "audio_start_seconds",
    },
    "chunks": {"talk_id": "speech_id"},
    "debates": {"debate": "id", "datum": "date"},
    "motions": {
        "dok_id": "doc_id",
        "hangar_id": "source_record_id",
        # Annual session, e.g. "2022/23". Deliberately not "term": the European
        # Parliament uses that for its five-year cycle.
        "rm": "session_label",
        "beteckning": "designation",
        "subtyp": "subtype",
        # In `motions` this is always a committee, though the source uses `organ`
        # more broadly elsewhere.
        "organ": "committee",
        "datum": "date",
        "systemdatum": "source_updated_at",
        "publicerad": "published_at",
        "year": "session_year",
        "titel": "title",
        "undertitel": "subtitle",
        "forslag_text": "proposals_text",
        "dokument_url_text": "url_text",
        "dokument_url_html": "url_html",
        "pdf_url": "url_pdf",
        "forslag": "proposals_raw",
        "bilagor": "attachments",
        "num_yrkanden": "num_proposals",
    },
    "motion_authors": {
        "dok_id": "doc_id",
        "intressent_id": "person_id",
        "namn": "name",
        "partibet": "party",
        "roll": "role",
    },
    "motion_chunks": {"motion_id": "doc_id"},
    "motion_yrkanden": {
        "dok_id": "doc_id",
        "nummer": "number",
        # The operative demand itself — the thing people search and cite.
        "lydelse": "text",
        "utskottet": "committee_recommendation",
        "kammaren": "chamber_decision",
        "behandlas_i": "handled_in",
    },
    # session_type values stay 'general' / 'mp'. Rewriting live rows and a CHECK
    # constraint buys nothing: 'mp' reads as "member" generically, and the display
    # label comes from parliament.yaml vocabulary.
    "chat_sessions": {"intressent_id": "person_id", "initial_talk_id": "initial_speech_id"},
    "chat_snapshots": {"intressent_id": "person_id", "initial_talk_id": "initial_speech_id"},
}

# --- indexes ------------------------------------------------------------------
INDEXES: dict[str, str] = {
    "talks_search_idx": "speeches_search_idx",
    "talks_debate_idx": "speeches_debate_idx",
    "talks_parti_idx": "speeches_party_idx",
    "talks_datum_idx": "speeches_date_idx",
    "talks_year_idx": "speeches_year_idx",
    "talks_intressent_idx": "speeches_person_idx",
    "talks_talare_idx": "speeches_speaker_idx",
    "talks_dok_id_idx": "speeches_source_doc_idx",
    "talks_summary_embedding_idx": "speeches_summary_embedding_idx",
    "chunks_talk_idx": "speech_chunks_speech_idx",
    "chunks_embedding_idx": "speech_chunks_embedding_idx",
    "debates_datum_idx": "debates_date_idx",
    "motions_search_idx": "documents_search_idx",
    "motions_datum_idx": "documents_date_idx",
    "motions_year_idx": "documents_session_year_idx",
    "motions_organ_idx": "documents_committee_idx",
    "motions_parties_idx": "documents_parties_idx",
    "motion_authors_intressent_idx": "document_authors_person_idx",
    "motion_chunks_motion_idx": "document_chunks_doc_idx",
    "motion_chunks_embedding_idx": "document_chunks_embedding_idx",
    "motion_yrkanden_dok_idx": "document_proposals_doc_idx",
    "motion_yrkanden_embedding_idx": "document_proposals_embedding_idx",
}

# --- identifiers safe to rewrite globally in code -----------------------------
# Names that resolve to the same new name in every table they appear in. Most
# Swedish column names qualify: `datum` becomes `date` in talks, motions and
# debates alike, so a global rewrite is correct.
#
# Two are deliberately absent because they genuinely differ per table and must be
# read in context:
#   dok_id  -> source_doc_id in talks, but doc_id in motions and its child tables
#   year    -> stays `year` in talks (calendar year), but becomes session_year
#              in motions (derived from the session label)
GLOBAL_IDENTIFIERS: dict[str, str] = {
    # same target in every table
    "datum": "date",
    "titel": "title",
    "namn": "name",
    "parti": "party",
    "talk_id": "speech_id",
    "motion_id": "doc_id",
    "replik": "is_reply",
    "organ": "committee",
    "rm": "session_label",
    "lydelse": "text",
    "subtyp": "subtype",
    "utskottet": "committee_recommendation",
    "kammaren": "chamber_decision",
    "roll": "role",
    "nummer": "number",
    "period": "session_year",
    "forslag": "proposals_raw",
    "aktiv": "active",
    "kon": "gender",
    "iort": "home_town",
    "sourceid": "source_id",
    "hangar_guid": "source_record_guid",
    "hangar_id": "source_record_id",
    "intressent_id": "person_id",
    "intressent_ids": "person_ids",
    "anforandetext": "text",
    "anforande_nummer": "sequence",
    "anforande_id": "source_speech_id",
    "avsnittsrubrik": "section_title",
    "kammaraktivitet": "activity_type",
    "talare": "speaker_name",
    "valkrets": "constituency",
    "efternamn": "last_name",
    "tilltalsnamn": "first_name",
    "sorteringsnamn": "sort_name",
    "fodd_ar": "birth_year",
    "personuppdrag": "assignments",
    "personuppgift": "contact_details",
    "partibet": "party",
    "num_yrkanden": "num_proposals",
    "motion_yrkanden": "document_proposals",
    "motion_authors": "document_authors",
    "motion_chunks": "document_chunks",
    "forslag_text": "proposals_text",
    "dokument_url_text": "url_text",
    "dokument_url_html": "url_html",
    "undertitel": "subtitle",
    "beteckning": "designation",
    "systemdatum": "source_updated_at",
    "publicerad": "published_at",
    "bilagor": "attachments",
    "behandlas_i": "handled_in",
    "debateurl": "url_video",
    "audiofileurl": "url_audio_file",
    "startpos": "audio_start_seconds",
    "bild_url_80": "image_url_small",
    "bild_url_192": "image_url_medium",
    "bild_url_max": "image_url_large",
    "person_url_xml": "source_url",
}


def legacy_sql_map() -> dict[str, str]:
    """Old identifier -> new, for rewriting SQL replayed from saved snapshots.

    Table names come first so `motion_yrkanden` is not partially rewritten by a
    column rule before its table rule fires.
    """
    out: dict[str, str] = dict(TABLES)
    out.update(GLOBAL_IDENTIFIERS)
    return out
