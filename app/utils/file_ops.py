"""File operations that hold up on Windows."""

from __future__ import annotations

import sys
import time
from pathlib import Path


def replace_with_retry(src: str | Path, dst: str | Path, attempts: int = 5) -> None:
    """
    os.replace that rides out a brief Windows file lock.

    On Windows, antivirus scanners, search indexers and sync clients open new
    files for a moment after they are written, and a rename during that window
    fails with a sharing violation.  A few short retries turn that transient
    failure into a non-event; a file genuinely open in a media player still
    fails and is reported.
    """
    src, dst = Path(src), Path(dst)
    for attempt in range(attempts):
        try:
            src.replace(dst)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1))
