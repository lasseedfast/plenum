"""Tests for the provenance registry and citation parsing system."""

import pytest
from backend.services.provenance import (
    ProvenanceRegistry,
    SourceRecord,
    normalize_talk_id,
    parse_and_renumber_citations,
    _trim_snippet,
)


# ---------------------------------------------------------------------------
# normalize_talk_id
# ---------------------------------------------------------------------------


class TestNormalizeTalkId:
    def test_strips_talks_prefix(self):
        assert normalize_talk_id("speeches/H40911") == "H40911"

    def test_bare_id_unchanged(self):
        assert normalize_talk_id("H40911") == "H40911"

    def test_none_returns_none(self):
        assert normalize_talk_id(None) is None

    def test_empty_returns_none(self):
        assert normalize_talk_id("") is None

    def test_other_prefix(self):
        assert normalize_talk_id("other/H40911") == "H40911"


# ---------------------------------------------------------------------------
# ProvenanceRegistry
# ---------------------------------------------------------------------------


def _make_record(source_id="H40911", **kwargs):
    defaults = dict(
        tool="search_speeches",
        speaker="Test Speaker",
        party="S",
        date="2024-01-15",
        heading="Test heading",
        snippet="Some snippet text",
        person_id="0123456789",
    )
    defaults.update(kwargs)
    return SourceRecord(source_id=source_id, **defaults)


class TestProvenanceRegistry:
    def test_register_and_get(self):
        reg = ProvenanceRegistry()
        reg.register(_make_record("H40911"))
        assert reg.get("H40911") is not None
        assert reg.get("H40911").speaker == "Test Speaker"

    def test_get_missing_returns_none(self):
        reg = ProvenanceRegistry()
        assert reg.get("MISSING") is None

    def test_size(self):
        reg = ProvenanceRegistry()
        assert reg.size() == 0
        reg.register(_make_record("H40911"))
        reg.register(_make_record("H40912"))
        assert reg.size() == 2

    def test_dedup_by_talk_id(self):
        """Multiple speech_chunks from same talk -> single entry."""
        reg = ProvenanceRegistry()
        reg.register(_make_record("H40911", snippet="short"))
        reg.register(_make_record("H40911", snippet="a much longer snippet text here"))
        assert reg.size() == 1
        # Keeps the longer snippet
        assert reg.get("H40911").snippet == "a much longer snippet text here"

    def test_dedup_fills_missing_metadata(self):
        reg = ProvenanceRegistry()
        reg.register(_make_record("H40911", speaker=None, party=None))
        reg.register(_make_record("H40911", speaker="Real Name", party="M"))
        assert reg.get("H40911").speaker == "Real Name"
        assert reg.get("H40911").party == "M"

    def test_all_sources_preserves_order(self):
        reg = ProvenanceRegistry()
        reg.register(_make_record("C"))
        reg.register(_make_record("A"))
        reg.register(_make_record("B"))
        ids = [s.source_id for s in reg.all_sources()]
        assert ids == ["C", "A", "B"]

    def test_get_persons(self):
        reg = ProvenanceRegistry()
        reg.register(_make_record("H40911", person_id="ID1", speaker="Alice", party="S"))
        reg.register(_make_record("H40912", person_id="ID2", speaker="Bob", party="M"))
        reg.register(_make_record("H40913", person_id=None, speaker="NoId"))
        persons = reg.get_persons()
        assert len(persons) == 2
        assert persons["ID1"] == {"name": "Alice", "party": "S"}
        assert persons["ID2"] == {"name": "Bob", "party": "M"}

    def test_to_cited_sources_format(self):
        reg = ProvenanceRegistry()
        reg.register(_make_record("H40911", speaker="Alice", party="S", date="2024-01-15"))
        sources = reg.to_cited_sources(["H40911"])
        assert len(sources) == 1
        s = sources[0]
        assert s["_id"] == "speeches/H40911"
        assert s["speaker"] == "Alice"
        assert s["party"] == "S"
        assert s["date"] == "2024-01-15"
        assert s["chunk_index"] == -1

    def test_to_cited_sources_skips_unknown(self):
        reg = ProvenanceRegistry()
        reg.register(_make_record("H40911"))
        sources = reg.to_cited_sources(["H40911", "MISSING"])
        assert len(sources) == 1


# ---------------------------------------------------------------------------
# parse_and_renumber_citations
# ---------------------------------------------------------------------------


