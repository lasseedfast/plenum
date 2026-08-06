"""A thin, provider-agnostic wrapper over the OpenAI-compatible chat API.

Works against self-hosted vLLM, OpenAI, OpenRouter, Berget, Gemini's compatibility
endpoint — anything speaking the OpenAI chat protocol. The differences between them
that actually bite in production are handled in one place here:

* vLLM accepts ``extra_body`` sampler knobs (``repetition_penalty``,
  ``chat_template_kwargs``) that hosted providers reject with a 4xx.
* OpenAI reasoning models (o1/o3/o4/gpt-5) require ``max_completion_tokens``
  instead of ``max_tokens``, and refuse any temperature other than 1.
* Some models emit ``<think>`` blocks inline in ``content`` rather than in
  ``reasoning_content``; those must never reach a user.

Error contract, preserved from the predecessor: on API failure ``generate``
returns an error *string* rather than raising. Call sites branch on
``isinstance(response, str)``.
"""
from __future__ import annotations

import json
import os
import re
import traceback
from typing import Any, Dict, Generator, List, Optional, Tuple, Type

from openai import OpenAI
from openai.types.chat import ChatCompletion
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message import (
    ChatCompletionMessage as _OpenAIChatCompletionMessage,
)
from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCall,
    Function as ToolCallFunction,
)
from pydantic import BaseModel

from .tools import execute_tool, parse_function_call_arguments

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)

# Models that reject `max_tokens` and any temperature but 1.
_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")

# Sampler fields only self-hosted vLLM understands.
_VLLM_ONLY_EXTRA_BODY = ("repetition_penalty", "chat_template_kwargs")


class ChatCompletionMessage(_OpenAIChatCompletionMessage):
    """Assistant message, extended with the structured-output fields.

    When ``generate(format=...)`` is used, ``content`` holds the *parsed model
    instance* rather than text, and ``parsed`` / ``content_text`` carry the
    instance and its raw JSON respectively.
    """

    model_config = {"extra": "allow"}


