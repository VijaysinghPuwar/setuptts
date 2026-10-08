"""
Speech voices built into the operating system.

Windows: every SAPI voice (the "Desktop" voices, Cortana, third-party SAPI
voices) plus the OneCore voices Windows installs for Narrator and language
packs (David, Mark, Zira, …).  They are driven through SAPI by a small
PowerShell helper, so no extra package is needed and a misbehaving
third-party voice can only crash the helper, never the app.

macOS: the voices `say` knows (System Settings ▸ Accessibility ▸ Spoken
Content ▸ System Voice ▸ Manage Voices).

These voices work offline.  The audio comes back as 16-bit PCM, which
`local_tts.Mp3Writer` encodes so system-voice exports are ordinary MP3s.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

#: ShortName prefix that marks a voice as built into the OS.
SYSTEM_PREFIX = "system:"
SAMPLE_RATE = 22_050

_LIST_TIMEOUT_S = 25
#: Generous: a slow SAPI voice renders roughly 20x realtime, and chunks are
#: kept around a minute of speech.
_SPEAK_TIMEOUT_S = 180

# Display names keyed by ShortName, filled when the voice list is read so
# that labels for history rows and job cards (which only keep the ShortName)
# read "Cortana", not a registry path.
_DISPLAY_NAMES: dict[str, str] = {}
_NAMES_LOCK = threading.Lock()


def is_system_voice(short_name: str | None) -> bool:
    return bool(short_name) and short_name.startswith(SYSTEM_PREFIX)


def system_display_name(short_name: str) -> str:
    """'Cortana', 'David (Desktop)'… for a system ShortName."""
    with _NAMES_LOCK:
        name = _DISPLAY_NAMES.get(short_name)
    if name:
        return name
    # Not listed this session (voice removed, or list not loaded yet):
    # derive something readable from the id.
    tail = short_name[len(SYSTEM_PREFIX):]
    tail = re.split(r"[\\/]", tail)[-1]
    # TTS_MS_EN-US_DAVID_11.0 → David;  MSTTS_V110_enUS_MarkM → Mark
    tail = re.sub(r"^(TTS_MS_|MSTTS_V\d+_)", "", tail)
    tail = re.sub(r"_\d+(\.\d+)?$", "", tail)
    tail = re.sub(r"^[A-Za-z]{2}-?[A-Za-z]{2}_", "", tail)
    if re.fullmatch(r"[A-Z][a-z]+M", tail):
        tail = tail[:-1]
    tail = tail.replace("_", " ").strip()
    return (tail.title() if tail.isupper() else tail) or "System voice"


def _register(short_name: str, display: str) -> None:
    with _NAMES_LOCK:
        _DISPLAY_NAMES[short_name] = display


def _rate_multiplier(rate: str) -> float:
    match = re.fullmatch(r"\s*([+-]?\d+)\s*%\s*", rate or "")
    pct = int(match.group(1)) if match else 0
    return max(0.25, 1.0 + pct / 100.0)


def sapi_rate(rate: str) -> int:
    """
    SAPI's -10..10 rate for an edge-style rate string ("+25%").

    SAPI speeds up roughly 3x from 0 to +10 (and slows 3x to -10), so
    rate = 10 · log₃(multiplier).
    """
    value = 10 * math.log(_rate_multiplier(rate)) / math.log(3)
    return max(-10, min(10, round(value)))


def say_wpm(rate: str) -> int:
    """`say -r` words per minute for an edge-style rate string."""
    return max(60, min(500, round(180 * _rate_multiplier(rate))))


def _clean_label(desc: str) -> tuple[str, str]:
    """('David (Desktop)', 'English (United States)') from a SAPI description."""
    name, _, lang = desc.partition(" - ")
    name = re.sub(r"^Microsoft\s+", "", name.strip())
    name = re.sub(r"\s+Desktop$", " (Desktop)", name)
    return name or desc, lang.strip()


# ══════════════════════════════════════════════════════════════════════ #
#  Listing                                                               #
# ══════════════════════════════════════════════════════════════════════ #

_WIN_LIST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$out = New-Object System.Collections.ArrayList
foreach ($path in @(
    'HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices',
    'HKEY_CURRENT_USER\SOFTWARE\Microsoft\Speech\Voices',
    'HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices')) {
  try {
    $cat = New-Object -ComObject SAPI.SpObjectTokenCategory
    $cat.SetId($path, $false)
    foreach ($t in $cat.EnumerateTokens()) {
      $lang = ''; $gender = ''
      try { $lang = $t.GetAttribute('Language') } catch {}
      try { $gender = $t.GetAttribute('Gender') } catch {}
      $locale = ''
      if ($lang) {
        try { $locale = [Globalization.CultureInfo]::GetCultureInfo(
                [Convert]::ToInt32(($lang -split ';')[0], 16)).Name } catch {}
      }
      [void]$out.Add([pscustomobject]@{
        id = $t.Id; desc = $t.GetDescription(0); locale = $locale;
        gender = $gender; onecore = ($path -like '*OneCore*') })
    }
  } catch {}
}
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::Out.Write((ConvertTo-Json -InputObject @($out) -Compress))
"""

