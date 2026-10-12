"""
Keep the computer awake while a job is generating audio.

A long audiobook job runs for hours.  If the system idle-sleeps part way,
the connection to the speech service is cut and every chunk in flight fails;
on wake the job has usually used up its retries.  ``keep_awake()`` holds an
"I'm busy" claim for as long as the block runs — the display may still turn
off, only system sleep is held off.

It also keeps the app at full speed while hidden.  macOS App Nap lowers a
hidden app's CPU priority within about a minute, which made an offline job
behind other windows run 3-4x slower; ``NSAppSleepDisabled`` in Info.plist
no longer stops it (macOS 26+), the job has to say it is busy itself.
Windows 11 power throttling (EcoQoS) does the same to background apps.

  * macOS   — ``NSProcessInfo beginActivityWithOptions:`` (user-initiated),
              reference counted across jobs.
  * Windows — ``SetProcessInformation(ProcessPowerThrottling)`` opts the
              process out of execution-speed throttling, likewise shared.

Sleep:

  * macOS   — ``caffeinate -i -w <pid>``: one shared process, reference
              counted across jobs.  ``-w`` makes it exit with SetupTTS, so a
              crash never leaves the Mac unable to sleep.
  * Windows — ``SetThreadExecutionState``, which is per thread: each worker
              thread sets and clears its own claim.
  * Linux   — ``systemd-inhibit`` when available, shared like macOS.

Failure to take the claim is logged and otherwise ignored: a job must never
fail because the computer could not be kept awake.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator

logger = logging.getLogger(__name__)

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

_lock = threading.Lock()
_holders = 0
_process: subprocess.Popen | None = None

_speed_lock = threading.Lock()
_speed_holders = 0
_speed_token: object | None = None

# NSActivityUserInitiatedAllowingIdleSystemSleep: no App Nap, no timer
# throttling; sleep itself is handled by caffeinate above.
_NS_ACTIVITY_USER_INITIATED_ALLOWING_SLEEP = 0x00FFFFFF & ~(1 << 20)
_PROCESS_POWER_THROTTLING = 4
_POWER_THROTTLING_EXECUTION_SPEED = 0x1


def _inhibitor_command() -> list[str] | None:
    if sys.platform == "darwin":
        caffeinate = shutil.which("caffeinate") or "/usr/bin/caffeinate"
        if os.path.exists(caffeinate):
            return [caffeinate, "-i", "-w", str(os.getpid())]
        return None
    inhibit = shutil.which("systemd-inhibit")
    sleep = shutil.which("sleep")
    if inhibit and sleep:
        return [
            inhibit, "--what=idle:sleep", "--who=SetupTTS",
            "--why=Generating audio", "--mode=block", sleep, "infinity",
        ]
    return None


def _acquire_shared() -> None:
    global _holders, _process
    with _lock:
        _holders += 1
        if _process is not None and _process.poll() is None:
            return
        command = _inhibitor_command()
        if command is None:
            return
        try:
            _process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("Holding off system sleep while audio is generated")
        except OSError as exc:
            _process = None
            logger.warning("Could not keep the computer awake: %s", exc)


def _release_shared() -> None:
    global _holders, _process
    with _lock:
        _holders = max(0, _holders - 1)
        if _holders or _process is None:
            return
        proc, _process = _process, None
    with contextlib.suppress(Exception):
        proc.terminate()
        proc.wait(timeout=2)
    logger.info("System sleep allowed again")


def _set_windows_state(flags: int) -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception as exc:
        logger.warning("Could not change the Windows sleep setting: %s", exc)
        return False


class _MacActivity:
    """An NSProcessInfo activity, driven through the Objective-C runtime."""

    def __init__(self) -> None:
        import ctypes
        import ctypes.util

        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.objc_autoreleasePoolPush.restype = ctypes.c_void_p
        objc.objc_autoreleasePoolPop.argtypes = [ctypes.c_void_p]
        self._ctypes = ctypes
        self._objc = objc
        self._send_addr = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        self._info = None
        self._token = None

    def _send(self, restype, receiver, selector: bytes, *args):
        c = self._ctypes
        # arm64 objc_msgSend must be called through the exact prototype.
        proto = c.CFUNCTYPE(restype, c.c_void_p, c.c_void_p, *[t for t, _ in args])
        return proto(self._send_addr)(receiver, self._objc.sel_registerName(selector),
                                      *[v for _, v in args])

    def begin(self, reason: str) -> None:
        c = self._ctypes
        pool = self._objc.objc_autoreleasePoolPush()
        try:
            info = self._send(c.c_void_p, self._objc.objc_getClass(b"NSProcessInfo"),
                              b"processInfo")
            text = self._send(c.c_void_p, self._objc.objc_getClass(b"NSString"),
                              b"stringWithUTF8String:", (c.c_char_p, reason.encode()))
            token = self._send(c.c_void_p, info, b"beginActivityWithOptions:reason:",
                               (c.c_uint64, _NS_ACTIVITY_USER_INITIATED_ALLOWING_SLEEP),
                               (c.c_void_p, text))
            if not token:
                raise OSError("beginActivityWithOptions returned nil")
            self._token = self._send(c.c_void_p, token, b"retain")
            self._info = info
        finally:
            self._objc.objc_autoreleasePoolPop(pool)

    def end(self) -> None:
        c = self._ctypes
        if self._token is None:
            return
        pool = self._objc.objc_autoreleasePoolPush()
        try:
            self._send(None, self._info, b"endActivity:", (c.c_void_p, self._token))
            self._send(None, self._token, b"release")
        finally:
            self._token = None
            self._objc.objc_autoreleasePoolPop(pool)


def _set_windows_throttling(opt_out: bool) -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        class _ThrottlingState(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                        ("StateMask", wintypes.ULONG)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetProcessInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetProcessInformation.restype = wintypes.BOOL
        # ControlMask set + StateMask clear = never throttle; both clear =
        # back to the system's default.
        state = _ThrottlingState(1, _POWER_THROTTLING_EXECUTION_SPEED if opt_out else 0, 0)
        if not kernel32.SetProcessInformation(kernel32.GetCurrentProcess(),
                                              _PROCESS_POWER_THROTTLING,
                                              ctypes.byref(state), ctypes.sizeof(state)):
            raise OSError(ctypes.get_last_error(), "SetProcessInformation failed")
        return True
    except Exception as exc:
        logger.warning("Could not change Windows power throttling: %s", exc)
        return False


def _begin_full_speed() -> object | None:
    if sys.platform == "darwin":
        activity = _MacActivity()
        activity.begin("Generating audio")
        return activity
    if sys.platform == "win32":
        return True if _set_windows_throttling(True) else None
    return None


def _end_full_speed(token: object) -> None:
    if isinstance(token, _MacActivity):
        token.end()
    elif sys.platform == "win32":
        _set_windows_throttling(False)


def _acquire_full_speed() -> None:
    global _speed_holders, _speed_token
    with _speed_lock:
        _speed_holders += 1
        if _speed_holders > 1:
            return
        try:
            _speed_token = _begin_full_speed()
            if _speed_token is not None:
                logger.info("Holding off App Nap / power throttling while audio is generated")
        except Exception as exc:  # noqa: BLE001
            _speed_token = None
            logger.warning("Could not keep the app at full speed: %s", exc)


def _release_full_speed() -> None:
    global _speed_holders, _speed_token
    with _speed_lock:
        _speed_holders = max(0, _speed_holders - 1)
        if _speed_holders or _speed_token is None:
            return
        token, _speed_token = _speed_token, None
        try:
            _end_full_speed(token)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not end the full-speed claim: %s", exc)


@contextlib.contextmanager
def keep_awake() -> Iterator[None]:
    """Hold off idle system sleep and background throttling for the block."""
    _acquire_full_speed()
    try:
        if sys.platform == "win32":
            claimed = _set_windows_state(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
            try:
                yield
            finally:
                if claimed:
                    _set_windows_state(_ES_CONTINUOUS)
            return

        _acquire_shared()
        try:
            yield
        finally:
            _release_shared()
    finally:
        _release_full_speed()
