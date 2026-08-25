"""
Provenance registry for tracking all sources the LLM sees during a chat session.

Every tool result (search hits, fetched documents, etc.) registers its sources here.
After the LLM produces an answer with [src:ID] tags, the registry validates cited IDs,
renumbers them to [1], [2], and generates the "Källor" section deterministically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

BODY_CAP_CHARS = 3000


@dataclass
class SourceRecord:
    """A single source (talk) that the LLM saw during research."""

    source_id: str  # bare talk ID, e.g. "H40911"
    tool: str  # which tool produced it
    speaker: str | None = None
    party: str | None = None
    date: str | None = None
    heading: str | None = None
    url_video: str | None = None
    snippet: str = ""
    person_id: str | None = None
    score: float = 0.0
    body: str = ""  # Grounding text (chunk + neighbours, summary, or capped full text). Capped at BODY_CAP_CHARS.


class ProvenanceRegistry:
    """
    Collects and deduplicates sources across all tool calls in a single chat session.

    Keyed by bare talk ID (e.g. "H40911"). Multiple speech_chunks from the same talk
    update the existing record (keeping the best snippet) rather than creating
    duplicate entries.
    """

    def __init__(self) -> None:
        self._sources: dict[str, SourceRecord] = {}
        self._order: list[str] = []  # insertion order

    def register(self, record: SourceRecord) -> str:
        """Register a source. Deduplicates by source_id, keeps best snippet/body."""
        sid = record.source_id
        # Cap body on the way in.
        if record.body and len(record.body) > BODY_CAP_CHARS:
            record.body = record.body[:BODY_CAP_CHARS].rstrip() + "…"
        if sid in self._sources:
            existing = self._sources[sid]
            # Keep the longer/better snippet
            if len(record.snippet) > len(existing.snippet):
                existing.snippet = record.snippet
            if len(record.body) > len(existing.body):
                existing.body = record.body
            # Fill in missing metadata
            if record.speaker and not existing.speaker:
                existing.speaker = record.speaker
            if record.party and not existing.party:
                existing.party = record.party
            if record.date and not existing.date:
                existing.date = record.date
            if record.heading and not existing.heading:
                existing.heading = record.heading
            if record.url_video and not existing.url_video:
                existing.url_video = record.url_video
            if record.person_id and not existing.person_id:
                existing.person_id = record.person_id
            if record.score > existing.score:
                existing.score = record.score
        else:
            self._sources[sid] = record
            self._order.append(sid)
        return sid

    def get(self, source_id: str) -> SourceRecord | None:
        return self._sources.get(source_id)

    def size(self) -> int:
        return len(self._sources)

    def all_sources(self) -> list[SourceRecord]:
        """Return all sources in registration order."""
        return [self._sources[sid] for sid in self._order if sid in self._sources]

    def get_persons(self) -> dict[str, dict]:
        """Return {person_id: {name, party}} for person link injection."""
        persons: dict[str, dict] = {}
        for src in self._sources.values():
            if src.person_id and src.speaker and src.person_id not in persons:
                persons[src.person_id] = {
                    "name": src.speaker,
                    "party": src.party or "",
                }
        return persons

    def to_cited_sources(self, cited_ids: list[str]) -> list[dict[str, Any]]:
        """Convert a list of cited source IDs to ChatSource dicts for the frontend."""
        result = []
        for sid in cited_ids:
            src = self._sources.get(sid)
            if not src:
                continue
            result.append(
                {
                    "_id": f"speeches/{sid}",
                    "heading": src.heading,
                    "snippet": _trim_snippet(src.snippet),
                    "chunk_index": -1,
                    "url_video": src.url_video,
                    "speaker": src.speaker,
                    "party": src.party,
                    "person_id": src.person_id,
                    "date": src.date,
                }
            )
        return result


# ---------------------------------------------------------------------------
# Citation parsing and renumbering
# ---------------------------------------------------------------------------

# Tag format: [src:ID] or [src:ID | Speaker (Party) | date]. Also tolerates
# [source:ID], [[src:ID]], and mixed case — all produced by LLMs in practice.
# Double brackets ([[...]]) are matched by \[{1,2} / \]{1,2}.
# Capture group 1 is always the bare ID; metadata after "|" is ignored.
_SRC_PATTERN = re.compile(
    r"\[{1,2}(?:src|source):([A-Za-z0-9_-]+)(?:\s*\|[^\]]*?)?\]{1,2}",
    re.IGNORECASE,
)
_KALLOR_SPLIT = re.compile(r"\n#+\s*K[äa]ll[ao]r", re.IGNORECASE)


def parse_and_renumber_citations(
    answer_text: str,
    registry: ProvenanceRegistry,
    max_fallback: int = 5,
) -> tuple[str, list[dict[str, Any]], list[str], list[str]]:
    """
    Parse [src:ID] tags from the model's answer, validate against the registry,
    replace with [1], [2], and generate a "Källor" section.

    Returns (validated_answer, cited_sources_list).
    """
    cited_ids_raw = _SRC_PATTERN.findall(answer_text)

    # Deduplicate preserving first-appearance order, validate against registry
    seen: set[str] = set()
    unique_cited_ids: list[str] = []
    invalid_ids: list[str] = []

    for cid in cited_ids_raw:
        if cid in seen:
            continue
        if registry.get(cid):
            seen.add(cid)
            unique_cited_ids.append(cid)
        else:
            invalid_ids.append(cid)
            seen.add(cid)  # don't report same invalid ID twice

    # Build replacement map
    id_to_number = {cid: i + 1 for i, cid in enumerate(unique_cited_ids)}

    def _replace_src(m: re.Match) -> str:
        src_id = m.group(1)
        num = id_to_number.get(src_id)
        return f"[{num}]" if num else ""

    validated_answer = _SRC_PATTERN.sub(_replace_src, answer_text)

    # Strip any leftover malformed tags the main pattern didn't catch
    # (e.g. mismatched brackets, extra punctuation around the tag).
    validated_answer = re.sub(
        r"\[{1,2}(?:src|source):[A-Za-z0-9_:.\-]{1,80}(?:\s*\|[^\]\n]{0,150})?\]{1,2}",
        "",
        validated_answer,
        flags=re.IGNORECASE,
    )

    # Strip any model-generated "Källor" section
    validated_answer = _KALLOR_SPLIT.split(validated_answer)[0].rstrip()

    # Build cited sources from registry
    if unique_cited_ids:
        cited_sources = registry.to_cited_sources(unique_cited_ids)
    elif not cited_ids_raw:
        # The model wrote no [src:...] tags at all (as opposed to citing unknown
        # IDs) — fall back to surfacing what it actually saw, capped at
        # max_fallback, so the UI still shows provenance for an uncited answer.
        # These are never renumbered into the body text itself, just listed.
        fallback_ids = [s.source_id for s in registry.all_sources()[:max_fallback]]
        cited_sources = registry.to_cited_sources(fallback_ids)
    else:
        cited_sources = []

    # Generate "Källor" section server-side
    if cited_sources:
        kallor_lines = []
        for i, src in enumerate(cited_sources, 1):
            speaker = src.get("speaker") or "Okänd"
            src_date = src.get("date") or ""
            heading = src.get("heading") or ""
            line = f"[{i}] {speaker} – {src_date}"
            if heading:
                line += f" – {heading}"
            kallor_lines.append(line)
        validated_answer += "\n\n### Källor\n\n" + "\n\n".join(kallor_lines)

    return validated_answer, cited_sources, unique_cited_ids, invalid_ids


def normalize_talk_id(raw: str | None) -> str | None:
    """Strip 'speeches/' prefix to get bare talk ID."""
    if not raw:
        return None
    if "/" in raw:
        return raw.split("/", 1)[1]
    return raw


def _trim_snippet(text: str, length: int = 400) -> str:
    cleaned = text.strip()
    if len(cleaned) <= length:
        return cleaned
    return f"{cleaned[:length].rstrip()}…"
