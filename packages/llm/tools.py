"""Turn ordinary Python functions into OpenAI-compatible tool schemas.

A decorated function's Google-style docstring becomes the tool description the
model reads, and its type annotations become the JSON schema. That means tool
documentation lives next to the implementation and cannot drift from it.

    @register_tool
    def search(query: str, limit: int = 10) -> str:
        '''Search the speech corpus.

        Args:
            query: Words to search for.
            limit: Maximum number of hits.
        '''

Pass ``description=`` to override the docstring — used to load country-specific
tool prose from ``prompts/tools/*.md`` while keeping the ``Args:`` parsing.
"""
from __future__ import annotations

import ast
import inspect
import json
import re
import types
from collections.abc import Callable, Iterable
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

TOOL_REGISTRY: dict[str, dict[str, Any]] = {}

_NoneType = type(None)


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """Reduce ``Optional[X]`` / ``X | None`` to ``(X, True)``.

    Without this, ``get_origin(Optional[list[str]])`` is ``Union`` rather than
    ``list``, so a list parameter would be advertised to the model as a string
    and the list coercion in :func:`execute_tool` would never fire.
    """
    if get_origin(annotation) is Union or isinstance(annotation, types.UnionType):
        args = [a for a in get_args(annotation) if a is not _NoneType]
        optional = len(args) != len(get_args(annotation))
        if not args:
            return str, optional
        # A genuine multi-type union (e.g. Union[str, List[str]]) has no single
        # JSON type; describe it by its first member, which is what callers coerce to.
        return args[0], optional
    return annotation, False


def _pytype_to_jsonschema(annotation: Any) -> dict:
    annotation, _ = _unwrap_optional(annotation)

    origin = get_origin(annotation)
    if origin in (list, list):
        args = get_args(annotation)
        return {"type": "array", "items": _pytype_to_jsonschema(args[0] if args else str)}

    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return {"type": "object", **annotation.model_json_schema()}

    return {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
        dict: {"type": "object"},
        list: {"type": "array", "items": {"type": "string"}},
    }.get(annotation, {"type": "string"})


_SECTION_HEADINGS = frozenset(
    {"returns", "return", "raises", "raise", "yields", "yield",
     "examples", "example", "notes", "note"}
)
_PARAM_RE = re.compile(r"^(\w+)\s*(?:\(([^)]+)\))?\s*:\s*(.*)$")


def _parse_google_docstring(docstring: str | None) -> dict:
    """Split a Google-style docstring into a description and per-parameter docs.

    Everything outside the ``Args:`` block becomes the description, so ``Returns:``
    and ``Examples:`` sections still reach the model.
    """
    if not docstring:
        return {"description": "", "params": {}}

    lines = [ln.rstrip() for ln in docstring.splitlines()]

    args_start = next(
        (i for i, ln in enumerate(lines) if ln.strip().lower() in ("args:", "arguments:")),
        None,
    )
    args_end = len(lines)
    if args_start is not None:
        for i in range(args_start + 1, len(lines)):
            stripped = lines[i].strip().lower()
            if stripped.endswith(":") and stripped.rstrip(":") in _SECTION_HEADINGS:
                args_end = i
                break

    if args_start is None:
        description = " ".join(ln.strip() for ln in lines if ln.strip())
        return {"description": description.strip(), "params": {}}

    desc_parts = [lines[i].strip() for i in range(args_start) if lines[i].strip()]
    desc_parts += [lines[i].strip() for i in range(args_end, len(lines)) if lines[i].strip()]

    params: dict[str, dict] = {}
    i = args_start + 1
    while i < args_end:
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        m = _PARAM_RE.match(line)
        if not m:
            i += 1
            continue
        desc = m.group(3)
        j = i + 1
        while j < args_end:
            nxt = lines[j].strip()
            if not nxt or _PARAM_RE.match(nxt):
                break
            desc += " " + nxt
            j += 1
        params[m.group(1)] = {"description": desc.strip(), "type": m.group(2)}
        i = j

    return {"description": " ".join(desc_parts).strip(), "params": params}


