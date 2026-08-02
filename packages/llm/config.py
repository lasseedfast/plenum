"""Declarative description of an LLM endpoint.

Separating *which endpoint* from *how to call it* is what lets one process talk to
several providers at once — a self-hosted vLLM for bulk summarisation, a hosted
model for user-facing chat, and a user's own key for either.

Keys carried here are ephemeral. A user-supplied key arrives on a request, is used
for that call, and is never written to disk or the database.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Optional


@dataclass(frozen=True)
class LLMConfig:
    """Everything needed to open a connection to one model.

    Args:
        base_url: OpenAI-compatible endpoint including the ``/v1`` suffix.
        model: Default model identifier.
        api_key: Provider key. Empty means self-hosted; its presence also
            suppresses the vLLM-only sampler fields (see ``client``).
        provider: Free-form label used for logging and provider-specific quirks.
        model_fast: Cheaper model for mechanical work (summarising, tagging).
            Falls back to ``model``.
        model_smart: Stronger model for user-facing reasoning. Falls back to ``model``.
    """

    base_url: str
    model: str
    api_key: str = ""
    provider: str = ""
    model_fast: str = ""
    model_smart: str = ""
    temperature: float = 0.01
    timeout: int = 240
    max_retries: int = 4

    @classmethod
    def from_env(cls) -> "LLMConfig":
        """Build the server's default endpoint from environment variables."""
        base_url = os.getenv("LLM_DIRECT_URL", "")
        if not base_url:
            raise ValueError("LLM_DIRECT_URL is not set; see .env.example")
        return cls(
            base_url=base_url,
            model=os.getenv("LLM_MODEL", "smart"),
            api_key=os.getenv("LLM_BEARER", ""),
            provider="vllm",
            model_fast=os.getenv("LLM_MODEL_FAST", ""),
            model_smart=os.getenv("LLM_MODEL_SMART", ""),
        )

    def with_override(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> "LLMConfig":
        """Return a copy pointed at a different provider, for a single request."""
        changes = {
            k: v
            for k, v in {
                "base_url": base_url,
                "api_key": api_key,
                "model": model,
                "provider": provider,
            }.items()
            if v
        }
        return replace(self, **changes)

    def resolve(self, role: str = "default") -> str:
        """Pick the model for a role, falling back to the default model."""
        return {
            "fast": self.model_fast,
            "smart": self.model_smart,
        }.get(role, "") or self.model

    def __repr__(self) -> str:  # never let a key reach a log line
        redacted = "set" if self.api_key else "unset"
        return (
            f"LLMConfig(provider={self.provider!r}, base_url={self.base_url!r}, "
            f"model={self.model!r}, api_key=<{redacted}>)"
        )
