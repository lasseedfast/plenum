import os
import sys

import bootstrap


def set_working_directory():
    """Set the working directory to project root."""
    os.chdir(str(bootstrap.PROJECT_ROOT))
    if str(bootstrap.PROJECT_ROOT) not in sys.path:
        sys.path.append(str(bootstrap.PROJECT_ROOT))
