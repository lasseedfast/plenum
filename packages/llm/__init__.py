"""Provider-agnostic LLM client and tool registry.

Replaces the `_llm` package the predecessor project depended on, which reached out
to a private server at import time and so could not be run by anyone else.

    from packages.llm import LLM, register_tool, get_tools

    @register_tool
    def search(query: str) -> str:
        '''Search the corpus.

        Args:
            query: What to look for.
        '''
        ...

    llm = LLM(base_url=..., model=..., tools=get_tools())
    reply = llm.generate(messages=[{"role": "user", "content": "..."}])
"""
from .client import LLM, ChatCompletionMessage, StreamAccumulator
from .config import LLMConfig
from .tools import (
    TOOL_REGISTRY,
    execute_tool,
    get_tools,
    parse_function_call_arguments,
    register_tool,
)

__all__ = [
    "LLM",
    "LLMConfig",
    "ChatCompletionMessage",
    "StreamAccumulator",
    "register_tool",
    "get_tools",
    "execute_tool",
    "parse_function_call_arguments",
    "TOOL_REGISTRY",
]
