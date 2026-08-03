"""Locate the project root and make it importable.

Scripts under ``scripts/`` are run directly (``python scripts/make_embeddings.py``), so
the repository root is not on ``sys.path`` and relative paths would resolve against
whatever directory the caller happened to be in. Importing this module fixes both,
deriving the root from this file's own location.

The predecessor hardcoded one absolute path in 22 files, which is why it could
only ever run on a single machine.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

# Bulk source data — downloaded corpora, not repository content. Kept outside the
# tree by default so a checkout stays small; point PLENUM_DATA_DIR at an existing
# download to reuse one.
DATA_DIR = Path(os.environ.get("PLENUM_DATA_DIR") or PROJECT_ROOT / "data")


def set_project_root() -> None:
    """Put the project root on ``sys.path`` and make it the working directory."""
    root = str(PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    os.chdir(root)


set_project_root()
