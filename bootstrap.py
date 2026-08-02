"""
Ensures the project root is in sys.path and sets the working directory to the project root.

Import this module at the top of your scripts to make project modules (like utils.py) importable from anywhere.
"""

import os
import sys

PROJECT_ROOT = "/home/lasse/riksdagen"

def set_project_root() -> None:
    """
    Sets the working directory to the project root and ensures it's in sys.path.

    Returns:
        None
    """
    os.chdir(PROJECT_ROOT)
    if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)

set_project_root()
