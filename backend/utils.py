import os
import sys

def set_working_directory():
    """Set the working directory to project root."""
    os.chdir("/home/lasse/riksdagen")
    if "/home/lasse/riksdagen" not in sys.path:
        sys.path.append("/home/lasse/riksdagen")