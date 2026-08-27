import json
import threading
from collections.abc import Generator
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.services.chat import ChatService  # Import the service class
from backend.services.event_logger import log_error
from backend.services.llm_override import ProviderOverride
from postgres_client import pg

router = APIRouter(prefix="/api", tags=["chat"])

# Pydantic models for request/response validation
class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(..., min_length=1)

class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    top_k: int = Field(default=5, ge=1, le=10)
    focus_ids: list[str] | None = Field(default=None, description="Optional ids from previously shared results.")
    provider_override: ProviderOverride | None = Field(default=None, description="Optional user-supplied provider.")
    use_editor: bool = Field(default=False, description="Run the editor pass (fact-check + language polish) before returning.")
    quick: bool = Field(default=False, description="Skip the planner/Researcher pre-pass and let the orchestrator answer directly. Faster, less thorough.")
    session_id: str | None = Field(default=None, description="Browser session id; used to group opt-in TEST eval logs.")

class ChatSource(BaseModel):
    _id: str
    chunk_index: int
    heading: str | None
    url_video: str | None
    snippet: str
    speaker: str | None = None
    party: str | None = None
    person_id: str | None = None
    date: str | None = None

class PersonRef(BaseModel):
    person_id: str
    name: str
    party: str


class AttributionWarning(BaseModel):
    paragraph_idx: int
    name: str
    party: str
    cited_ns: list[int] = Field(default_factory=list)
    reason: str


class ChatResponse(BaseModel):
    answer: str
    sources: list[ChatSource]
    persons: list[PersonRef] = Field(default_factory=list)
    tables: list[dict] = Field(default_factory=list)
    focus_ids: list[str] = Field(default_factory=list)
    attribution_warnings: list[AttributionWarning] = Field(default_factory=list)

# Instantiate the chat service once (can be reused for all requests)
chat_service = ChatService()

