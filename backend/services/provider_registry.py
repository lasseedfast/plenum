"""
Provider registry: loads providers.yaml and resolves provider configs.

providers.yaml defines base URLs, model IDs, and capabilities for each
provider. API keys are never stored here — the server's own key comes from
env vars, and user-supplied keys come from the request body.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class ResolvedProvider:
    id: str
    name: str
    base_url: str
    supports_thinking: bool
    user_api_key: bool     # True = key supplied by user per-request; False = server-managed
    smart_model: str       # model id for smart + communicator roles
    fast_model: str        # model id for fast role (falls back to smart_model)


_registry: dict[str, ResolvedProvider] | None = None
_PROVIDERS_PATH = Path(__file__).parent.parent.parent / "providers.yaml"


def _load() -> dict[str, ResolvedProvider]:
    global _registry
    if _registry is not None:
        return _registry

    path = _PROVIDERS_PATH
    if not path.exists():
        _registry = {}
        return _registry

    with open(path) as f:
        data = yaml.safe_load(f)

    result: dict[str, ResolvedProvider] = {}
    for p in data.get("providers", []):
        provider_id = p["id"]
        base_url = p.get("base_url", "")

        # Resolve API key for server-managed providers (vLLM).
        # For user_api_key providers the key comes from the request, not here.

        models: list[dict] = p.get("models", [])
        smart_model = next(
            (m["id"] for m in models if m.get("role") == "smart"),
            models[0]["id"] if models else "",
        )
        fast_model = next(
            (m["id"] for m in models if m.get("role") == "fast"),
            smart_model,
        )

        result[provider_id] = ResolvedProvider(
            id=provider_id,
            name=p.get("name", provider_id),
            base_url=base_url,
            supports_thinking=p.get("supports_thinking", False),
            user_api_key=p.get("user_api_key", True),
            smart_model=smart_model,
            fast_model=fast_model,
        )

        print(f"Loaded provider config: {result[provider_id]}")

    _registry = result
    return _registry


def get_provider(provider_id: str) -> Optional[ResolvedProvider]:
    """Return the resolved provider config, or None if unknown."""
    return _load().get(provider_id)


def list_providers() -> list[ResolvedProvider]:
    """Return all configured providers."""
    return list(_load().values())


def get_server_api_key(provider_id: str) -> str | None:
    """Return the server-side API key for a provider from its env var, if configured."""
    path = _PROVIDERS_PATH
    if not path.exists():
        return None
    with open(path) as f:
        data = yaml.safe_load(f)
    for p in data.get("providers", []):
        if p["id"] == provider_id:
            env_name = p.get("server_api_key_env")
            if env_name:
                return os.getenv(env_name)
    return None
