"""Speculative live-streaming of the tool loop's final answer.

The ReAct tool loop (`ChatService`/`MpChatService`'s ``_run_tool_loop``) can't know,
before a ``generate()`` call returns, whether that call will end in ``tool_calls``
(the loop continues) or in bare ``content`` (this iteration *is* the final answer)
— both are legitimate outcomes of the same call, and a turn can even carry short
narration content before a tool call. So there is no way to decide up front
whether a given iteration is worth streaming live to the user.

``AnswerStreamFilter`` resolves this by buffering: it only forwards content once
it is implausible that the buffered text is a one-line narration blurb ("Jag
söker nu efter..."), and ``run_streaming_iteration`` uses it to speculatively
turn content deltas into ``answer_delta`` SSE events — retracting them with a
single ``answer_delta_retract`` event in the rare case a tool call shows up
after all.

Citation markers (``[src:ID]``) are never forwarded raw: their validity isn't
known until ``parse_and_renumber_citations`` runs on the complete text (it can
drop hallucinated IDs), so a marker must never reach the client, even for a
frame. This module only ever *strips* markers from the live preview — it never
renumbers them; that still happens exactly once, on the complete answer, same
as today.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.services.event_logger import log_error, log_event
from backend.services.provenance import _SRC_PATTERN
from packages.llm import ChatCompletionMessage

EventCallback = Callable[[dict[str, Any]], None]


def strip_citation_markers(text: str) -> str:
    """Remove complete ``[src:ID]``/``[[src:ID]]`` tags from a text segment.

    Used only on the live preview stream. The full raw text (markers intact)
    keeps accumulating server-side, untouched, for the one real
    ``parse_and_renumber_citations`` pass on the complete answer.
    """
    return _SRC_PATTERN.sub("", text)


class AnswerStreamFilter:
    """Buffers streamed content until it's confidently prose, not narration.

    One instance per tool-loop iteration.

    ``feed()`` is called with each raw content chunk as it arrives; it returns
    whatever is now safe to forward live (or ``None``), holding back:
      - text below the confidence gate — narration blurbs in this codebase are
        capped at 150 chars (see ``mp_chat.py``'s status-text truncation); the
        gate sits well clear of that, so a false-positive on unusually long
        narration is very unlikely, while a real multi-paragraph answer still
        clears it almost immediately;
      - a trailing bracket run that could still grow into a ``[src:...]``/
        ``[[src:...]]`` marker.
    Complete markers found in a releasable segment are stripped outright
    (never rendered, never renumbered live).
    """

    def __init__(self, min_chars: int = 400, soft_min_chars: int = 200) -> None:
        self._min_chars = min_chars
        self._soft_min_chars = soft_min_chars
        self._pending = ""  # raw text not yet past the confidence gate
        self._buffer = ""  # gate-cleared text not yet forwarded (bracket-safety only)
        self._armed = False
        self.flushed_any = False

    def feed(self, text: str) -> str | None:
        """Add a raw content chunk; return text safe to forward live now, or None."""
        if not text:
            return None
        if not self._armed:
            self._pending += text
            if not self._gate_passes(self._pending):
                return None
            self._armed = True
            self._buffer, self._pending = self._pending, ""
            log_event("answer_stream_gate_armed", buffered_chars=len(self._buffer))
        else:
            self._buffer += text
        return self._release_safe_segment()

    def flush_remainder(self) -> str | None:
        """Release whatever's still held back.

        Call once, after the final chunk of an iteration confirmed to be the
        answer (no ``tool_calls``) — safe now that the full text is known, so
        the confidence gate no longer applies. Still strips citation markers.

        A bracket run left unclosed at this point (e.g. generation was cut
        off mid-tag by a token limit) can never complete now — it's dropped
        outright rather than ever being released raw, same principle as the
        mid-stream case, just with "wait for more" replaced by "there is no
        more".
        """
        remainder = self._pending + self._buffer
        self._pending = ""
        self._buffer = ""
        if not remainder:
            return None
        remainder = remainder[: _last_unmatched_bracket_start(remainder)]
        if not remainder:
            return None
        piece = strip_citation_markers(remainder)
        if piece:
            self.flushed_any = True
        return piece or None

    def _gate_passes(self, text: str) -> bool:
        if len(text) >= self._min_chars:
            return True
        if len(text) >= self._soft_min_chars:
            # Already into a second sentence/clause — a narration blurb is
            # always a single short sentence, so more text after the first
            # sentence boundary is a strong "this is the real answer" signal.
            boundary = _first_sentence_boundary(text)
            if boundary is not None and boundary < len(text.rstrip()) - 1:
                return True
        return False

    def _release_safe_segment(self) -> str | None:
        """Release ``self._buffer`` up to the start of any trailing, still-open
        bracket run — a citation marker's validity depends on characters that
        may not have arrived yet, so it (and its double-bracket partner, if
        any) is held back until it either completes or is provably not one."""
        safe_end = _last_unmatched_bracket_start(self._buffer)
        segment, self._buffer = self._buffer[:safe_end], self._buffer[safe_end:]
        if not segment:
            return None
        piece = strip_citation_markers(segment)
        if piece:
            self.flushed_any = True
        return piece or None


def _first_sentence_boundary(text: str) -> int | None:
    for i, ch in enumerate(text):
        if ch in ".!?" and i + 1 < len(text) and text[i + 1] in " \n":
            return i
    return None


def _last_unmatched_bracket_start(text: str) -> int:
    """Index from which the text might still be an incomplete bracket marker.

    Compares the position of the last ``[`` against the last ``]``: if the
    last ``[`` comes after the last ``]`` (or there's no ``]`` at all), that
    bracket run is still open — hold from there. Also grabs an immediately
    preceding ``[`` so a ``[[src:...`` double-bracket run isn't split, leaving
    an orphaned single ``[`` on screen while its partner is held back.
    """
    last_open = text.rfind("[")
    if last_open == -1:
        return len(text)
    last_close = text.rfind("]")
    if last_close > last_open:
        return len(text)
    cutoff = last_open
    if cutoff > 0 and text[cutoff - 1] == "[":
        cutoff -= 1
    return cutoff


def run_streaming_iteration(
    llm,
    gen_kwargs: dict[str, Any],
    event_callback: EventCallback | None,
    iteration: int | None = None,
) -> ChatCompletionMessage:
    """Run one tool-loop iteration with ``stream=True``, speculatively
    forwarding the final answer live as ``answer_delta`` SSE events.

    Returns a ``ChatCompletionMessage`` shape-compatible with what a blocking
    ``generate()`` call returns — the tool loop's existing branching logic
    (tool execution, citation-hallucination retry, empty-response handling)
    needs no changes to consume it; it's a drop-in replacement for the
    blocking call at the single call site in ``_run_tool_loop``.

    Raises on API/stream failure — mirrors the blocking path's error-string
    contract (``isinstance(response, str)``) being converted to a raised
    exception, so a mid-stream failure surfaces the same way: as an SSE
    ``error`` event, via ``stream_chat_response``'s existing exception
    handling.
    """
    accumulator = llm.generate(**gen_kwargs, stream=True)
    if isinstance(accumulator, str):
        # generate() caught the error before the stream ever started.
        exc = RuntimeError(f"LLM API error: {accumulator}")
        log_error("llm_api_failure", exc, model=getattr(llm, "model", None), iteration=iteration)
        raise exc

    answer_filter = AnswerStreamFilter()
    retracted = False
    try:
        for kind, text in accumulator:
            if kind == "content":
                piece = answer_filter.feed(text)
                if piece and event_callback:
                    event_callback({"type": "answer_delta", "text": piece})
            elif kind == "tool_call":
                # The model has started emitting a tool call — whatever content
                # came before was narration, not the final answer. If any of it
                # already cleared the confidence gate and reached the client,
                # tell it to discard the speculative preview.
                if answer_filter.flushed_any and not retracted and event_callback:
                    event_callback({"type": "answer_delta_retract"})
                    retracted = True
            # "thinking" deltas aren't surfaced to the SSE consumer today,
            # matching the blocking path (reasoning_content is only logged).
    except Exception as exc:
        wrapped = RuntimeError(f"LLM API error: {exc}")
        log_error("llm_api_failure", wrapped, model=getattr(llm, "model", None), iteration=iteration)
        raise wrapped from exc

    message = accumulator.message
    if not message.tool_calls:
        # Confirmed: this iteration is the final answer. Release anything
        # still held back — the confidence gate no longer matters once the
        # full text is known.
        piece = answer_filter.flush_remainder()
        if piece and event_callback:
            event_callback({"type": "answer_delta", "text": piece})
    return message
