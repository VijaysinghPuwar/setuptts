"""
Offline neural voices (Piper, https://github.com/rhasspy/piper).

Piper voices are ONNX models that run on the CPU — roughly 10x faster than
realtime on an ordinary laptop — with no account, key or internet once the
model is on disk.  One English voice ships inside the app; the rest of the
public catalog (rhasspy/piper-voices on Hugging Face, ~170 voices in ~50
languages) can be downloaded from the Get Voices dialog.

ShortNames look like ``piper:en_US-lessac-medium``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import ssl
import sys
import threading
import time
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.utils.paths import AppPaths, resource_path

logger = logging.getLogger(__name__)

PIPER_PREFIX = "piper:"
#: The voice that ships with the app.
BUNDLED_VOICE = "en_US-lessac-medium"

HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
CATALOG_URL = f"{HF_BASE}/voices.json?download=true"
_CATALOG_TTL_S = 7 * 24 * 3600

# Languages whose Piper models need extra phonemizer downloads or packages
# that are not bundled; listing them would only offer voices that fail.
_UNSUPPORTED_FAMILIES = {"zh", "ja", "he", "th"}

# The catalog has no gender field; these are the well-known single-speaker
# voices whose gender is documented in their model cards.
_GENDERS = {
    "amy": "Female", "lessac": "Female", "kristin": "Female", "kathleen": "Female",
    "hfc_female": "Female", "ljspeech": "Female", "alba": "Female", "cori": "Female",
    "jenny_dioco": "Female", "southern_english_female": "Female", "siwis": "Female",
    "eva_k": "Female", "kerstin": "Female", "ramona": "Female", "paola": "Female",
    "lisa": "Female", "berta": "Female", "gosia": "Female", "nathalie": "Female",
    "upmc": "Female", "anna": "Female", "natia": "Female", "irina": "Female",
    "ryan": "Male", "joe": "Male", "john": "Male", "bryce": "Male", "norman": "Male",
    "hfc_male": "Male", "danny": "Male", "kusal": "Male", "alan": "Male",
    "northern_english_male": "Male", "thorsten": "Male", "karlsson": "Male",
    "riccardo": "Male", "davefx": "Male", "carlfm": "Male", "faber": "Male",
    "gilles": "Male", "tom": "Male", "denis": "Male", "dmitri": "Male",
    "ruslan": "Male", "pavoque": "Male", "kareem": "Male", "fahrettin": "Male",
    "mihai": "Male", "artur": "Male", "darkman": "Male", "lukas": "Male",
    "jirka": "Male", "talesyntese": "Male", "rdh": "Male", "harri": "Male",
}


def is_piper_voice(short_name: str | None) -> bool:
    return bool(short_name) and short_name.startswith(PIPER_PREFIX)


def piper_available() -> bool:
    try:
        import piper  # noqa: F401
        import onnxruntime  # noqa: F401
    except Exception:  # noqa: BLE001 - missing or broken native runtime
        logger.warning("Piper runtime unavailable", exc_info=True)
        return False
    return True


def bundled_dir() -> Path:
    return resource_path("app/assets/piper")


def user_dir() -> Path:
    d = AppPaths().data_dir / "piper_voices"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key_parts(key: str) -> tuple[str, str, str]:
    """'en_US-lessac-medium' → ('en_US', 'lessac', 'medium')."""
    lang, _, rest = key.partition("-")
    name, _, quality = rest.rpartition("-")
    return lang, name or rest, quality


def display_name(key: str) -> str:
    """'Lessac', 'Amy (low)', 'Northern English Male'…"""
    _, name, quality = _key_parts(key)
    label = name.replace("_", " ").title()
    return label if quality in ("medium", "") else f"{label} ({quality.replace('_', '-')})"


def gender_for(key: str) -> str:
    return _GENDERS.get(_key_parts(key)[1], "")


def _locale_for(lang_code: str) -> str:
    return lang_code.replace("_", "-")


# ══════════════════════════════════════════════════════════════════════ #
#  Installed voices                                                      #
# ══════════════════════════════════════════════════════════════════════ #

def _model_paths() -> dict[str, Path]:
    """key → .onnx path; user downloads win over the bundled copy."""
    found: dict[str, Path] = {}
    for root in (bundled_dir(), user_dir()):
        try:
            for onnx in sorted(root.glob("*.onnx")):
                if onnx.with_name(onnx.name + ".json").exists():
                    found[onnx.stem] = onnx
        except OSError:
            continue
    return found


def model_path(key: str) -> Path | None:
    return _model_paths().get(key)


def is_bundled(key: str) -> bool:
    p = model_path(key)
    return p is not None and p.parent == bundled_dir()


def installed_keys() -> set[str]:
    return set(_model_paths())


def list_piper_voices() -> list[dict]:
    """Installed Piper voices as edge-style dicts (``Source='piper'``)."""
    if not piper_available():
        return []
    voices = []
    for key, onnx in _model_paths().items():
        lang_code = _key_parts(key)[0]
        try:
            cfg = json.loads(onnx.with_name(onnx.name + ".json").read_text(encoding="utf-8"))
            lang_code = (cfg.get("language") or {}).get("code") or lang_code
        except (OSError, ValueError):
            logger.warning("Unreadable Piper config for %s", key)
            continue
        if lang_code.split("_")[0] in _UNSUPPORTED_FAMILIES:
            continue
        voices.append({
            "ShortName": PIPER_PREFIX + key,
            "FriendlyName": f"{display_name(key)} (Offline neural) - {_locale_for(lang_code)}",
            "Locale": _locale_for(lang_code),
            "Gender": gender_for(key),
            "Source": "piper",
        })
    return voices


# ══════════════════════════════════════════════════════════════════════ #
#  Catalog + download                                                    #
# ══════════════════════════════════════════════════════════════════════ #

@dataclass(frozen=True)
class CatalogVoice:
    key: str
    locale: str
    language: str          # "English (United States)"
    quality: str
    size_bytes: int
    files: dict            # relative path → {"size_bytes", "md5_digest"}

    @property
    def display_name(self) -> str:
        return display_name(self.key)

    @property
    def gender(self) -> str:
        return gender_for(self.key)


def _ssl_context() -> ssl.SSLContext:
    from app.services.tts_service import build_ssl_context
    return build_ssl_context()


def _open(url: str, timeout: float = 30):
    req = urllib.request.Request(url, headers={"User-Agent": "SetupTTS"})
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())


def fetch_catalog(*, force_refresh: bool = False) -> list[CatalogVoice]:
    """The downloadable voices, from cache when fresh (raises when offline with no cache)."""
    cache = AppPaths().cache_dir / "piper_voices.json"
    raw: dict | None = None
    fresh = cache.exists() and (time.time() - cache.stat().st_mtime) < _CATALOG_TTL_S
    if fresh and not force_refresh:
        try:
            raw = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
    if raw is None:
        try:
            with _open(CATALOG_URL) as resp:
                body = resp.read()
            raw = json.loads(body.decode("utf-8"))
            tmp = cache.with_name(cache.name + ".tmp")
            tmp.write_bytes(body)
            tmp.replace(cache)
        except Exception:
            if cache.exists():
                logger.warning("Voice catalog refresh failed; using cached copy", exc_info=True)
                raw = json.loads(cache.read_text(encoding="utf-8"))
            else:
                raise
    return _parse_catalog(raw)


def _parse_catalog(raw: dict) -> list[CatalogVoice]:
    out = []
    for key, entry in (raw or {}).items():
        try:
            lang = entry["language"]
            if lang["family"] in _UNSUPPORTED_FAMILIES:
                continue
            files = {p: f for p, f in entry["files"].items() if p.endswith((".onnx", ".onnx.json"))}
            if len(files) != 2:
                continue
            out.append(CatalogVoice(
                key=key,
                locale=_locale_for(lang["code"]),
                language=f'{lang["name_english"]} ({lang["country_english"]})',
                quality=entry.get("quality", ""),
                size_bytes=sum(int(f.get("size_bytes", 0)) for f in files.values()),
                files=files,
            ))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda v: (v.language, v.display_name))
    return out


class DownloadCancelled(Exception):
    pass


def download_voice(
    voice: CatalogVoice,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """
    Download *voice* into the user voice folder, verifying size and MD5.
    Files land under temporary names and are renamed only once both are
    verified, so an interrupted download never leaves a broken voice.
    """
    dest = user_dir()
    total = voice.size_bytes
    done = 0
    staged: list[tuple[Path, Path]] = []
    try:
        # Config first (tiny) so the model rename is the commit point.
        for rel, meta in sorted(voice.files.items(), key=lambda kv: kv[0].endswith(".onnx")):
            final = dest / Path(rel).name
            part = final.with_name(final.name + ".part")
            md5 = hashlib.md5()
            size = 0
            with _open(f"{HF_BASE}/{rel}?download=true", timeout=60) as resp, open(part, "wb") as fh:
                while True:
                    if cancelled and cancelled():
                        raise DownloadCancelled()
                    block = resp.read(256 * 1024)
                    if not block:
                        break
                    fh.write(block)
                    md5.update(block)
                    size += len(block)
                    done += len(block)
                    if progress:
                        progress(done, total)
            want_size = int(meta.get("size_bytes") or 0)
            want_md5 = meta.get("md5_digest")
            if want_size and size != want_size:
                raise OSError(f"Download of {final.name} is incomplete ({size} of {want_size} bytes)")
            if want_md5 and md5.hexdigest() != want_md5:
                raise OSError(f"Download of {final.name} is corrupted (checksum mismatch)")
            staged.append((part, final))
        for part, final in staged:
            os.replace(part, final)
        staged.clear()
        return dest / f"{voice.key}.onnx"
    finally:
        for part, _ in staged:
            part.unlink(missing_ok=True)
        for rel in voice.files:
            (dest / (Path(rel).name + ".part")).unlink(missing_ok=True)


def remove_voice(key: str) -> bool:
    """Delete a downloaded voice.  The bundled voice cannot be removed."""
    # Drop the cached model first: Windows won't delete a file that is
    # still open.
    with _LOADED_LOCK:
        for path in [p for p in _LOADED if Path(p).stem == key]:
            _LOADED.pop(path, None)
    removed = False
    for p in (user_dir() / f"{key}.onnx", user_dir() / f"{key}.onnx.json"):
        try:
            p.unlink()
            removed = True
        except FileNotFoundError:
            pass
    return removed


# ══════════════════════════════════════════════════════════════════════ #
#  Synthesis                                                             #
# ══════════════════════════════════════════════════════════════════════ #

# Loaded voices, most recently used last.  Each holds ~400 MB once it has run
# (onnxruntime keeps its memory arena, which is ~35% faster than without), so
# only the last one used stays cached.  A job keeps its own reference, so
# evicting a voice never disturbs one in progress — it is freed when that
# job ends.
_LOADED: "OrderedDict[str, object]" = OrderedDict()
_LOADED_LOCK = threading.Lock()
_MAX_LOADED = 1
_PHONEMIZE_LOCK = threading.Lock()


def _total_ram_bytes() -> int:
    try:
        if os.name == "nt":
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = _MemStatus()
            status.dwLength = ctypes.sizeof(_MemStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return int(status.ullTotalPhys)
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except Exception:  # noqa: BLE001
        return 0


def _plan_parallel_runs() -> tuple[int, int]:
    """
    (runs at once per voice, CPU threads per run).

    One run of a voice model stops getting faster past ~6 threads, so two
    sentences in flight with the cores split between them is ~30% faster
    (Ryan high on an M-series Mac: 133 → 171 chars/s; Lessac medium 810 →
    1015) for ~220 MB more memory.  Small machines keep one run.
    """
    cores = os.cpu_count() or 1
    if cores < 4 or _total_ram_bytes() < 8 * 1024 ** 3:
        return 1, 0                     # 0 = onnxruntime's default
    # Windows/Linux count hyper-threads; macOS (no SMT) counts real cores.
    physical = cores if sys.platform == "darwin" else max(2, cores // 2)
    return 2, max(2, physical // 2)


_PARALLEL_RUNS, _THREADS_PER_RUN = _plan_parallel_runs()


_ESPEAK_DIR: Path | None = None
_IS_WINDOWS = os.name == "nt"


def _espeak_data_dir() -> Path:
    """
    espeak-ng's data folder, at a path espeak-ng can open.

    On Windows espeak-ng opens files with the ANSI APIs, so a folder under a
    profile such as C:\\Users\\José (the portable build unpacks into %TEMP%)
    can't be read and every offline voice fails.  Use the 8.3 short name, or
    failing that a one-time copy in an ASCII-named folder.
    """
    global _ESPEAK_DIR
    if _ESPEAK_DIR is not None:
        return _ESPEAK_DIR
    from piper.phonemize_espeak import ESPEAK_DATA_DIR

    data = Path(ESPEAK_DATA_DIR)
    if not _IS_WINDOWS or str(data).isascii():
        _ESPEAK_DIR = data
        return data
    short = _windows_short_path(data)
    if short is not None and str(short).isascii():
        _ESPEAK_DIR = short
        return short
    for root in (os.environ.get("ProgramData"), os.environ.get("PUBLIC"), "C:\\SetupTTS"):
        if not root or not root.isascii():
            continue
        target = Path(root) / "SetupTTS" / "espeak-ng-data"
        try:
            marker = target / ".complete"
            if not marker.exists():
                import shutil

                shutil.rmtree(target, ignore_errors=True)
                shutil.copytree(data, target)
                marker.write_text("1", encoding="ascii")
            logger.info("Using an ASCII copy of espeak-ng data at %s", target)
            _ESPEAK_DIR = target
            return target
        except OSError:
            logger.warning("Could not copy espeak-ng data to %s", target, exc_info=True)
    logger.warning("espeak-ng data path is not ASCII and no fallback worked: %s", data)
    _ESPEAK_DIR = data
    return data


def _windows_short_path(path: Path) -> Path | None:
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, len(buf))
        return Path(buf.value) if 0 < n < len(buf) else None
    except Exception:  # noqa: BLE001
        return None


def _configure_session(voice, model: str) -> None:
    """Re-open the model with threads sized for parallel runs."""
    try:
        import onnxruntime

        opts = onnxruntime.SessionOptions()
        opts.intra_op_num_threads = _THREADS_PER_RUN
        opts.inter_op_num_threads = 1
        voice.session = onnxruntime.InferenceSession(
            model, sess_options=opts, providers=["CPUExecutionProvider"],
        )
    except Exception:  # noqa: BLE001 - the default session still works
        logger.warning("Could not tune the offline voice session", exc_info=True)


def _load(path: Path):
    key = str(path)
    with _LOADED_LOCK:
        voice = _LOADED.get(key)
        if voice is None:
            from piper import PiperVoice
            voice = PiperVoice.load(
                key, download_dir=str(user_dir()), espeak_data_dir=_espeak_data_dir(),
            )
            if _THREADS_PER_RUN:
                _configure_session(voice, key)
            # At most _PARALLEL_RUNS runs of one model at a time, across every
            # job and preview using it (see PiperSynthesizer.synthesize).
            voice.setuptts_run_lock = threading.BoundedSemaphore(_PARALLEL_RUNS)
            _LOADED[key] = voice
            while len(_LOADED) > _MAX_LOADED:
                _LOADED.popitem(last=False)
        else:
            _LOADED.move_to_end(key)
        return voice


class PiperError(RuntimeError):
    pass


class PiperCancelled(PiperError):
    pass


_RATE_RE = re.compile(r"\s*([+-]?\d+)\s*%\s*")


#: Longest phoneme run sent to the model at once (~40-60 words).  The model's
#: memory grows steeply with input length and onnxruntime keeps the peak, so
#: one long unpunctuated run (a list of numbers, a table pasted as text) took
#: the app past 3 GB.  Ordinary sentences are far below this.
_MAX_PHONEMES = 250


def _cap_phonemes(phonemes: list[str], limit: int = _MAX_PHONEMES) -> list[list[str]]:
    """Split an over-long sentence at word gaps (or hard, for one giant word)."""
    if len(phonemes) <= limit:
        return [phonemes]
    pieces: list[list[str]] = []
    start = 0
    while len(phonemes) - start > limit:
        cut = start + limit
        space = max((i for i in range(start + limit // 2, cut) if phonemes[i] == " "), default=None)
        if space is not None:
            cut = space
        pieces.append(phonemes[start:cut])
        start = cut + 1 if space is not None else cut
    if start < len(phonemes):
        pieces.append(phonemes[start:])
    return [p for p in pieces if p]


class PiperSynthesizer:
    """Same interface as SystemSynthesizer: synthesize / cancel / close."""

    def __init__(self, short_name: str) -> None:
        if not is_piper_voice(short_name):
            raise ValueError(f"Not a Piper voice: {short_name}")
        key = short_name[len(PIPER_PREFIX):]
        path = model_path(key)
        if path is None:
            raise PiperError(
                f"The offline voice “{display_name(key)}” is not installed. "
                "Download it again from Get Voices, or pick another voice."
            )
        try:
            self._voice = _load(path)
        except Exception as exc:  # noqa: BLE001
            raise PiperError(f"Could not load the offline voice “{display_name(key)}”: {exc}") from exc
        self.sample_rate = int(self._voice.config.sample_rate)
        self._run_lock = getattr(self._voice, "setuptts_run_lock", None) or threading.Lock()
        self._cancelled = False
        self._pool = None
        if _PARALLEL_RUNS > 1:
            from concurrent.futures import ThreadPoolExecutor

            self._pool = ThreadPoolExecutor(_PARALLEL_RUNS, thread_name_prefix="piper")

    def synthesize(self, text: str, rate: str) -> bytes:
        import numpy as np
        from piper import SynthesisConfig

        if self._cancelled:
            raise PiperCancelled("cancelled")
        m = _RATE_RE.fullmatch(rate or "")
        mult = max(0.25, 1.0 + (int(m.group(1)) if m else 0) / 100.0)
        cfg = SynthesisConfig(length_scale=self._voice.config.length_scale / mult)
        # espeak-ng keeps global state and is not thread-safe; two jobs (or a
        # job and a preview) phonemizing at once can crash it.  Inference
        # itself is thread-safe and runs unlocked.
        with _PHONEMIZE_LOCK:
            # (phonemes, ends_sentence): a split sentence gets no pause
            # between its pieces, so it still sounds like one sentence.
            pieces = [(piece, i == len(split) - 1)
                      for sentence in self._voice.phonemize(text)
                      for split in [_cap_phonemes(sentence)]
                      for i, piece in enumerate(split)]
        # A short pause between sentences: Piper returns each sentence
        # trimmed, and joined back-to-back they run together.
        gap = bytes(2 * int(self.sample_rate * 0.12))

        def render(phonemes: list[str]) -> bytes:
            if self._cancelled:
                raise PiperCancelled("cancelled")
            ids = self._voice.phonemes_to_ids(phonemes)
            # Runs per model are capped across all jobs and previews:
            # onnxruntime keeps the peak working memory of every concurrent
            # run (unbounded, two jobs and a preview reached 1.2 GB).
            with self._run_lock:
                if self._cancelled:
                    raise PiperCancelled("cancelled")
                audio = self._voice.phoneme_ids_to_audio(ids, cfg)
            if isinstance(audio, tuple):
                audio = audio[0]
            audio = np.asarray(audio, dtype=np.float32).reshape(-1)
            peak = float(np.max(np.abs(audio))) if audio.size else 0.0
            if peak < 1e-8:
                return b""
            return np.clip(audio / peak * 32767.0, -32767, 32767).astype("<i2").tobytes()

        work = [(p, ends) for p, ends in pieces if p]
        if self._pool is not None and len(work) > 1:
            # Sentences render two at a time; map() keeps them in order.
            rendered = list(self._pool.map(render, [p for p, _ in work]))
        else:
            rendered = [render(p) for p, _ in work]
        parts = []
        for audio, (_, ends_sentence) in zip(rendered, work):
            if not audio:
                continue
            parts.append(audio)
            if ends_sentence:
                parts.append(gap)
        return b"".join(parts)

    def cancel(self) -> None:
        self._cancelled = True

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None

    def __enter__(self) -> "PiperSynthesizer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