def _openai_function_schema(name: str, description: str, parameters: dict) -> dict:
    params = dict(parameters)
    if params.get("type") != "object":
        params = {
            "type": "object",
            "properties": params.get("properties", params),
            "required": params.get("required", []),
        }
    params.setdefault("additionalProperties", False)
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": params},
    }


def register_tool(
    func: Callable | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    schema: dict | None = None,
    aliases: Iterable[str] = (),
):
    """Register a function as an LLM-callable tool.

    Args:
        name: Tool name advertised to the model. Defaults to the function name.
        description: Overrides the docstring description. ``Args:`` parsing still
            applies, so parameter docs keep coming from the docstring.
        schema: Replaces generated parameter schema wholesale.
        aliases: Extra names resolving to the same callable. Excluded from
            :func:`get_tools`, so a renamed tool keeps working when an old name
            is replayed from a persisted conversation.
    """

    def _register(f: Callable) -> Callable:
        fname = name or f.__name__
        doc = _parse_google_docstring(f.__doc__)

        if schema is not None:
            func_schema = schema
        else:
            props, required = {}, []
            for pname, param in inspect.signature(f).parameters.items():
                ann = param.annotation if param.annotation is not inspect.Parameter.empty else str
                prop = _pytype_to_jsonschema(ann)
                if pname in doc["params"]:
                    prop["description"] = doc["params"][pname]["description"]
                props[pname] = prop
                if param.default is inspect.Parameter.empty:
                    required.append(pname)
            func_schema = {
                "type": "object",
                "properties": props,
                "required": required,
                "additionalProperties": False,
            }

        entry = {
            "callable": f,
            "schema": _openai_function_schema(fname, description or doc["description"] or "", func_schema),
            "hidden": False,
        }
        TOOL_REGISTRY[fname] = entry
        for alias in aliases:
            TOOL_REGISTRY[alias] = {**entry, "hidden": True}
        return f

    return _register if func is None else _register(func)


def get_tools(
    specific_tools: list[str] | None = None,
    exclude_tools: list[str] | None = None,
) -> list[dict]:
    """Return the OpenAI-format tool list to advertise to a model.

    Hidden aliases are never advertised — they exist only so replayed tool calls
    that use a retired name still resolve.
    """
    if specific_tools and exclude_tools:
        raise ValueError("Pass specific_tools or exclude_tools, not both")

    if isinstance(specific_tools, str):
        specific_tools = [specific_tools]

    if specific_tools:
        return [TOOL_REGISTRY[t]["schema"] for t in specific_tools if t in TOOL_REGISTRY]

    visible = [e["schema"] for e in TOOL_REGISTRY.values() if not e.get("hidden")]
    if exclude_tools:
        excluded = set(exclude_tools)
        return [t for t in visible if t["function"]["name"] not in excluded]
    return visible


def parse_function_call_arguments(raw: Any) -> dict:
    """Best-effort recovery of a tool-call argument payload.

    Models sometimes emit not-quite-JSON. Try strict JSON, then Python literals,
    then the first embedded object, before giving up and handing back the raw text
    so the caller can surface a useful error.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {"_raw_unexpected": str(type(raw)), "value": raw}

    for parse in (json.loads, ast.literal_eval):
        try:
            parsed = parse(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if m:
        for parse in (json.loads, ast.literal_eval):
            try:
                parsed = parse(m.group(0))
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass

    return {"_raw": raw}


def execute_tool(name: str, args: dict) -> Any:
    """Invoke a registered tool, coercing arguments to the declared types."""
    entry = TOOL_REGISTRY.get(name)
    if not entry:
        raise RuntimeError(f"Tool {name!r} is not registered")

    fn = entry["callable"]
    kwargs = {}
    for pname, param in inspect.signature(fn).parameters.items():
        if pname not in args:
            continue
        val = args[pname]
        ann, _ = _unwrap_optional(
            param.annotation if param.annotation is not inspect.Parameter.empty else None
        )
        # Models frequently send a comma-separated string where a list is declared.
        if get_origin(ann) in (list, list) or ann is list:
            if isinstance(val, str):
                val = [x.strip() for x in val.split(",") if x.strip()]
        kwargs[pname] = val

    return fn(**kwargs)