@router.post("/chat", response_model=ChatResponse)
def chat_endpoint(payload: ChatRequest, request: Request) -> ChatResponse:
    """
    Handles chat requests from the frontend. Uses ChatService to generate a response.

    Args:
        payload (ChatRequest): The chat history and parameters from the frontend.

    Returns:
        ChatResponse: The assistant's answer and a list of sources.
    """
    # Convert Pydantic models to dicts for the service
    messages = [msg.model_dump() for msg in payload.messages]
    try:
        result = chat_service.get_chat_response(
            messages=messages,
            top_k=payload.top_k,
            focus_ids=payload.focus_ids or [],
            provider_override=payload.provider_override,
            use_editor=payload.use_editor,
            quick=payload.quick,
            session_id=payload.session_id or request.headers.get("X-Session-Id"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        import traceback
        print("UNHANDLED ERROR in chat_endpoint:", e)
        traceback.print_exc()
        log_error("http_500", e, route="/api/chat")
        raise HTTPException(status_code=500, detail=f"Internal server error: {e}") from e
    raw_answer = result.get("answer", "")
    if not isinstance(raw_answer, str):
        raw_answer = str(raw_answer)

    raw_sources = result.get("sources", [])
    if not isinstance(raw_sources, list):
        raw_sources = []

    sources = [
        ChatSource(
            _id=src.get("_id", ""),
            chunk_index=src.get("chunk_index", 0),
            heading=src.get("heading"),
            url_video=src.get("url_video"),
            snippet=src.get("snippet", ""),
            speaker=src.get("speaker"),
            party=src.get("party"),
            person_id=src.get("person_id"),
            date=src.get("date"),
        )
        for src in raw_sources
    ]
    persons = [
        PersonRef(**p) for p in result.get("persons", [])
        if isinstance(p, dict) and "person_id" in p and "name" in p
    ]
    warnings = [
        AttributionWarning(**w) for w in result.get("attribution_warnings", [])
        if isinstance(w, dict) and "paragraph_idx" in w and "name" in w
    ]
    return ChatResponse(
        answer=raw_answer,
        sources=sources,
        persons=persons,
        tables=result.get("tables", []),
        focus_ids=result.get("focus_ids", []),
        attribution_warnings=warnings,
    )


@router.post("/chat/stream")
def chat_stream_endpoint(payload: ChatRequest, request: Request) -> StreamingResponse:
    """
    SSE endpoint: streams tool-call progress events followed by the final answer.
    Each event is a line of the form:  data: <json>\\n\\n
    Event types: "tool_call", "status", "answer_delta", "answer_delta_retract",
    "answer", "error". "answer_delta" pieces are a provisional, speculative
    preview of the final answer as it's generated — see
    backend/services/streaming_answer.py.
    Using streaming avoids Cloudflare's 100-second proxy timeout for long-running queries.
    """
    messages = [msg.model_dump() for msg in payload.messages]
    session_id = payload.session_id or request.headers.get("X-Session-Id")

    def generate() -> Generator[str, None, None]:
        # SSE comment keepalive: nginx and Cloudflare buffer connections until
        # data arrives. By sending a ": keepalive\n\n" comment every 5 seconds
        # we force buffer flushes so progress hints reach the browser in real-time.
        import queue as _queue

        done_event = threading.Event()
        pipe: _queue.Queue[str] = _queue.Queue()

        def produce() -> None:
            try:
                for event in chat_service.stream_chat_response(
                    messages=messages,
                    top_k=payload.top_k,
                    focus_ids=payload.focus_ids or [],
                    provider_override=payload.provider_override,
                    use_editor=payload.use_editor,
                    quick=payload.quick,
                    session_id=session_id,
                ):
                    pipe.put(f"data: {json.dumps(event, ensure_ascii=False)}\n\n")
                    if event.get("type") in ("answer", "error"):
                        break
            except Exception as exc:
                import traceback
                traceback.print_exc()
                log_error("sse_stream_exception", exc, route="/api/chat/stream")
                pipe.put(f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n")
            finally:
                done_event.set()

        threading.Thread(target=produce, daemon=True).start()

        while not done_event.is_set() or not pipe.empty():
            try:
                chunk = pipe.get(timeout=5)
                yield chunk
            except _queue.Empty:
                # No data for 5 s — send an SSE comment to keep the connection alive
                # and force proxies/CDNs to flush their buffers.
                if not done_event.is_set():
                    yield ": keepalive\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering for SSE
        },
    )


# ── Provider list + model list proxy ─────────────────────────────────────────

@router.get("/providers")
def list_providers() -> dict:
    """Return all configured providers from providers.yaml (no secrets included)."""
    from backend.services.provider_registry import list_providers as _list
    return {
        "providers": [
            {"id": p.id, "name": p.name, "user_api_key": p.user_api_key}
            for p in _list()
        ]
    }

@router.get("/providers/{provider_id}/models")
def list_provider_models(provider_id: str, request: Request) -> dict:
    """
    Proxy GET {provider_base_url}/models with the user-supplied API key.
    The key must be passed in the X-Provider-Key header — it is never logged or stored.
    """
    import requests as _requests

    from backend.services.provider_registry import get_provider

    provider = get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id!r}")

    api_key = request.headers.get("X-Provider-Key", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="X-Provider-Key header is required")

    base = provider.base_url.rstrip("/")

    # OpenRouter supports a query param to filter by tool-capable models directly.
    params = {}
    if "openrouter.ai" in base:
        params["supported_parameters"] = "tools"

    try:
        resp = _requests.get(
            f"{base}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
            timeout=10,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach provider: {exc}") from exc

    if not resp.ok:
        raise HTTPException(status_code=resp.status_code, detail=f"Provider error: {resp.text[:200]}")

    data = resp.json()
    raw_models = [m for m in data.get("data", []) if isinstance(m, dict) and "id" in m]

    # Filter to tool-capable models. Each provider exposes this differently.
    if "berget.ai" in base:
        # Berget exposes capabilities.function_calling per model.
        raw_models = [m for m in raw_models if m.get("capabilities", {}).get("function_calling")]
    elif "openai.com" in base:
        # OpenAI /v1/models includes embeddings, TTS, image models — keep only text generation.
        _SKIP = ("whisper", "tts", "dall-e", "embedding", "moderation", "babbage", "davinci", "o1-mini", "realtime")
        raw_models = [m for m in raw_models if not any(s in m["id"] for s in _SKIP)]
    # OpenRouter already filtered by ?supported_parameters=tools above.

    models = [_model_info(m) for m in raw_models]
    models.sort(key=lambda m: m["id"])
    return {"models": models}


def _model_info(m: dict) -> dict:
    """Normalise one provider /models entry into what the settings UI shows.

    Only OpenRouter reports pricing today; for the other providers the price
    fields stay None and the UI simply leaves the cost out rather than guessing.
    """
    pricing = m.get("pricing") or {}

    def per_million(key: str) -> float | None:
        raw = pricing.get(key)
        if raw in (None, ""):
            return None
        try:
            # Providers quote USD per token, usually as a string ("0.000003").
            return float(raw) * 1_000_000
        except (TypeError, ValueError):
            return None

    top = m.get("top_provider") or {}
    architecture = m.get("architecture") or {}
    params = m.get("supported_parameters") or []

    context = m.get("context_length") or top.get("context_length") or m.get("max_context_length")
    max_output = top.get("max_completion_tokens") or m.get("max_output_tokens")

    return {
        "id": m["id"],
        "name": m.get("name") or m["id"],
        "description": (m.get("description") or "")[:400],
        "context_length": context if isinstance(context, int) else None,
        "max_output_tokens": max_output if isinstance(max_output, int) else None,
        # USD per 1M tokens, or None when the provider does not publish prices.
        "prompt_price": per_million("prompt"),
        "completion_price": per_million("completion"),
        "reasoning": bool(m.get("reasoning")) or "reasoning" in params,
        "input_modalities": architecture.get("input_modalities") or [],
    }


# ── MP (person) endpoints ──────────────────────────────────────────────────────

class Uppdrag(BaseModel):
    typ: str | None = None
    organ_kod: str | None = None
    roll_kod: str | None = None
    status: str | None = None
    uppgift: str | None = None
    from_: str | None = Field(None, alias="from")
    tom: str | None = None

    model_config = {"populate_by_name": True}


class PersonDetail(BaseModel):
    person_id: str
    name: str
    first_name: str | None = None
    last_name: str | None = None
    party: str | None = None
    constituency: str | None = None
    status: str | None = None
    image_url_medium: str | None = None
    birth_year: str | None = None
    uppdrag: list[Uppdrag] | None = None


@router.get("/person/{person_id}", response_model=PersonDetail)
def get_person(person_id: str) -> PersonDetail:
    """Fetch basic person data for a Riksdag member."""
    rows = pg.execute(
        """SELECT person_id, name, first_name, last_name, party, constituency,
                  status, image_url_medium, birth_year, assignments
           FROM people WHERE person_id = %s""",
        (person_id,)
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Person not found")
    row = dict(rows[0])
    raw = row.pop("assignments", None) or {}
    uppdrag_list = raw.get("uppdrag", []) if isinstance(raw, dict) else []
    return PersonDetail(**row, uppdrag=[Uppdrag(**u) for u in uppdrag_list])


class MpChatRequest(BaseModel):
    messages: list[ChatMessage]
    person_id: str
    initial_speech_id: str | None = None
    provider_override: ProviderOverride | None = Field(default=None, description="Optional user-supplied provider.")


@router.post("/chat/mp/stream")
def mp_chat_stream_endpoint(payload: MpChatRequest) -> StreamingResponse:
    """
    SSE streaming endpoint for chatting with an MP persona.
    The LLM role-plays as the specified person, grounded in their actual speeches.
    Same event types as /chat/stream, including the "answer_delta"/
    "answer_delta_retract" live-preview events.
    """
    from backend.services.mp_chat import MpChatService

    try:
        mp_service = MpChatService(
            person_id=payload.person_id,
            initial_speech_id=payload.initial_speech_id,
            provider_override=payload.provider_override,
        )
    except ValueError as e:
        # Same exception type covers "no such person" and "no such provider";
        # only the former should read as a missing resource.
        status = 400 if "provider" in str(e).lower() else 404
        raise HTTPException(status_code=status, detail=str(e)) from e

    messages = [msg.model_dump() for msg in payload.messages]

    def generate() -> Generator[str, None, None]:
        import queue as _queue

        done_event = threading.Event()
        pipe: _queue.Queue[str] = _queue.Queue()

        def produce() -> None:
            try:
                for event in mp_service.stream_chat_response(messages=messages):
                    pipe.put(f"data: {json.dumps(event, ensure_ascii=False)}\n\n")
                    if event.get("type") in ("answer", "error"):
                        break
            except Exception as exc:
                import traceback
                traceback.print_exc()
                log_error("sse_stream_exception", exc, route="/api/chat/mp/stream")
                pipe.put(f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n")
            finally:
                done_event.set()

        threading.Thread(target=produce, daemon=True).start()

        while not done_event.is_set() or not pipe.empty():
            try:
                chunk = pipe.get(timeout=5)
                yield chunk
            except _queue.Empty:
                if not done_event.is_set():
                    yield ": keepalive\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
