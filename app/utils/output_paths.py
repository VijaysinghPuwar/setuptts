"""
Validation for the export destination, done before a job is queued.

A bad destination used to surface only when the worker reached the final
assembly step — for an audiobook that can be hours in — and even then as a
generic "could not assemble" error.  Everything that can be known up front
(an illegal file name, a missing or read-only folder, an over-long Windows
path) is checked here instead, and explained in plain words.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Characters Windows forbids in file names.  They are refused on every
# platform: a file named "Part 1: Intro.mp3" made on a Mac cannot be copied to
# a Windows drive or a FAT-formatted player.
_ILLEGAL_CHARS = set('<>:"/\\|?*')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
# Without the long-paths opt-in, Win32 file APIs fail past MAX_PATH (260).
# The staged/temp files the worker writes beside the output add a suffix, so
# leave headroom.
_WINDOWS_PATH_LIMIT = 240


@dataclass(frozen=True)
class OutputPathProblem:
    title: str
    message: str


def normalise_filename(name: str) -> str:
    """Trim the name and make sure it ends in .mp3 (an empty name → output.mp3)."""
    name = (name or "").strip()
    if not name:
        return "output.mp3"
    if not name.lower().endswith(".mp3"):
        name += ".mp3"
    return name


def check_output_path(folder: str, filename: str) -> OutputPathProblem | None:
    """Return a user-facing problem with this destination, or None if it is fine."""
    name = normalise_filename(filename)
    stem = name[:-4]

    bad = sorted({c for c in name if c in _ILLEGAL_CHARS or ord(c) < 32})
    if bad:
        shown = " ".join(c if ord(c) >= 32 else "?" for c in bad)
        return OutputPathProblem(
            "Invalid File Name",
            f"The file name can't contain these characters:  {shown}\n\n"
            "Please rename the file and try again.",
        )
    if not stem.strip(" ."):
        return OutputPathProblem(
            "Invalid File Name", "Please enter a name for the audio file.",
        )
    if name != name.rstrip(" .") or stem != stem.rstrip(" ."):
        return OutputPathProblem(
            "Invalid File Name",
            "The file name can't end with a space or a period.",
        )
    if stem.split(".")[0].upper() in _RESERVED_NAMES:
        return OutputPathProblem(
            "Invalid File Name",
            f"“{stem}” is a name Windows reserves for devices. "
            "Please choose a different file name.",
        )

    if not folder:
        return OutputPathProblem(
            "No Save Location", "Please choose a folder to save the audio in.",
        )
    directory = Path(folder)
    if not directory.is_dir():
        return OutputPathProblem(
            "Save Location Not Found",
            f"The folder\n\n{folder}\n\nno longer exists. It may have been "
            "moved, renamed, or be on a drive that is not connected.\n\n"
            "Click Browse to choose where to save the audio.",
        )
    if not _is_writable(directory):
        return OutputPathProblem(
            "Can't Save Here",
            f"SetupTTS is not allowed to save files in\n\n{folder}\n\n"
            "Click Browse to choose a different folder.",
        )
    full = directory / name
    if sys.platform == "win32" and len(str(full)) > _WINDOWS_PATH_LIMIT:
        return OutputPathProblem(
            "File Path Too Long",
            "The folder path plus the file name is too long for Windows.\n\n"
            "Choose a folder closer to the top of the drive (for example "
            "Documents or Desktop), or use a shorter file name.",
        )
    if full.exists() and full.is_dir():
        return OutputPathProblem(
            "Invalid File Name",
            f"A folder named “{name}” already exists there. "
            "Please choose a different file name.",
        )
    return None


def next_free_path(path: str | Path) -> Path:
    """'book.mp3' → 'book (2).mp3', 'book (3).mp3', … — the first that doesn't exist."""
    path = Path(path)
    if not path.exists():
        return path
    stem = re.sub(r" \(\d+\)$", "", path.stem)
    for n in range(2, 10_000):
        candidate = path.with_name(f"{stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"No free file name next to {path}")


def _is_writable(directory: Path) -> bool:
    # os.access is unreliable on Windows (it ignores ACLs), so actually try.
    try:
        fd, probe = tempfile.mkstemp(prefix=".setuptts-write-test-", dir=directory)
    except OSError:
        return False
    try:
        os.close(fd)
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass
    return True
