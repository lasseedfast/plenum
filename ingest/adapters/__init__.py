"""Per-parliament adapters: source records in, plenum columns out."""
from __future__ import annotations

import importlib
from typing import Any


def load_adapter(module_path: str) -> Any:
    """Import the adapter module named in parliament.yaml `sources.adapter`."""
    try:
        return importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            f"Could not import adapter {module_path!r} from parliament.yaml. "
            f"See docs/PORTING.md for what an adapter must provide."
        ) from exc