def _strip_think(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


def _is_reasoning_model(model: str) -> bool:
    name = (model or "").split("/")[-1].lower()
    return name.startswith(_REASONING_PREFIXES)


class LLM:
    """One configured connection to a chat model.

    Args:
        system_message: Seeds ``self.messages`` when no explicit history is passed.
        temperature: Default sampling temperature; overridable per call.
        model: Model identifier sent to the provider.
        base_url: OpenAI-compatible endpoint, including the ``/v1`` suffix.
        api_key: Provider key. Its presence also marks this as an *external*
            provider, which suppresses the vLLM-only ``extra_body`` fields.
        think: Default reasoning mode. When false on self-hosted vLLM, chain-of-thought
            is disabled at the template level so no reasoning tokens are generated.
    """

    def __init__(
        self,
        system_message: str = "You are an assistant.",
        temperature: float = 0.01,
        model: Optional[str] = None,
        max_length_answer: int = 3000,
        messages: Optional[List[dict]] = None,
        chat: bool = True,
        tools: Optional[list] = None,
        think: bool = False,
        timeout: int = 240,
        silent: bool = False,
        presence_penalty: float = 0.3,
        top_p: float = 0.9,
        extra_body: Optional[Dict[str, Any]] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        max_retries: int = 4,
    ) -> None:
        self.model = model or os.getenv("LLM_MODEL", "smart")
        self.system_message = system_message
        self.messages = messages or [{"role": "system", "content": system_message}]
        self.max_length_answer = max_length_answer
        self.chat = chat
        self.tools = tools or []
        self.think = think
        self.silent = silent
        self.options = {
            "temperature": temperature,
            "presence_penalty": presence_penalty,
            "top_p": top_p,
        }
        # repetition_penalty > 1.0 damps already-seen tokens; 1.2 breaks generation
        # loops without measurably hurting quality.
        self.extra_body = extra_body if extra_body is not None else {"repetition_penalty": 1.2}

        self._api_key = api_key
        self.base_url = base_url or os.getenv("LLM_DIRECT_URL") or ""
        if not self.base_url:
            raise ValueError(
                "No LLM endpoint configured. Pass base_url= or set LLM_DIRECT_URL."
            )

        # max_retries covers transient 408/409/429/5xx with exponential backoff.
        # Above the SDK default of 2 because a self-hosted vLLM engine crash returns
        # 500 and usually restarts within seconds — the extra attempts ride it out.
        self.client = OpenAI(
            base_url=self.base_url,
            api_key=self._api_key or os.getenv("LLM_BEARER", "NONE"),
            timeout=timeout,
            max_retries=max_retries,
        )

    # -- request assembly -----------------------------------------------------

    @property
    def _is_external_provider(self) -> bool:
        """A caller-supplied key means a hosted provider, not our own vLLM."""
        return bool(self._api_key)

    def _build_extra_body(self, think: Optional[bool]) -> Optional[dict]:
        body = dict(self.extra_body or {})

        effective_think = self.think if think is None else think
        if not effective_think and not self._is_external_provider:
            ctk = dict(body.get("chat_template_kwargs") or {})
            ctk["enable_thinking"] = False
            body["chat_template_kwargs"] = ctk

        if self._is_external_provider:
            for key in _VLLM_ONLY_EXTRA_BODY:
                body.pop(key, None)

        return body or None

    def _sampling_kwargs(self, model: str, temperature: Optional[float],
                         max_tokens: Optional[int]) -> dict:
        temp = self.options["temperature"] if temperature is None else temperature
        limit = max_tokens or self.max_length_answer

        if _is_reasoning_model(model):
            # These models accept only the default temperature and a different token field.
            return {"max_completion_tokens": limit}

        return {
            "temperature": temp,
            "top_p": self.options["top_p"],
            "max_tokens": limit,
        }

    def _create(self, **kwargs) -> ChatCompletion:
        """Call the API, retrying once if the token-limit field name is rejected."""
        try:
            return self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            swapped = _swap_token_param(kwargs, exc)
            if swapped is None:
                raise _friendly_error(exc, self.base_url) from exc
            return self.client.chat.completions.create(**swapped)

    # -- public API -----------------------------------------------------------

    def generate(
        self,
        query: Optional[str] = None,
        *,
        messages: Optional[List[dict]] = None,
        tools: Optional[list] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        format: Optional[Type[BaseModel]] = None,
        stream: bool = False,
        think: Optional[bool] = None,
        max_tokens: Optional[int] = None,
        extra_body: Optional[Dict[str, Any]] = None,
        auto_execute_tools: bool = True,
    ):
        """Run one completion.

        Returns a :class:`ChatCompletionMessage`, a generator of ``(kind, chunk)``
        pairs when ``stream`` is set, or an error string when the call fails.

        Args:
            query: Convenience for a single user turn; ignored if ``messages`` is given.
            messages: Full conversation to send. Also becomes this instance's history.
            tools: Tool schemas to advertise. Defaults to the instance's tools.
            format: Pydantic model requesting structured output via JSON schema.
            auto_execute_tools: Execute returned tool calls and append their results
                to the history. It does *not* continue the conversation — the caller
                decides whether to call again.
        """
        if messages is not None:
            self.messages = list(messages)
        elif query is not None:
            self.messages.append({"role": "user", "content": query})

        resolved_model = model or self.model
        if extra_body is not None:
            self.extra_body = extra_body

        try:
            if format is not None:
                return self._generate_structured(
                    resolved_model, format, temperature, max_tokens, think
                )

            request = {
                "model": resolved_model,
                "messages": self.messages,
                "extra_body": self._build_extra_body(think),
                **self._sampling_kwargs(resolved_model, temperature, max_tokens),
            }
            tools_to_use = self.tools if tools is None else tools
            if tools_to_use:
                request["tools"] = tools_to_use
            if stream:
                request["stream"] = True
                return StreamAccumulator(self.client.chat.completions.create(**request))

            message = self._create(**request).choices[0].message

            if auto_execute_tools and getattr(message, "tool_calls", None):
                self._run_tool_calls(message.tool_calls)

            if isinstance(message.content, str):
                message.content = _strip_think(message.content)

            self.messages.append({"role": "assistant", "content": message.content})
            if not self.chat:
                self.messages = self.messages[:1]
            return message

        except Exception as exc:
            if not self.silent:
                traceback.print_exc()
            return f"LLM request failed: {exc}"

    # -- structured output ----------------------------------------------------

    def _generate_structured(
        self,
        model: str,
        format: Type[BaseModel],
        temperature: Optional[float],
        max_tokens: Optional[int],
        think: Optional[bool],
    ) -> ChatCompletionMessage:
        # vLLM's json_schema mode rejects `role: tool` turns, so fold them into
        # user turns. Done on a copy — the caller's history stays intact.
        messages = [
            {"role": "user", "content": f"Tool output:\n{m.get('content', '')}"}
            if m.get("role") == "tool" else m
            for m in self.messages
        ]

        response = self._create(
            model=model,
            messages=messages,
            extra_body=self._build_extra_body(think),
            response_format={
                "type": "json_schema",
                "json_schema": {"name": format.__name__, "schema": format.model_json_schema()},
            },
            **self._sampling_kwargs(model, temperature, max_tokens),
        )

        content_text = response.choices[0].message.content or ""
        parsed = format.model_validate_json(_extract_json(content_text))

        message = ChatCompletionMessage.model_construct(role="assistant", content=parsed)
        message.parsed = parsed
        message.parsed_dict = parsed.model_dump()
        message.content_text = content_text
        return message

    # -- tool execution -------------------------------------------------------

    def _run_tool_calls(self, tool_calls) -> None:
        """Execute each returned tool call, appending results as `tool` messages.

        A failing tool appends its error rather than raising, so the model can see
        what went wrong and correct itself on the next turn.
        """
        for call in tool_calls:
            fn = getattr(call, "function", None)
            if fn is None:
                continue
            name = getattr(fn, "name", None)
            try:
                args = parse_function_call_arguments(getattr(fn, "arguments", None))
                result = execute_tool(name, args)
                content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            except Exception as exc:
                if not self.silent:
                    print(f"[llm] tool {name} failed: {exc}")
                content = json.dumps({"error": str(exc)}, ensure_ascii=False)
            self.messages.append({"role": "tool", "name": name or "unknown", "content": content})


# -- streaming ------------------------------------------------------------


class StreamAccumulator:
    """Wraps a raw streaming ``ChatCompletion`` response.

    Iterate it for ``("thinking" | "content" | "tool_call", text)`` events as
    they arrive — ``text`` is ``None`` for a ``"tool_call"`` event, which just
    signals that the model has started emitting a tool call (the payload
    itself is accumulated internally; read it from ``.message`` once the
    iterator is exhausted). It fires once per stream, on the first tool-call
    delta seen, which is what a caller speculatively streaming ``content``
    live needs to know: the instant tool-call deltas start arriving, whatever
    ``content`` came before was narration, not a final answer.

    Once the iterator is exhausted, ``.message`` returns a reconstructed
    :class:`ChatCompletionMessage` — the same shape a blocking
    ``generate()`` call returns (``content``, ``tool_calls``,
    ``reasoning_content``) — so callers can treat a streamed and a blocking
    call identically once the stream is done.
    """

    # Neither <think> nor </think> can be split into more pieces than their
    # own length, so this is the longest prefix of either tag we might need
    # to hold back across a chunk boundary while waiting to see the rest.
    _MAX_TAG_LEN = max(len("<think>"), len("</think>"))

    def __init__(self, response) -> None:
        self._response = response
        self._exhausted = False
        self._content_parts: List[str] = []
        self._reasoning_parts: List[str] = []
        self._tool_calls: Dict[int, Dict[str, Any]] = {}
        self._pending = ""  # carry-over buffer, tag-boundary-safe
        self._in_think_block = False

    def __iter__(self) -> Generator[Tuple[str, Optional[str]], None, None]:
        for chunk in self._response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                self._reasoning_parts.append(reasoning)
                yield "thinking", reasoning

            tool_call_deltas = getattr(delta, "tool_calls", None)
            if tool_call_deltas:
                is_first_sighting = not self._tool_calls
                for tc in tool_call_deltas:
                    self._accumulate_tool_call(tc)
                if is_first_sighting:
                    yield "tool_call", None

            text = getattr(delta, "content", None)
            if text:
                yield from self._feed_content(text)

        yield from self._flush_pending()
        self._exhausted = True

    def _accumulate_tool_call(self, tc) -> None:
        entry = self._tool_calls.setdefault(tc.index, {"id": None, "name": None, "arguments": ""})
        if getattr(tc, "id", None):
            entry["id"] = tc.id
        fn = getattr(tc, "function", None)
        if fn is not None:
            if getattr(fn, "name", None):
                entry["name"] = fn.name
            if getattr(fn, "arguments", None):
                entry["arguments"] += fn.arguments

    def _feed_content(self, text: str) -> Generator[Tuple[str, str], None, None]:
        self._pending += text
        while True:
            piece = self._extract_safe_piece()
            if piece is None:
                break
            kind, safe_text = piece
            if not safe_text:
                continue
            (self._reasoning_parts if kind == "thinking" else self._content_parts).append(safe_text)
            yield kind, safe_text

    def _extract_safe_piece(self) -> Optional[Tuple[str, str]]:
        """Pull one provably-safe ``(kind, text)`` piece off ``self._pending``.

        Returns ``None`` if what remains might still be a partial ``<think>``/
        ``</think>`` tag — i.e. there's nothing safe to release yet.
        """
        tag = "</think>" if self._in_think_block else "<think>"
        idx = self._pending.find(tag)
        if idx != -1:
            before, after = self._pending[:idx], self._pending[idx + len(tag):]
            self._pending = after
            kind = "thinking" if self._in_think_block else "content"
            self._in_think_block = not self._in_think_block
            return kind, before

        # No full tag in the buffer yet — release everything except a tail
        # that could still grow into one on the next chunk.
        safe_len = len(self._pending) - (self._MAX_TAG_LEN - 1)
        if safe_len <= 0:
            return None
        kind = "thinking" if self._in_think_block else "content"
        safe_text, self._pending = self._pending[:safe_len], self._pending[safe_len:]
        return kind, safe_text

    def _flush_pending(self) -> Generator[Tuple[str, str], None, None]:
        if self._pending:
            # A still-open think block at EOF means a truncated/malformed
            # stream — surface the remainder as "thinking" so it can never
            # leak as prose, and let normal error handling deal with the
            # truncation itself.
            kind = "thinking" if self._in_think_block else "content"
            (self._reasoning_parts if kind == "thinking" else self._content_parts).append(self._pending)
            yield kind, self._pending
            self._pending = ""

    @property
    def message(self) -> ChatCompletionMessage:
        if not self._exhausted:
            raise RuntimeError(
                "StreamAccumulator.message read before the stream was exhausted — "
                "iterate the accumulator fully first."
            )
        content = _strip_think("".join(self._content_parts)) if self._content_parts else None
        tool_calls = None
        if self._tool_calls:
            tool_calls = [
                ChatCompletionMessageToolCall(
                    id=entry["id"] or f"call_{index}",
                    type="function",
                    function=ToolCallFunction(name=entry["name"] or "", arguments=entry["arguments"]),
                )
                for index, entry in sorted(self._tool_calls.items())
            ]
        message = ChatCompletionMessage.model_construct(
            role="assistant", content=content, tool_calls=tool_calls
        )
        message.reasoning_content = "".join(self._reasoning_parts) or None
        return message


# -- module helpers -----------------------------------------------------------


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a response that may carry stray prose."""
    text = _strip_think(text).strip()
    if text.startswith("{"):
        return text
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return match.group(0) if match else text


def _swap_token_param(kwargs: dict, exc: Exception) -> Optional[dict]:
    """Retry payload with the other token-limit field, if that's what was rejected.

    Providers disagree about `max_tokens` vs `max_completion_tokens`, and the model
    lists that require each keep changing. Rather than track them, react to the error.
    """
    text = str(exc).lower()
    if "max_tokens" not in text and "max_completion_tokens" not in text:
        return None

    alternates = {"max_tokens": "max_completion_tokens", "max_completion_tokens": "max_tokens"}
    for current, replacement in alternates.items():
        if current in kwargs:
            swapped = dict(kwargs)
            swapped[replacement] = swapped.pop(current)
            swapped.pop("temperature", None)  # reasoning models reject it too
            return swapped
    return None


def _friendly_error(exc: Exception, base_url: str) -> Exception:
    """Turn provider errors into something a deployer can act on."""
    text = str(exc).lower()
    if any(s in text for s in ("connection", "timeout", "refused", "unreachable")):
        return RuntimeError(f"LLM endpoint unreachable at {base_url}. Is the server running? ({exc})")
    if any(s in text for s in ("401", "403", "invalid api key", "unauthorized")):
        return RuntimeError(f"LLM provider rejected the API key. ({exc})")
    return exc
