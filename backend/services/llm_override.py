"""User-supplied provider selection, shared by chat, MP-chat and research.

Kept deliberately light: it imports ``_llm.LLM``, pydantic and the provider
registry — nothing else. ``backend.services.research.handlers`` runs inside the
job child and must not pull in ``backend.services.chat`` (heavy import-time side
effects), so the model definition and the research LLM factory live here rather
than on the chat route.

The API key is ephemeral. It arrives in a request body, is handed to the LLM
clients, and — for research — rides the job's stdin ``secrets`` channel into the
child process. It is never written to the ``jobs`` row, and the only persisted
copy is the one the browser encrypts under the user's own key (see
``backend/routes/settings.py``).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from backend.services.provider_registry import ResolvedProvider, get_provider, get_server_api_key
from packages.llm import LLM


class ProviderOverride(BaseModel):
    """User-supplied provider selection. api_key is ephemeral — never stored."""

    provider_id: str
    api_key: str = Field(default="", max_length=400)
    smart_model: str = ""   # model for orchestrator + communicator roles
    fast_model: str = ""    # model for summarisation (falls back to smart_model)
    editor_model: str = ""  # model for the editor pass (chat only; research ignores it)


def resolve(override) -> ResolvedProvider:
    """Look up the provider config for an override, or raise ValueError.

    Accepts either a ProviderOverride or the plain dict it becomes once it has
    round-tripped through a job spec.
    """
    provider_id = override["provider_id"] if isinstance(override, dict) else override.provider_id
    provider = get_provider(provider_id)
    if provider is None:
        raise ValueError(f"Unknown provider: {provider_id!r}")
    return provider


def _field(override, name: str) -> str:
    if isinstance(override, dict):
        return override.get(name) or ""
    return getattr(override, name, "") or ""


def build_research_llms(override) -> tuple[LLM, LLM]:
    """Build the (smart, fast) pair a research job runs on from an override.

    Mirrors the model tiering and temperatures of ``_build_llms_from_env`` in
    backend/services/research/handlers.py — only the provider, models and key
    differ. Falls back to the server's own key when the override carries none,
    which is what lets a user pick a server-managed provider without handing
    over a secret.
    """
    provider = resolve(override)
    key = _field(override, "api_key") or get_server_api_key(provider.id)
    smart_model = _field(override, "smart_model") or provider.smart_model
    fast_model = _field(override, "fast_model") or provider.fast_model or smart_model

    smart = LLM(model=smart_model, base_url=provider.base_url, api_key=key, temperature=0.2)
    fast = LLM(model=fast_model, base_url=provider.base_url, api_key=key, temperature=0.1)
    return smart, fast
