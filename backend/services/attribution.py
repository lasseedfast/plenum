"""Runtime attribution validator for chat answers.

Used in two ways:

1. **Detector (read-only).** Scans the final answer for `Name (PARTY)` / `[Name](/mp/id)(PARTY)`
   tokens and checks that at least one `[N]` citation in the same paragraph points to a
   source whose speaker/party matches. Mismatches are returned as structured warnings —
   no answer text is mutated. Feeds the API response and, when enabled, the editor pass.

2. **Link gate.** Given a paragraph and the set of cited sources in it, `paragraph_supports_name`
   tells `_inject_person_links` whether wrapping a bare name with a portrait link is safe.

A nearest-`[N]` heuristic was considered and rejected: one MP often speaks *about* another,
so the nearest citation is not always the anchoring one. We only assert matches within the
paragraph set (any-match), never across paragraphs.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional

# Matches [Name](/mp/12345) (PARTY) — the injected link with trailing party
_SPEAKER_LINK_RE = re.compile(
    r"\[([^\]]+)\]\(/mp/[^)]+\)\s*\(([A-ZÅÄÖ]{1,3})\)",
    re.UNICODE,
)

# Matches Name (PARTY) — plain text form, used as a fallback scan.
# Parties are 1–3 uppercase letters, optionally åäö (e.g. "L", "MP", "SD", "FP").
# We intentionally restrict "Name" to Title-case-ish tokens so we don't light up on
# every parenthesised aside in the text.
_PLAIN_SPEAKER_RE = re.compile(
    r"(?<!\[)(?<!\w)([A-ZÅÄÖ][\wåäöÅÄÖ.\-']+(?:\s+[A-ZÅÄÖ][\wåäöÅÄÖ.\-']+){0,3})\s*\(([A-ZÅÄÖ]{1,3})\)",
    re.UNICODE,
)

# Matches [N] citation references (after parse_and_renumber_citations has run).
_CITATION_N_RE = re.compile(r"\[(\d+)\]")


def _normalize(s: Optional[str]) -> str:
    if not s:
        return ""
    return unicodedata.normalize("NFKD", s).casefold().strip()


def _name_tokens_match(para_name: str, src_name: str) -> bool:
    """Loose name match: last name in src must appear in para, or vice versa."""
    p, s = _normalize(para_name), _normalize(src_name)
    if not p or not s:
        return False
    return p in s or s in p


def _split_paragraphs_with_offset(body: str) -> List[tuple[int, str]]:
    """Return [(char_offset, paragraph_text), ...]. Skips the Källor section caller passes."""
    paragraphs: List[tuple[int, str]] = []
    offset = 0
    for chunk in body.split("\n\n"):
        paragraphs.append((offset, chunk))
        offset += len(chunk) + 2  # the "\n\n" we split on
    return paragraphs


def _cited_sources_in(paragraph: str, cited_sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """For a paragraph, return the source dicts whose [N] appears inside it.

    `cited_sources` is 1-indexed: cited_sources[0] is `[1]`, etc. This matches what
    `parse_and_renumber_citations` produces.
    """
    ns = {int(m) for m in _CITATION_N_RE.findall(paragraph)}
    out: List[Dict[str, Any]] = []
    for n in ns:
        idx = n - 1
        if 0 <= idx < len(cited_sources):
            out.append(cited_sources[idx])
    return out


def paragraph_supports_name(
    paragraph: str,
    name: str,
    cited_sources: List[Dict[str, Any]],
) -> bool:
    """Does this paragraph have at least one [N] whose source speaker matches `name`?

    Used by `_inject_person_links` to gate portrait links. If no citation in the
    paragraph supports the name, we leave the name as plain text — safer than
    wrapping it with a misleading /mp/id link.
    """
    for src in _cited_sources_in(paragraph, cited_sources):
        src_name = src.get("speaker") or ""
        if _name_tokens_match(name, src_name):
            return True
    return False


def detect_attribution_warnings(
    answer_body: str,
    cited_sources: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Scan each paragraph and flag `Name (PARTY)` tokens with no matching cited source.

    Returns a list of warning dicts:
        {
          "paragraph_idx": int,    # index among paragraphs containing >=1 [N]
          "name": str,
          "party": str,
          "cited_ns": list[int],   # the [N] numbers that were in the paragraph
          "reason": "no_matching_speaker" | "party_mismatch"
        }

    No mutation. The caller decides what to do (log, pass to editor, display a badge).
    """
    warnings: List[Dict[str, Any]] = []
    para_idx = 0
    for _, para in _split_paragraphs_with_offset(answer_body):
        ns = [int(m) for m in _CITATION_N_RE.findall(para)]
        if not ns:
            continue  # no citations, no attribution claim to verify
        srcs = [cited_sources[n - 1] for n in ns if 0 < n <= len(cited_sources)]

        tokens: List[tuple[str, str]] = [
            (m.group(1), m.group(2)) for m in _SPEAKER_LINK_RE.finditer(para)
        ]
        # Also pick up plain "Name (PARTY)" mentions that haven't been wrapped yet.
        # Dedupe against the link form so we don't double-report.
        linked_names = {_normalize(n) for n, _ in tokens}
        for m in _PLAIN_SPEAKER_RE.finditer(para):
            nm, pt = m.group(1), m.group(2)
            if _normalize(nm) in linked_names:
                continue
            tokens.append((nm, pt))

        for nm, pt in tokens:
            name_matches = [s for s in srcs if _name_tokens_match(nm, s.get("speaker") or "")]
            if not name_matches:
                warnings.append({
                    "paragraph_idx": para_idx,
                    "name": nm,
                    "party": pt,
                    "cited_ns": ns,
                    "reason": "no_matching_speaker",
                })
                continue
            # Name matched something — check party too.
            party_ok = any(
                (s.get("party") or "").upper() == pt.upper() for s in name_matches
            )
            if not party_ok:
                warnings.append({
                    "paragraph_idx": para_idx,
                    "name": nm,
                    "party": pt,
                    "cited_ns": ns,
                    "reason": "party_mismatch",
                })
        para_idx += 1
    return warnings