class TestParseAndRenumberCitations:
    def test_basic_renumbering(self):
        reg = ProvenanceRegistry()
        reg.register(_make_record("H40911", speaker="Alice"))
        reg.register(_make_record("GH09100", speaker="Bob"))

        text = "Claim one[src:H40911] and claim two[src:GH09100]."
        answer, sources, cited, invalid = parse_and_renumber_citations(text, reg)

        assert "[1]" in answer
        assert "[2]" in answer
        assert "[src:" not in answer
        assert len(sources) == 2
        assert sources[0]["_id"] == "speeches/H40911"
        assert sources[1]["_id"] == "speeches/GH09100"
        assert cited == ["H40911", "GH09100"]
        assert invalid == []

    def test_duplicate_src_tags_collapse(self):
        """Multiple [src:H40911] in text -> single [1]."""
        reg = ProvenanceRegistry()
        reg.register(_make_record("H40911"))

        text = "Claim[src:H40911] and more[src:H40911]."
        answer, sources, cited, invalid = parse_and_renumber_citations(text, reg)

        assert answer.startswith("Claim[1] and more[1].")
        assert len(sources) == 1
        assert cited == ["H40911"]

    def test_invalid_ids_dropped(self):
        reg = ProvenanceRegistry()
        reg.register(_make_record("H40911"))

        text = "Valid[src:H40911] and invalid[src:FAKE123]."
        answer, sources, cited, invalid = parse_and_renumber_citations(text, reg)

        assert "[1]" in answer
        assert "FAKE123" not in answer
        assert len(sources) == 1
        assert invalid == ["FAKE123"]

    def test_strips_model_generated_kallor(self):
        reg = ProvenanceRegistry()
        reg.register(_make_record("H40911"))

        text = "Claim[src:H40911].\n\n### Källor\n[1] Some model-generated stuff"
        answer, sources, cited, invalid = parse_and_renumber_citations(text, reg)

        # Model-generated Källor should be stripped and replaced
        assert "Some model-generated stuff" not in answer
        assert "### Källor" in answer  # But our server-generated one exists
        assert "[1] Test Speaker" in answer

    def test_kallor_generation(self):
        reg = ProvenanceRegistry()
        reg.register(_make_record("H40911", speaker="Alice", date="2024-01-15", heading="Om skolan"))

        text = "Claim[src:H40911]."
        answer, sources, cited, invalid = parse_and_renumber_citations(text, reg)

        assert "### Källor" in answer
        assert "[1] Alice – 2024-01-15 – Om skolan" in answer

    def test_fallback_when_no_src_tags(self):
        reg = ProvenanceRegistry()
        reg.register(_make_record("H40911"))
        reg.register(_make_record("H40912"))
        reg.register(_make_record("H40913"))

        text = "An answer with no citations at all."
        answer, sources, cited, invalid = parse_and_renumber_citations(text, reg)

        # Fallback: include sources, but no inline citations
        assert len(sources) == 3  # all 3, under the max_fallback of 5
        assert "[1]" not in answer.split("### Källor")[0]  # no fake inline cites

    def test_fallback_capped_at_max(self):
        reg = ProvenanceRegistry()
        for i in range(10):
            reg.register(_make_record(f"H{i:05d}"))

        text = "An answer with no citations."
        answer, sources, cited, invalid = parse_and_renumber_citations(
            text, reg, max_fallback=5
        )
        assert len(sources) == 5

    def test_empty_registry_no_sources(self):
        reg = ProvenanceRegistry()
        text = "An answer."
        answer, sources, cited, invalid = parse_and_renumber_citations(text, reg)
        assert sources == []
        assert "Källor" not in answer

    def test_multiple_sources_same_claim(self):
        reg = ProvenanceRegistry()
        reg.register(_make_record("A"))
        reg.register(_make_record("B"))

        text = "Multi-source claim[src:A][src:B]."
        answer, sources, cited, invalid = parse_and_renumber_citations(text, reg)

        assert "[1][2]" in answer
        assert len(sources) == 2

    def test_preserves_non_citation_brackets(self):
        reg = ProvenanceRegistry()
        reg.register(_make_record("H40911"))

        text = "Array [0] and citation[src:H40911]."
        answer, sources, cited, invalid = parse_and_renumber_citations(text, reg)

        # [0] should remain untouched
        assert "[0]" in answer
        assert "[1]" in answer

    def test_ordering_by_first_appearance(self):
        reg = ProvenanceRegistry()
        reg.register(_make_record("A"))
        reg.register(_make_record("B"))
        reg.register(_make_record("C"))

        # C appears first in the text
        text = "First[src:C] then[src:A] then[src:B]."
        answer, sources, cited, invalid = parse_and_renumber_citations(text, reg)

        assert cited == ["C", "A", "B"]
        assert sources[0]["_id"] == "speeches/C"
        assert sources[1]["_id"] == "speeches/A"
        assert sources[2]["_id"] == "speeches/B"


# ---------------------------------------------------------------------------
# _trim_snippet
# ---------------------------------------------------------------------------


class TestTrimSnippet:
    def test_short_text_unchanged(self):
        assert _trim_snippet("hello") == "hello"

    def test_long_text_trimmed(self):
        long_text = "a" * 500
        result = _trim_snippet(long_text, length=400)
        assert len(result) <= 401  # 400 + ellipsis char
        assert result.endswith("…")

    def test_whitespace_stripped(self):
        assert _trim_snippet("  hello  ") == "hello"
