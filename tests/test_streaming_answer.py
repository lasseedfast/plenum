"""Tests for the speculative final-answer streaming filter and driver."""

from types import SimpleNamespace

import pytest

from backend.services.streaming_answer import (
    AnswerStreamFilter,
    _last_unmatched_bracket_start,
    run_streaming_iteration,
    strip_citation_markers,
)
from packages.llm.client import StreamAccumulator

# ---------------------------------------------------------------------------
# _last_unmatched_bracket_start
# ---------------------------------------------------------------------------


class TestLastUnmatchedBracketStart:
    def test_no_brackets(self):
        text = "hello world"
        assert _last_unmatched_bracket_start(text) == len(text)

    def test_complete_single_marker(self):
        text = "Claim[src:H40911] more"
        assert _last_unmatched_bracket_start(text) == len(text)

    def test_incomplete_single_marker(self):
        text = "Claim[src:H409"
        assert _last_unmatched_bracket_start(text) == len("Claim")

    def test_incomplete_double_bracket_holds_both_brackets(self):
        text = "text [[src:AB"
        assert _last_unmatched_bracket_start(text) == len("text ")

    def test_complete_double_bracket_marker(self):
        text = "text [[src:ABC]] more"
        assert _last_unmatched_bracket_start(text) == len(text)

    def test_preserves_non_citation_bracket_once_closed(self):
        text = "Array [0] and citation[src:H"
        assert _last_unmatched_bracket_start(text) == len("Array [0] and citation")

    def test_orphan_open_bracket_before_unrelated_close_is_not_held(self):
        # A stray '[' that's already followed by a later, unrelated ']' can
        # never grow into a marker anymore — nothing to hold back.
        text = "text [ [nested] stray"
        assert _last_unmatched_bracket_start(text) == len(text)


# ---------------------------------------------------------------------------
# strip_citation_markers
# ---------------------------------------------------------------------------


class TestStripCitationMarkers:
    def test_strips_single_bracket(self):
        assert strip_citation_markers("Claim[src:H40911] more") == "Claim more"

    def test_strips_double_bracket(self):
        assert strip_citation_markers("Claim[[src:H40911]] more") == "Claim more"

    def test_leaves_non_citation_brackets(self):
        assert strip_citation_markers("Array [0] here") == "Array [0] here"


# ---------------------------------------------------------------------------
# AnswerStreamFilter
# ---------------------------------------------------------------------------


class TestAnswerStreamFilterGate:
    def test_short_narration_never_arms(self):
        f = AnswerStreamFilter(min_chars=400, soft_min_chars=200)
        assert f.feed("Jag söker nu efter tal om AI.") is None
        assert f.flushed_any is False

    def test_hard_gate_arms_past_min_chars(self):
        f = AnswerStreamFilter(min_chars=20, soft_min_chars=1000)
        out = f.feed("x" * 25)
        assert out is not None
        assert f.flushed_any is True

    def test_soft_gate_needs_a_second_sentence(self):
        f = AnswerStreamFilter(min_chars=1000, soft_min_chars=10)
        # One short sentence past soft_min_chars but no second sentence yet — held back.
        assert f.feed("A single narration sentence.") is None

    def test_soft_gate_arms_once_into_a_second_sentence(self):
        f = AnswerStreamFilter(min_chars=1000, soft_min_chars=10)
        out = f.feed("First sentence done. Second one starts here")
        assert out is not None

    def test_flush_remainder_releases_unarmed_short_answer(self):
        f = AnswerStreamFilter(min_chars=400, soft_min_chars=200)
        assert f.feed("Nej.") is None
        remainder = f.flush_remainder()
        assert remainder == "Nej."
        assert f.flushed_any is True

    def test_flush_remainder_empty_when_nothing_fed(self):
        f = AnswerStreamFilter()
        assert f.flush_remainder() is None
        assert f.flushed_any is False


class TestAnswerStreamFilterCitationSafety:
    def test_marker_split_across_feed_calls_never_leaks(self):
        f = AnswerStreamFilter(min_chars=10, soft_min_chars=5)
        f.feed("This is a long enough narration to arm the gate for sure. ")
        collected = ""
        for chunk in ["Claim one", "[src:H4", "0911]", " and more text after."]:
            piece = f.feed(chunk)
            if piece:
                assert "[src:" not in piece
                collected += piece
        assert "H40911" not in collected
        assert collected == "Claim one and more text after."

    def test_double_bracket_marker_split_never_leaks_orphan_bracket(self):
        f = AnswerStreamFilter(min_chars=10, soft_min_chars=5)
        f.feed("This is a long enough narration to arm the gate for sure. ")
        collected = ""
        for chunk in ["Claim ", "[[src:H4", "0911]]", " end."]:
            piece = f.feed(chunk)
            if piece:
                assert "[" not in piece
                collected += piece
        assert collected == "Claim  end."

    def test_incomplete_marker_at_true_end_of_stream_is_dropped_not_leaked(self):
        # The trailing "[src:H409" is never going to be completed (no more
        # chunks are coming) — flush_remainder must drop it outright rather
        # than ever releasing a raw, unclosed citation-marker fragment.
        f = AnswerStreamFilter(min_chars=10, soft_min_chars=5)
        pieces = []
        pieces.append(f.feed("Long enough narration to arm the gate for sure now. "))
        pieces.append(f.feed("Trailing claim[src:H409"))  # incomplete marker, stream ends here
        pieces.append(f.flush_remainder())
        collected = "".join(p for p in pieces if p)
        assert "[src:" not in collected
        assert "H409" not in collected
        assert "Trailing claim" in collected

    def test_flush_remainder_strips_markers_in_a_short_never_armed_answer(self):
        # Short answers never arm the gate, so their text sits in `_pending`
        # and never passes through feed()'s per-chunk bracket handling —
        # flush_remainder is the only place that ever filters it.
        f = AnswerStreamFilter(min_chars=400, soft_min_chars=200)
        assert f.feed("Enligt[src:H40911] nej.") is None
        remainder = f.flush_remainder()
        assert remainder is not None
        assert "[src:" not in remainder
        assert "H40911" not in remainder
        assert "Enligt" in remainder and "nej." in remainder


