"""Opt-in conversation logging to Postgres for LLM-based evaluation.

Activated ONLY when the first user message of a conversation starts with
"TEST " (case-sensitive, followed by the real question). The prefix is
stripped before the LLM sees it so the logged conversation behaves like a
real one. Normal conversations are never stored.

Never raises — logging must not break the chat (same contract as
event_logger).
"""

import re
import threading
import time
import traceback as _tb
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_MIGRATION = Path(__file__).resolve().parents[2] / "_postgres/migrations/add_eval_conversations.sql"

_TEST_PREFIX = re.compile(r"^TEST\s+(?=\S)")
_STR_MAX = 20_000
_MAX_EVENTS = 1_000
_MAX_DOC_BYTES = 4_000_000

_table_ready = False
_ready_lock = threading.Lock()


def detect_and_strip_test_prefix(messages: list) -> Tuple[list, bool]:
    """Return (messages, is_eval), inspecting only the FIRST user message.

    On match, returns a new list where the first user message dict is a copy
    with the "TEST " prefix removed. The input is never mutated. Bare "TEST"
    (no remainder) and "TESTAR..." do not activate; "TEST" in later user
    messages is ignored.
    """
    try:
        for idx, msg in enumerate(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content") or ""
            if not isinstance(content, str) or not _TEST_PREFIX.match(content):
                return messages, False
            stripped = dict(msg)
            stripped["content"] = _TEST_PREFIX.sub("", content, count=1)
            return list(messages[:idx]) + [stripped] + list(messages[idx + 1 :]), True
        return messages, False
    except Exception:
        return messages, False


def sanitize_provider(provider_override) -> Optional[Dict[str, Any]]:
    """Whitelist provider metadata. api_key is ephemeral and NEVER stored."""
    if provider_override is None:
        return None
    out = {}
    for field in ("provider_id", "smart_model", "fast_model", "editor_model"):
        value = getattr(provider_override, field, None)
        if value:
            out[field] = value
    return out or None


def _truncate(value):
    """Recursively cap strings at _STR_MAX chars (event_logger style)."""
    if isinstance(value, str) and len(value) > _STR_MAX:
        return value[:_STR_MAX] + "…[truncated]"
    if isinstance(value, dict):
        return {k: _truncate(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_truncate(v) for v in value]
    return value


def _insert(doc: dict) -> None:
    global _table_ready
    import json

    from postgres_client import pg

    with _ready_lock:
        if not _table_ready:
            pg.execute_void(_MIGRATION.read_text())
            _table_ready = True
    pg.execute_void(
        """
        INSERT INTO eval_conversations
            (session_id, turn_index, stream, started_at, finished_at,
             duration_s, iterations, has_error, doc)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            doc["session_id"],
            doc["turn_index"],
            doc["stream"],
            doc["started_at"],
            doc["finished_at"],
            doc["duration_s"],
            doc["iterations"],
            doc["error"] is not None,
            json.dumps(doc, default=str),
        ),
    )


class ConversationRecorder:
    """Collects one chat turn's events and persists them on finish().

    record() may be called concurrently from the tool-loop thread and the
    shadow-communicator daemon threads; events arriving after finish() are
    dropped.
    """

    def __init__(
        self,
        session_id: Optional[str],
        messages: list,
        request_meta: Dict[str, Any],
        stream: bool = False,
    ):
        self.session_id = session_id or f"anon-{uuid.uuid4().hex[:12]}"
        self.messages = messages
        self.request_meta = request_meta
        self.stream = stream
        self.turn_index = sum(1 for m in messages if m.get("role") == "user")
        self.started_at = datetime.now(timezone.utc)
        self._t0 = time.monotonic()
        self._events: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._iter = 0
        self._finished = False

    def wrap(
        self, downstream: Optional[Callable[[Dict[str, Any]], None]]
    ) -> Callable[[Dict[str, Any]], None]:
        """Record every event, forwarding non-eval-only events downstream."""

        def callback(event: Dict[str, Any]) -> None:
            self.record(event)
            if downstream is not None and not event.get("_eval_only"):
                downstream(event)

        return callback

    def record(self, event: Dict[str, Any]) -> None:
        try:
            with self._lock:
                if self._finished or len(self._events) >= _MAX_EVENTS:
                    return
                compact = {
                    "i": len(self._events),
                    "t_ms": int((time.monotonic() - self._t0) * 1000),
                    **{k: v for k, v in event.items() if k != "_eval_only"},
                }
                if event.get("type") == "tool_call":
                    self._iter += 1
                    compact["iteration"] = self._iter
                self._events.append(_truncate(compact))
        except Exception:
            pass

    def finish(
        self,
        result: Optional[dict] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        try:
            with self._lock:
                if self._finished:
                    return
                self._finished = True
                events = self._events
            error_block = None
            if error is not None:
                error_block = {
                    "type": type(error).__name__,
                    "message": str(error),
                    "traceback": "".join(
                        _tb.format_exception(type(error), error, error.__traceback__)
                    )[:_STR_MAX],
                }
            doc = {
                "kind": "chat_turn",
                "session_id": self.session_id,
                "turn_index": self.turn_index,
                "stream": self.stream,
                "started_at": self.started_at.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "duration_s": round(time.monotonic() - self._t0, 2),
                "request": {"messages": _truncate(self.messages), **self.request_meta},
                "events": events,
                "iterations": self._iter,
                "result": _truncate(result) if result is not None else None,
                "error": error_block,
            }
            if self._doc_size(doc) > _MAX_DOC_BYTES:
                doc = self._degrade(doc)
            _insert(doc)
        except Exception:
            pass

    @staticmethod
    def _doc_size(doc: dict) -> int:
        import json

        return len(json.dumps(doc, default=str))

    @staticmethod
    def _degrade(doc: dict) -> dict:
        """Shrink an oversized doc: keep search_card hit ids only, trim sources."""
        for event in doc.get("events", []):
            if event.get("type") == "search_card" and isinstance(
                event.get("results"), list
            ):
                event["results"] = [
                    r.get("_id") for r in event["results"] if isinstance(r, dict)
                ]
                event["_degraded"] = True
        result = doc.get("result")
        if isinstance(result, dict) and isinstance(result.get("sources"), list):
            for src in result["sources"]:
                if isinstance(src, dict) and isinstance(src.get("snippet"), str):
                    src["snippet"] = src["snippet"][:500]
        return doc
