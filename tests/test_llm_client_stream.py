"""Tests for StreamAccumulator: reconstructing a full ChatCompletionMessage
from a raw streaming ChatCompletion response, chunk by chunk."""

from types import SimpleNamespace

import pytest

from packages.llm.client import StreamAccumulator


def _chunk(content=None, reasoning=None, tool_calls=None):
    delta = SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _empty_chunk():
    """A chunk with no choices at all — providers send these sometimes."""
    return SimpleNamespace(choices=[])


def _tc(index, id=None, name=None, arguments=None):
    fn = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=id, function=fn)


class TestContentOnly:
    def test_joins_content_across_chunks(self):
        # Individual events may be split differently than the input chunks —
        # StreamAccumulator holds back a tail that could still be a partial
        # <think> tag (see TestThinkTagHandling) — so only the joined result
        # and .message are part of the contract, not the exact chunking.
        acc = StreamAccumulator(iter([_chunk(content="Hello "), _chunk(content="world.")]))
        events = list(acc)
        assert all(kind == "content" for kind, _ in events)
        assert "".join(text for _, text in events) == "Hello world."
        assert acc.message.content == "Hello world."
        assert acc.message.tool_calls is None

    def test_no_content_yields_none(self):
        acc = StreamAccumulator(iter([_chunk(content=None)]))
        list(acc)
        assert acc.message.content is None

    def test_skips_chunks_with_no_choices(self):
        acc = StreamAccumulator(iter([_empty_chunk(), _chunk(content="ok")]))
        events = list(acc)
        assert events == [("content", "ok")]
        assert acc.message.content == "ok"


class TestThinkTagHandling:
    def test_think_block_at_start(self):
        acc = StreamAccumulator(iter([_chunk(content="<think>reasoning</think>answer")]))
        list(acc)
        assert acc.message.content == "answer"
        assert acc.message.reasoning_content == "reasoning"

    def test_think_open_tag_split_across_chunks(self):
        chunks = [_chunk(content="<thi"), _chunk(content="nk>reasoning here</th"), _chunk(content="ink>answer text")]
        acc = StreamAccumulator(iter(chunks))
        list(acc)
        assert acc.message.content == "answer text"
        assert acc.message.reasoning_content == "reasoning here"

    def test_reasoning_content_field_no_inline_think(self):
        acc = StreamAccumulator(iter([_chunk(reasoning="thinking..."), _chunk(content="the answer")]))
        events = list(acc)
        assert ("thinking", "thinking...") in events
        assert acc.message.content == "the answer"
        assert acc.message.reasoning_content == "thinking..."

    def test_truncated_stream_mid_think_block_never_leaks_as_content(self):
        # A stream that ends while still inside <think>...</think> (e.g. connection
        # dropped) must never surface the unfinished reasoning as answer prose.
        acc = StreamAccumulator(iter([_chunk(content="<think>unfinished reasoning")]))
        events = list(acc)
        assert all(kind == "thinking" for kind, _ in events)
        assert acc.message.content is None
        assert acc.message.reasoning_content == "unfinished reasoning"

    def test_content_before_and_after_think_block(self):
        acc = StreamAccumulator(iter([_chunk(content="before <think>middle</think> after")]))
        list(acc)
        assert acc.message.content == "before  after"
        assert acc.message.reasoning_content == "middle"


class TestToolCalls:
    def test_single_tool_call_arguments_split_across_fragments(self):
        chunks = [
            _chunk(tool_calls=[_tc(0, id="call_1", name="search_speeches")]),
            _chunk(tool_calls=[_tc(0, arguments='{"query":')]),
            _chunk(tool_calls=[_tc(0, arguments='"AI"}')]),
        ]
        acc = StreamAccumulator(iter(chunks))
        events = list(acc)
        assert events == [("tool_call", None)]  # signalled once, on first sighting
        msg = acc.message
        assert msg.content is None
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].id == "call_1"
        assert msg.tool_calls[0].function.name == "search_speeches"
        assert msg.tool_calls[0].function.arguments == '{"query":"AI"}'

    def test_two_parallel_tool_calls_by_index(self):
        chunks = [
            _chunk(tool_calls=[_tc(0, id="call_1", name="search")]),
            _chunk(tool_calls=[_tc(0, arguments='{"q":"AI"}'), _tc(1, id="call_2", name="fetch")]),
            _chunk(tool_calls=[_tc(1, arguments='{"id":1}')]),
        ]
        acc = StreamAccumulator(iter(chunks))
        list(acc)
        msg = acc.message
        assert [tc.id for tc in msg.tool_calls] == ["call_1", "call_2"]
        assert msg.tool_calls[0].function.arguments == '{"q":"AI"}'
        assert msg.tool_calls[1].function.name == "fetch"
        assert msg.tool_calls[1].function.arguments == '{"id":1}'

    def test_tool_call_event_fires_once_per_stream(self):
        chunks = [
            _chunk(tool_calls=[_tc(0, id="call_1", name="search")]),
            _chunk(tool_calls=[_tc(0, arguments="{}"), _tc(1, id="call_2", name="fetch")]),
        ]
        acc = StreamAccumulator(iter(chunks))
        events = list(acc)
        assert events.count(("tool_call", None)) == 1

    def test_narration_content_before_tool_calls(self):
        chunks = [
            _chunk(content="Jag söker nu efter tal om AI."),
            _chunk(tool_calls=[_tc(0, id="call_1", name="search_speeches", arguments="{}")]),
        ]
        acc = StreamAccumulator(iter(chunks))
        events = list(acc)
        content_text = "".join(text for kind, text in events if kind == "content")
        assert content_text == "Jag söker nu efter tal om AI."
        assert ("tool_call", None) in events
        msg = acc.message
        assert msg.content == "Jag söker nu efter tal om AI."
        assert msg.tool_calls[0].function.name == "search_speeches"


class TestMessageAccess:
    def test_raises_if_read_before_exhaustion(self):
        acc = StreamAccumulator(iter([_chunk(content="x")]))
        with pytest.raises(RuntimeError):
            _ = acc.message

    def test_readable_after_full_iteration(self):
        acc = StreamAccumulator(iter([_chunk(content="x")]))
        for _ in acc:
            pass
        assert acc.message.content == "x"