# ---------------------------------------------------------------------------
# run_streaming_iteration
# ---------------------------------------------------------------------------


def _chunk(content=None, tool_calls=None):
    delta = SimpleNamespace(content=content, reasoning_content=None, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _tc(index, id=None, name=None, arguments=None):
    fn = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=id, function=fn)


class _FakeLLM:
    """Stands in for packages.llm.LLM: generate(..., stream=True) returns a
    StreamAccumulator over a canned chunk sequence (or an error string)."""

    model = "fake-model"

    def __init__(self, chunks=None, error=None):
        self._chunks = chunks or []
        self._error = error

    def generate(self, **kwargs):
        assert kwargs.get("stream") is True
        if self._error is not None:
            return self._error
        return StreamAccumulator(iter(self._chunks))


class TestRunStreamingIteration:
    def test_long_answer_streams_deltas_and_returns_message(self):
        long_text = "Enligt riksdagens protokoll har flera ledamoter debatterat AI. " * 3
        llm = _FakeLLM(chunks=[_chunk(content=long_text)])
        events = []
        message = run_streaming_iteration(llm, {"messages": []}, events.append)

        deltas = [e for e in events if e["type"] == "answer_delta"]
        assert deltas  # at least one delta was forwarded live
        # feed()/flush_remainder() never strip whitespace (only citation
        # markers) — the raw text reconstructs exactly, unlike .message.content
        # below, which goes through the same _strip_think().strip() the
        # non-streaming path always applies.
        assert "".join(d["text"] for d in deltas) == long_text
        assert not any(e["type"] == "answer_delta_retract" for e in events)
        assert message.tool_calls is None
        assert message.content == long_text.strip()

    def test_short_answer_flushes_once_at_the_end(self):
        llm = _FakeLLM(chunks=[_chunk(content="Nej.")])
        events = []
        message = run_streaming_iteration(llm, {"messages": []}, events.append)
        deltas = [e for e in events if e["type"] == "answer_delta"]
        assert "".join(d["text"] for d in deltas) == "Nej."
        assert message.content == "Nej."

    def test_citation_marker_never_reaches_an_event(self):
        text = "Långt påstående med källa[src:H40911] och mer text för att passera spärren. "
        llm = _FakeLLM(chunks=[_chunk(content=text)])
        events = []
        run_streaming_iteration(llm, {"messages": []}, events.append)
        for e in events:
            if e["type"] == "answer_delta":
                assert "[src:" not in e["text"]

    def test_tool_call_after_long_narration_emits_exactly_one_retract(self):
        long_narration = "Jag har läst igenom flera anföranden om detta ämne och tänker nu göra en sökning. " * 3
        llm = _FakeLLM(chunks=[
            _chunk(content=long_narration),
            _chunk(tool_calls=[_tc(0, id="call_1", name="search_speeches", arguments="{}")]),
        ])
        events = []
        message = run_streaming_iteration(llm, {"messages": []}, events.append)

        retracts = [e for e in events if e["type"] == "answer_delta_retract"]
        deltas = [e for e in events if e["type"] == "answer_delta"]
        assert deltas  # the narration was long enough to have cleared the gate
        assert len(retracts) == 1
        assert message.tool_calls is not None
        assert message.tool_calls[0].function.name == "search_speeches"

    def test_short_narration_before_tool_call_never_retracts(self):
        llm = _FakeLLM(chunks=[
            _chunk(content="Jag söker nu."),
            _chunk(tool_calls=[_tc(0, id="call_1", name="search_speeches", arguments="{}")]),
        ])
        events = []
        message = run_streaming_iteration(llm, {"messages": []}, events.append)
        assert not any(e["type"] == "answer_delta" for e in events)
        assert not any(e["type"] == "answer_delta_retract" for e in events)
        assert message.tool_calls[0].function.name == "search_speeches"

    def test_generate_error_string_raises(self):
        llm = _FakeLLM(error="LLM request failed: connection refused")
        with pytest.raises(RuntimeError, match="connection refused"):
            run_streaming_iteration(llm, {"messages": []}, lambda e: None)

    def test_mid_stream_exception_raises_runtime_error(self):
        def bad_chunks():
            yield _chunk(content="partial answer text that starts fine")
            raise ConnectionError("dropped")

        llm = _FakeLLM()
        llm.generate = lambda **kwargs: StreamAccumulator(bad_chunks())
        with pytest.raises(RuntimeError, match="dropped"):
            run_streaming_iteration(llm, {"messages": []}, lambda e: None)

    def test_none_event_callback_is_safe(self):
        long_text = "Ett långt svar som absolut ska passera konfidensgränsen för strömning. " * 3
        llm = _FakeLLM(chunks=[_chunk(content=long_text)])
        message = run_streaming_iteration(llm, {"messages": []}, None)
        assert message.content == long_text.strip()
