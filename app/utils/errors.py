"""
Turn failures into messages a non-technical user can act on.

Worker messages are written for people, but some of them carry the underlying
exception text for troubleshooting.  That text is useful in a bug report and
alarming in a dialog, so it is split off here: the dialog shows the summary,
and the technical part goes behind "Show Details…" (and is always in the log).
"""

from __future__ import annotations

import errno
import re
from dataclasses import dataclass

#: Marker the worker puts in front of raw exception text.  Everything after it
#: is technical detail.  "Details:" is the older spelling, still recognised.
DETAILS_MARKERS = ("Technical details:", "Details:")

# Raw exception text that must never be the headline of a dialog.
_TECHNICAL = re.compile(
    r"(Traceback|aiohttp|client_exceptions|ClientConnector|ClientPayload|"
    r"ContentLengthError|WSServerHandshake|edge_tts|NoAudioReceived|"
    r"ConnectionResetError|ConnectionRefusedError|OSError\(|Errno|\[WinError|"
    r"getaddrinfo|SSLCertVerification|CERTIFICATE_VERIFY_FAILED|"
    r"asyncio\.|TimeoutError|socket\.)",
)


@dataclass(frozen=True)
class FriendlyError:
    summary: str
    details: str = ""


def split_error(message: str) -> FriendlyError:
    """Separate a worker message into its readable part and its technical tail."""
    text = (message or "").strip()
    for marker in DETAILS_MARKERS:
        idx = text.find(marker)
        if idx < 0:
            continue
        head = text[:idx].rstrip()
        tail = text[idx + len(marker):].strip()
        # A tail can be followed by a human "what next" paragraph (e.g. the
        # preserved-progress note).  Keep that paragraph in the summary.
        tail, sep, after = tail.partition("\n\n")
        if sep and not _TECHNICAL.search(after):
            head = f"{head}\n\n{after.strip()}" if head else after.strip()
        elif sep:
            tail = f"{tail}\n\n{after}"
        if not head:
            head = friendly_error_text(tail)
        return FriendlyError(head, tail)

    if _TECHNICAL.search(text):
        return FriendlyError(friendly_error_text(text), text)
    return FriendlyError(text)


def friendly_error_text(raw: str | BaseException) -> str:
    """Best plain-language explanation for a raw exception or its text."""
    if isinstance(raw, BaseException):
        code = getattr(raw, "errno", None)
        if code == errno.ENOSPC:
            return _DISK_FULL
        if isinstance(raw, PermissionError):
            return _NO_PERMISSION
        raw = f"{type(raw).__name__}: {raw}"

    low = str(raw).lower()
    if "no space left" in low or "disk full" in low or "not enough space" in low \
            or "errno 28" in low or "winerror 112" in low:
        return _DISK_FULL
    if "permission" in low or "access is denied" in low or "access denied" in low \
            or "read-only" in low or "winerror 5" in low:
        return _NO_PERMISSION
    if "certificate" in low or "ssl" in low:
        return (
            "SetupTTS could not open a secure connection to the Microsoft "
            "speech service. Check that your computer's date and time are "
            "correct, then try again."
        )
    if "getaddrinfo" in low or "name or service not known" in low \
            or "nodename nor servname" in low or "could not resolve" in low \
            or "11001" in low:
        return (
            "SetupTTS could not reach the Microsoft speech service. "
            "Check your internet connection and try again."
        )
    if "timeout" in low or "timed out" in low:
        return (
            "SetupTTS could not receive audio from the speech service in time. "
            "The service may be busy — please try again in a minute."
        )
    if "noaudioreceived" in low or "no audio" in low:
        return (
            "The speech service returned no audio for this text. "
            "Try again, or choose a different voice."
        )
    if "503" in low or "429" in low or "handshake" in low:
        return (
            "The Microsoft speech service is temporarily unavailable. "
            "Please try again in a few minutes."
        )
    if "connection" in low or "network" in low or "payload" in low \
            or "reset" in low or "clientconnector" in low:
        return (
            "The connection to the Microsoft speech service was interrupted. "
            "Check your internet connection and try again."
        )
    return "Something went wrong. Please try again."


_DISK_FULL = (
    "There is not enough free disk space to save the audio. "
    "Free up some space, then try again."
)
_NO_PERMISSION = (
    "SetupTTS is not allowed to save files in that folder. "
    "Choose a different save location."
)