# One request per line on stdin (ASCII JSON), one reply line per request:
# "OK" or "ERR <message>".  Kept running for a whole job so PowerShell's
# start-up cost is paid once, not per chunk.
_WIN_SERVE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$voice = New-Object -ComObject SAPI.SpVoice
$current = ''
[Console]::Out.WriteLine('READY'); [Console]::Out.Flush()
while ($null -ne ($line = [Console]::In.ReadLine())) {
  try {
    $req = $line | ConvertFrom-Json
    if ($req.voice -ne $current) {
      $tok = New-Object -ComObject SAPI.SpObjectToken
      $tok.SetId($req.voice, '', $false)
      $voice.Voice = $tok
      $current = $req.voice
    }
    $voice.Rate = [int]$req.rate
    $voice.Volume = 100
    $stream = New-Object -ComObject SAPI.SpFileStream
    $stream.Format.Type = 22
    $stream.Open($req.wav, 3, $false)
    try {
      $voice.AudioOutputStream = $stream
      $text = [IO.File]::ReadAllText($req.text, [Text.Encoding]::UTF8)
      [void]$voice.Speak($text, 16)
    } finally { $stream.Close() }
    [Console]::Out.WriteLine('OK')
  } catch {
    [Console]::Out.WriteLine('ERR ' + ($_.Exception.Message -replace '[\r\n]+', ' '))
  }
  [Console]::Out.Flush()
}
"""


def _powershell() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    exe = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(exe) if exe.exists() else "powershell.exe"


def _ps_args(script: str) -> list[str]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _list_windows() -> list[dict]:
    proc = subprocess.run(
        _ps_args(_WIN_LIST_SCRIPT), capture_output=True, timeout=_LIST_TIMEOUT_S,
        stdin=subprocess.DEVNULL, creationflags=_no_window(),
    )
    raw = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or not raw:
        raise RuntimeError(
            f"Listing system voices failed (exit {proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()[:300]}"
        )
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]

    voices: list[dict] = []
    for item in data:
        token_id = str(item.get("id") or "")
        if not token_id:
            continue
        name, lang_label = _clean_label(str(item.get("desc") or token_id))
        locale = str(item.get("locale") or "") or "en-US"
        gender = str(item.get("gender") or "")
        gender = gender if gender in ("Female", "Male") else ""
        voices.append({
            "ShortName": SYSTEM_PREFIX + token_id,
            "FriendlyName": f"{name} (Windows) - {lang_label or locale}",
            "Locale": locale,
            "Gender": gender,
            "Source": "system",
            "_display": name,
        })
    return _dedupe_names(voices)


_SAY_LINE = re.compile(r"^(?P<name>.+?)\s+(?P<loc>[a-z]{2,3}(?:[_-][A-Za-z0-9]+)+)\s+#")


def _list_macos() -> list[dict]:
    proc = subprocess.run(["say", "-v", "?"], capture_output=True, timeout=_LIST_TIMEOUT_S,
                          stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError(f"`say -v ?` failed (exit {proc.returncode})")
    voices: list[dict] = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        m = _SAY_LINE.match(line.strip())
        if not m:
            continue
        name = m.group("name").strip()
        locale = m.group("loc").replace("_", "-")
        voices.append({
            "ShortName": SYSTEM_PREFIX + name,
            "FriendlyName": f"{name} (macOS) - {locale}",
            "Locale": locale,
            "Gender": "",
            "Source": "system",
            "_display": name,
        })
    return _dedupe_names(voices)


def _dedupe_names(voices: list[dict]) -> list[dict]:
    """Register display names, suffixing repeats so every entry is distinct."""
    counts: dict[tuple[str, str], int] = {}
    for v in voices:
        key = (v["_display"], v["Locale"])
        counts[key] = counts.get(key, 0) + 1
        if counts[key] > 1:
            v["_display"] = f'{v["_display"]} ({counts[key]})'
        _register(v["ShortName"], v.pop("_display"))
    return voices


def list_system_voices() -> list[dict]:
    """
    Voices installed in the OS, as edge-style dicts with ``Source='system'``.

    Never raises: a machine without usable system speech just has none.
    """
    try:
        if sys.platform == "win32":
            voices = _list_windows()
        elif sys.platform == "darwin":
            voices = _list_macos()
        else:
            voices = []
    except Exception:  # noqa: BLE001 - optional feature, must not break startup
        logger.warning("Could not list system voices", exc_info=True)
        return []
    logger.info("Found %d system voices", len(voices))
    return voices


# ══════════════════════════════════════════════════════════════════════ #
#  Synthesis                                                             #
# ══════════════════════════════════════════════════════════════════════ #

class SystemSpeechError(RuntimeError):
    pass


class SystemSpeechCancelled(SystemSpeechError):
    pass


def _read_wav(path: Path) -> bytes:
    """16-bit mono PCM at SAMPLE_RATE from a WAV file."""
    with wave.open(str(path), "rb") as wf:
        channels, width, rate = wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    if width != 2:
        raise SystemSpeechError(f"Unexpected sample width {width} from system voice")
    if channels == 2:
        import array
        a = array.array("h", frames)
        frames = array.array("h", ((a[i] + a[i + 1]) // 2 for i in range(0, len(a), 2))).tobytes()
    elif channels != 1:
        raise SystemSpeechError(f"Unexpected channel count {channels} from system voice")
    if rate != SAMPLE_RATE:
        frames = _resample(frames, rate, SAMPLE_RATE)
    return frames


def _resample(frames: bytes, src: int, dst: int) -> bytes:
    """Linear resample of 16-bit mono PCM (rare: only for odd voice formats)."""
    import array
    a = array.array("h", frames)
    if not a:
        return b""
    n_out = int(len(a) * dst / src)
    step = src / dst
    out = array.array("h", bytes(2 * n_out))
    last = len(a) - 1
    for i in range(n_out):
        pos = i * step
        j = int(pos)
        frac = pos - j
        s0 = a[min(j, last)]
        s1 = a[min(j + 1, last)]
        out[i] = int(s0 + (s1 - s0) * frac)
    return out.tobytes()


class SystemSynthesizer:
    """
    Renders text with one system voice.  One instance per job; not thread-safe
    except for :meth:`cancel`, which may be called from any thread.
    """

    def __init__(self, short_name: str) -> None:
        if not is_system_voice(short_name):
            raise ValueError(f"Not a system voice: {short_name}")
        self._voice_id = short_name[len(SYSTEM_PREFIX):]
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._cancelled = False
        self._tmpdir = Path(tempfile.mkdtemp(prefix="setuptts_sys_"))
        self._count = 0
        self.sample_rate = SAMPLE_RATE

    # -- public -------------------------------------------------------- #

    def synthesize(self, text: str, rate: str) -> bytes:
        """PCM (16-bit mono, SAMPLE_RATE) for *text*."""
        if self._cancelled:
            raise SystemSpeechCancelled("cancelled")
        if not text.strip():
            return b""
        self._count += 1
        text_path = self._tmpdir / f"in_{self._count}.txt"
        wav_path = self._tmpdir / f"out_{self._count}.wav"
        text_path.write_text(text, encoding="utf-8")
        try:
            if sys.platform == "win32":
                self._speak_windows(text_path, wav_path, rate)
            elif sys.platform == "darwin":
                self._speak_macos(text_path, wav_path, rate)
            else:
                raise SystemSpeechError("System voices are not supported on this platform")
            if self._cancelled:
                raise SystemSpeechCancelled("cancelled")
            if not wav_path.exists():
                raise SystemSpeechError("The system voice produced no audio")
            # A header-only WAV is a real result: punctuation-only text, or
            # characters the voice cannot read.  The caller decides.
            return _read_wav(wav_path)
        finally:
            for p in (text_path, wav_path):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass

    def cancel(self) -> None:
        self._cancelled = True
        self._kill()

    def close(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.stdin.close()   # helper exits at end of input
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                pass
        self._kill()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def __enter__(self) -> "SystemSynthesizer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- Windows -------------------------------------------------------- #

    def _ensure_helper(self) -> subprocess.Popen:
        with self._lock:
            if self._cancelled:
                raise SystemSpeechCancelled("cancelled")
            proc = self._proc
            if proc is not None and proc.poll() is None:
                return proc
            proc = subprocess.Popen(
                _ps_args(_WIN_SERVE_SCRIPT),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, creationflags=_no_window(),
            )
            self._proc = proc
        line = self._readline(proc, _LIST_TIMEOUT_S)
        if line != "READY":
            self._kill()
            raise SystemSpeechError(f"System speech helper failed to start: {line!r}")
        return proc

    def _readline(self, proc: subprocess.Popen, timeout: float) -> str:
        result: list[bytes] = []
        reader = threading.Thread(target=lambda: result.append(proc.stdout.readline()),
                                  daemon=True)
        reader.start()
        reader.join(timeout)
        if reader.is_alive():
            self._kill()
            reader.join(2)
            if self._cancelled:
                raise SystemSpeechCancelled("cancelled")
            raise SystemSpeechError("The system voice stopped responding")
        if self._cancelled:
            raise SystemSpeechCancelled("cancelled")
        if not result or not result[0]:
            raise SystemSpeechError("The system speech helper exited unexpectedly")
        return result[0].decode("utf-8", "replace").strip()

    def _speak_windows(self, text_path: Path, wav_path: Path, rate: str) -> None:
        last: Exception | None = None
        for _attempt in range(2):   # one restart if the helper died
            proc = self._ensure_helper()
            request = json.dumps({
                "voice": self._voice_id, "rate": sapi_rate(rate),
                "wav": str(wav_path), "text": str(text_path),
            }) + "\n"
            try:
                proc.stdin.write(request.encode("ascii"))
                proc.stdin.flush()
            except OSError as exc:
                if self._cancelled:
                    raise SystemSpeechCancelled("cancelled") from exc
                last = exc
                self._kill()
                continue
            reply = self._readline(proc, _SPEAK_TIMEOUT_S)
            if reply == "OK":
                return
            raise SystemSpeechError(
                "The system voice could not speak this text"
                + (f": {reply[4:]}" if reply.startswith("ERR ") else f" ({reply!r})")
            )
        raise SystemSpeechError(f"The system speech helper could not be started: {last}")

    # -- macOS ---------------------------------------------------------- #

    def _speak_macos(self, text_path: Path, wav_path: Path, rate: str) -> None:
        with self._lock:
            if self._cancelled:
                raise SystemSpeechCancelled("cancelled")
            proc = subprocess.Popen(
                ["say", "-v", self._voice_id, "-r", str(say_wpm(rate)),
                 "--file-format=WAVE", f"--data-format=LEI16@{SAMPLE_RATE}",
                 "-o", str(wav_path), "-f", str(text_path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._proc = proc
        try:
            _, err = proc.communicate(timeout=_SPEAK_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            self._kill()
            raise SystemSpeechError("The system voice stopped responding") from exc
        if self._cancelled:
            raise SystemSpeechCancelled("cancelled")
        if proc.returncode != 0:
            raise SystemSpeechError(
                f"`say` failed (exit {proc.returncode}): "
                f"{(err or b'').decode('utf-8', 'replace').strip()[:300]}"
            )

    def _kill(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                pass
