"""
NVIDIA graphics-card acceleration for offline (Piper) voices — Windows only.

Piper voices run on the processor everywhere.  On a Windows PC with a recent
NVIDIA card the same model can run through onnxruntime's CUDA provider
instead.  The CUDA build of onnxruntime and NVIDIA's libraries are ~1.3 GB,
far too big for every installer, so they are an optional download ("GPU
support") unpacked into the app-data folder:

    <data>/gpu_runtime/<PACK_ID>/onnxruntime/...      onnxruntime-gpu package
    <data>/gpu_runtime/<PACK_ID>/nvidia/.../*.dll     CUDA, cuBLAS, cuFFT, cuDNN…

The bundled onnxruntime is the CPU build.  When GPU support is switched on,
:func:`startup` (called before anything imports onnxruntime) points the
``onnxruntime`` import at the downloaded GPU build, which still contains the
CPU provider, and puts the NVIDIA DLL folders on the DLL search path.

Nothing here runs on macOS or Linux: every entry point returns at once when
``SUPPORTED`` is false, so the Mac keeps exactly the CPU path it always had.

Before GPU support is offered as "on", ``SetupTTS --gpu-check`` (see
app/gpu_check.py) runs in a separate process — a driver crash there can't
take the app down — and times the same voice on the processor and on the
card.  The card is used only when it is clearly faster.  If the card fails
during a job, synthesis falls back to the processor and the job carries on
(see PiperSynthesizer).
"""

from __future__ import annotations

import hashlib
import importlib.abc
import importlib.machinery
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.utils.file_ops import replace_with_retry
from app.utils.paths import AppPaths

logger = logging.getLogger(__name__)

SUPPORTED = sys.platform == "win32"

#: onnxruntime-gpu 1.30.0 is built against CUDA 13.0 and cuDNN 9.  The Python
#: half of the package must match the bundled CPU onnxruntime (1.30.0), and
#: its native extension the app's Python (3.12).
PACK_ID = "ort-1.30.0-cuda13"
PYTHON_TAG = (3, 12)
#: CUDA 13 needs an R580 or newer driver …
MIN_DRIVER = (580, 0)
#: … and dropped Maxwell, Pascal and Volta (GTX 900/10-series, Titan V).
MIN_COMPUTE = (7, 5)
#: The card must beat the processor by this much to be worth using.
MIN_SPEEDUP = 1.25
#: Downloads (~1.33 GB) plus the unpacked files (~2.9 GB), with headroom.
FREE_SPACE_NEEDED = 5 * 1024 ** 3


@dataclass(frozen=True)
class _Wheel:
    filename: str
    url: str
    sha256: str
    size: int

    @property
    def is_onnxruntime(self) -> bool:
        return self.filename.startswith("onnxruntime_gpu-")


# Pinned exactly, verified by SHA-256.  CUDA 13 wheels keep their DLLs under
# nvidia/cu13/bin/x86_64, cuDNN under nvidia/cudnn/bin.
WHEELS: tuple[_Wheel, ...] = (
    _Wheel("onnxruntime_gpu-1.30.0-cp312-cp312-win_amd64.whl",
           "https://files.pythonhosted.org/packages/19/ef/e15db03ea1fce9bc60349add2604df8c39abac10d28d4eef27b621845139/onnxruntime_gpu-1.30.0-cp312-cp312-win_amd64.whl",
           "882da66f936e1f560b155118c5953dc45268cd92893abb8d31a940f4d1effb36", 160478594),
    _Wheel("nvidia_cuda_runtime-13.4.92-py3-none-win_amd64.whl",
           "https://files.pythonhosted.org/packages/86/00/d5436004268f049214193659ebc36550b5ef3925c3d13b4cc980e13be6f5/nvidia_cuda_runtime-13.4.92-py3-none-win_amd64.whl",
           "08dca5e4aba480c2fd5b55075c0fa71b84ef9dcf0521f2d58baa14a803a7311c", 2778543),
    _Wheel("nvidia_cuda_nvrtc-13.4.92-py3-none-win_amd64.whl",
           "https://files.pythonhosted.org/packages/64/5b/aa91896f64444eff1dd7f189f4db05046c7f0ba08c0f531283c1c1aac581/nvidia_cuda_nvrtc-13.4.92-py3-none-win_amd64.whl",
           "6af7ac5372920f6a7a560d0699348560fe44cb52c1d722d6afd3119f8af948c4", 46972283),
    _Wheel("nvidia_cublas-13.8.0.4-py3-none-win_amd64.whl",
           "https://files.pythonhosted.org/packages/a3/df/f1246959833e2c437db8be3e5b477f66b87f8817821ed40de6c7561c9a36/nvidia_cublas-13.8.0.4-py3-none-win_amd64.whl",
           "8c5494423bb8a46822cb6b0cb95d7fa4be2d7b96a31155dff083839ec8297910", 423266897),
    _Wheel("nvidia_cufft-12.4.0.43-py3-none-win_amd64.whl",
           "https://files.pythonhosted.org/packages/7b/cc/be7fe31058127336a66c88414a2ecc6beecf680baf85994a43eaf292b202/nvidia_cufft-12.4.0.43-py3-none-win_amd64.whl",
           "4ff7075f2d0b5f69291f70938d37a86ec632cbe5747184c74ba1f50f17accacc", 160953139),
    _Wheel("nvidia_nvjitlink-13.4.92-py3-none-win_amd64.whl",
           "https://files.pythonhosted.org/packages/72/7c/c44bd7277afafed9de262a23baec4dc8dcf7ecbb1b9ecb228821442d1e9b/nvidia_nvjitlink-13.4.92-py3-none-win_amd64.whl",
           "b286f3a4f227a9363efdec263c7b91788cef1478d2b8a5fa8bab7f3e82ff82fd", 39053950),
    _Wheel("nvidia_curand-10.4.4.72-py3-none-win_amd64.whl",
           "https://files.pythonhosted.org/packages/e2/d4/f59ab342b82ff05ff27d674acd470ba625d788bf71338f7da6c89669d6b9/nvidia_curand-10.4.4.72-py3-none-win_amd64.whl",
           "e0bce83e083ef25976ee74f59e8f067c15149a74538f1be6c2462e286e7c9c68", 56731059),
    _Wheel("nvidia_cudnn_cu13-9.27.0.42-py3-none-win_amd64.whl",
           "https://files.pythonhosted.org/packages/87/6a/e55ff0ac26a5c6e2b21f41c9d04ad096b4ed6da593fba7e25845c61b0532/nvidia_cudnn_cu13-9.27.0.42-py3-none-win_amd64.whl",
           "7d96f634adafd55c72231eb0500ca77ab109ec8ebff7b33000b76e081bc4558e", 436469905),
)
DOWNLOAD_BYTES = sum(w.size for w in WHEELS)

#: Loaded up front, by full path, in dependency order (cuBLAS needs cuBLASLt
#: first).  The CUDA part is what onnxruntime's own preload_dlls() loads for
#: CUDA 13 plus curand/nvrtc/nvJitLink; the cuDNN part is its full list, so
#: no cuDNN engine library is left to the DLL search order of a frozen app.
_PRELOAD_DLLS = (
    "cudart64_13.dll", "cublasLt64_13.dll", "cublas64_13.dll", "cufft64_12.dll",
    "curand64_10.dll", "nvrtc64_130_0.dll", "nvJitLink_130_0.dll",
    "cudnn_graph64_9.dll", "cudnn_engines_runtime_compiled64_9.dll",
    "cudnn_engines_precompiled64_9.dll", "cudnn_heuristic64_9.dll", "cudnn_ops64_9.dll",
    "cudnn_adv64_9.dll", "cudnn64_9.dll", "cudnn_engines_tensor_ir64_9.dll",
)


# ══════════════════════════════════════════════════════════════════════ #
#  Detecting the card                                                    #
# ══════════════════════════════════════════════════════════════════════ #

@dataclass(frozen=True)
class GpuInfo:
    name: str
    driver: str                         # "581.42"
    memory_mb: int
    compute: tuple[int, int] | None     # (8, 6); None when the driver can't say

    @property
    def driver_version(self) -> tuple[int, int]:
        nums = [int(n) for n in re.findall(r"\d+", self.driver)[:2]]
        return (nums + [0, 0])[0], (nums + [0, 0])[1]


def parse_nvidia_smi(text: str) -> list[GpuInfo]:
    """Rows of ``name, driver_version, memory.total, compute_cap`` (csv, no units)."""
    gpus = []
    for line in (text or "").splitlines():
        cols = [c.strip() for c in line.split(",")]
        if len(cols) < 3 or not cols[0]:
            continue
        try:
            memory = int(float(cols[2]))
        except ValueError:
            memory = 0
        compute = None
        if len(cols) > 3:
            m = re.fullmatch(r"(\d+)\.(\d+)", cols[3])
            if m:
                compute = (int(m.group(1)), int(m.group(2)))
        gpus.append(GpuInfo(cols[0], cols[1], memory, compute))
    return gpus


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _nvidia_smi() -> str | None:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    candidates = [
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe",
    ]
    return next((str(p) for p in candidates if p.is_file()), None)


def detect_gpus(timeout: float = 10.0) -> list[GpuInfo]:
    """NVIDIA cards the driver reports (empty off Windows or with no NVIDIA driver)."""
    if not SUPPORTED:
        return []
    exe = _nvidia_smi()
    if exe is None:
        return []
    # compute_cap is unknown to older drivers; ask again without it.
    for fields in ("name,driver_version,memory.total,compute_cap",
                   "name,driver_version,memory.total"):
        try:
            out = subprocess.run(
                [exe, f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL, creationflags=_no_window(),
            )
        except (OSError, subprocess.SubprocessError):
            logger.warning("nvidia-smi could not be run", exc_info=True)
            return []
        if out.returncode == 0:
            gpus = parse_nvidia_smi(out.stdout)
            if gpus:
                return gpus
    return []


def requirement_problem(gpu: GpuInfo | None) -> str | None:
    """Why this card can't be used, in plain words — or None when it can."""
    if gpu is None:
        return "No NVIDIA graphics card was found on this PC."
    if sys.version_info[:2] != PYTHON_TAG:
        return "This build of SetupTTS can't use GPU support."
    if gpu.driver_version < MIN_DRIVER:
        return (f"The NVIDIA driver is too old (version {gpu.driver}). Update it to "
                f"version {MIN_DRIVER[0]} or newer from nvidia.com or the NVIDIA app, "
                "then try again.")
    if gpu.compute is not None and gpu.compute < MIN_COMPUTE:
        return (f"The {gpu.name} is too old for GPU support, which needs a GeForce "
                "RTX 20-series / GTX 16-series card or newer.")
    return None


# ══════════════════════════════════════════════════════════════════════ #
#  The downloaded runtime                                                #
# ══════════════════════════════════════════════════════════════════════ #

def runtime_root() -> Path:
    return AppPaths().data_dir / "gpu_runtime"


def pack_dir() -> Path:
    return runtime_root() / PACK_ID


def _marker() -> Path:
    return pack_dir() / ".complete"


#: Files without which the pack can't work; one quarantined by antivirus or
#: deleted by hand must not count as installed.
_KEY_FILES = (
    "onnxruntime/__init__.py",
    "onnxruntime/capi/onnxruntime_pybind11_state.pyd",
    "onnxruntime/capi/onnxruntime.dll",
    "onnxruntime/capi/onnxruntime_providers_shared.dll",
    "onnxruntime/capi/onnxruntime_providers_cuda.dll",
)


def pack_installed() -> bool:
    """GPU support is fully in place: this release's wheels, key files present."""
    # A removal waiting for the next start already counts as removed.
    if (runtime_root() / ".remove").exists():
        return False
    try:
        manifest = json.loads(_marker().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(manifest, dict) or \
            manifest.get("wheels") != [w.filename for w in WHEELS]:
        return False
    return all((pack_dir() / f).is_file() for f in _KEY_FILES)


def pack_size_on_disk() -> int:
    total = 0
    for root, _dirs, files in os.walk(runtime_root()):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


class DownloadCancelled(Exception):
    pass


def _open(url: str, *, start: int = 0, timeout: float = 60):
    import urllib.request

    from app.services.tts_service import build_ssl_context

    headers = {"User-Agent": "SetupTTS"}
    if start:
        headers["Range"] = f"bytes={start}-"
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout, context=build_ssl_context())


def _sha256_of(path: Path) -> "hashlib._Hash":
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h


def _fetch(wheel: _Wheel, dest: Path, report: Callable[[int], None],
           cancelled: Callable[[], bool]) -> None:
    """Download *wheel* to *dest*, resuming a partial ``.part``; verify SHA-256."""
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if have > wheel.size:
        part.unlink()
        have = 0
    h = _sha256_of(part) if have else hashlib.sha256()
    report(have)
    if have < wheel.size:
        with _open(wheel.url, start=have) as resp:
            if have and getattr(resp, "status", 200) != 206:
                # The server ignored the range: start over.
                have = 0
                h = hashlib.sha256()
                report(0)
            with open(part, "ab" if have else "wb") as fh:
                while True:
                    if cancelled():
                        raise DownloadCancelled()
                    block = resp.read(1024 * 1024)
                    if not block:
                        break
                    fh.write(block)
                    h.update(block)
                    have += len(block)
                    report(have)
    if have != wheel.size:
        raise OSError(f"Download of {wheel.filename} is incomplete "
                      f"({have:,} of {wheel.size:,} bytes). Try again to resume.")
    if h.hexdigest() != wheel.sha256:
        part.unlink(missing_ok=True)
        raise OSError(f"Download of {wheel.filename} is corrupted (checksum mismatch). "
                      "Try again.")
    replace_with_retry(part, dest, attempts=8)


def _wanted(wheel: _Wheel, name: str) -> bool:
    if name.endswith("/"):
        return False
    if wheel.is_onnxruntime:
        return name.startswith("onnxruntime/")
    return name.startswith("nvidia/") and name.lower().endswith(".dll")


def _extract(wheel: _Wheel, archive: Path, into: Path, cancelled: Callable[[], bool]) -> None:
    root = into.resolve()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if cancelled():
                raise DownloadCancelled()
            if not _wanted(wheel, info.filename):
                continue
            target = (into / info.filename).resolve()
            if root not in target.parents:
                raise OSError(f"Unsafe path in {wheel.filename}: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)


def download_pack(
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """
    Download, verify and unpack GPU support.  Resumable: finished wheels are
    remembered and a partial download continues where it stopped.  The
    folder only becomes live (renamed into place) once everything is in.
    """
    if not SUPPORTED:
        raise OSError("GPU support is only available on Windows.")
    if _STATE.activated:
        # Its DLLs are loaded in this session; Windows won't replace them.
        raise OSError("GPU support is in use. Restart SetupTTS, then download it again.")
    cancelled = cancelled or (lambda: False)
    root = runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / ".remove").unlink(missing_ok=True)
    staging = root / f"{PACK_ID}.staging"
    downloads = root / "downloads"
    staging.mkdir(parents=True, exist_ok=True)
    downloads.mkdir(parents=True, exist_ok=True)

    def done_flag(w: _Wheel) -> Path:
        return staging / f".done-{w.filename}"

    finished = sum(w.size for w in WHEELS if done_flag(w).exists())
    free = shutil.disk_usage(root).free
    if free < FREE_SPACE_NEEDED - finished:
        err = OSError(28, f"GPU support needs about {FREE_SPACE_NEEDED / 1024 ** 3:.0f} GB "
                          f"of free disk space; this drive has {free / 1024 ** 3:.1f} GB.")
        raise err

    base = finished
    for wheel in WHEELS:
        if done_flag(wheel).exists():
            continue
        archive = downloads / wheel.filename
        if not archive.exists():
            _fetch(wheel, archive,
                   lambda n, b=base: progress and progress(b + n, DOWNLOAD_BYTES),
                   cancelled)
        _extract(wheel, archive, staging, cancelled)
        done_flag(wheel).write_text("1", encoding="ascii")
        archive.unlink(missing_ok=True)
        base += wheel.size
        if progress:
            progress(base, DOWNLOAD_BYTES)

    (staging / ".complete").write_text(
        json.dumps({"id": PACK_ID, "wheels": [w.filename for w in WHEELS],
                    "installed": time.strftime("%Y-%m-%d %H:%M:%S")}),
        encoding="utf-8",
    )
    final = pack_dir()
    if final.exists():
        shutil.rmtree(final, ignore_errors=True)
        if final.exists():
            raise OSError(f"Could not replace the old GPU support folder ({final}). "
                          "Restart SetupTTS and try again.")
    # Defender scans ~2.9 GB of fresh DLLs; a folder rename while any file
    # in it is open fails, so ride that out.  Until it succeeds the per-wheel
    # flags stay, and a retry only redoes this step.
    replace_with_retry(staging, final, attempts=30)
    for flag in final.glob(".done-*"):
        flag.unlink(missing_ok=True)
    shutil.rmtree(downloads, ignore_errors=True)
    logger.info("GPU support installed at %s", final)
    return final


def remove_pack() -> bool:
    """
    Delete GPU support.  Returns False when its DLLs are loaded in this
    session (Windows won't delete them); it is then removed at next start.
    """
    root = runtime_root()
    if _STATE.activated:
        root.mkdir(parents=True, exist_ok=True)
        (root / ".remove").write_text("1", encoding="ascii")
        return False
    shutil.rmtree(root, ignore_errors=True)
    if root.exists():
        root.mkdir(parents=True, exist_ok=True)
        (root / ".remove").write_text("1", encoding="ascii")
        return False
    return True


def _apply_pending_removal() -> None:
    """Finish a removal scheduled last session; the marker goes last, so a
    file still locked now is retried at the next start instead of orphaned."""
    root = runtime_root()
    marker = root / ".remove"
    if not marker.exists():
        return
    for child in root.iterdir():
        if child == marker:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink(missing_ok=True)
            except OSError:
                pass
    if [c for c in root.iterdir() if c != marker]:
        logger.warning("GPU support could not be fully removed yet; will retry at next start")
        return
    shutil.rmtree(root, ignore_errors=True)
    logger.info("Removed GPU support (scheduled at last session)")


# ══════════════════════════════════════════════════════════════════════ #
#  Speed check results                                                   #
# ══════════════════════════════════════════════════════════════════════ #

def _check_path() -> Path:
    return runtime_root() / "check.json"


def load_check() -> dict | None:
    try:
        data = json.loads(_check_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("pack") == PACK_ID else None


def save_check(result: dict) -> None:
    result = dict(result, pack=PACK_ID)
    _check_path().parent.mkdir(parents=True, exist_ok=True)
    _check_path().write_text(json.dumps(result, indent=2), encoding="utf-8")


def check_recommends(result: dict | None) -> bool:
    """The card worked, produced sound, and was clearly faster than the processor."""
    if not result or not result.get("ok"):
        return False
    try:
        return float(result.get("speedup", 0)) >= MIN_SPEEDUP
    except (TypeError, ValueError):
        return False


def check_command(out_path: Path) -> list[str]:
    """Command line that runs the speed check in a separate process."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--gpu-check", str(out_path)]
    main_py = Path(__file__).resolve().parents[2] / "main.py"
    return [sys.executable, str(main_py), "--gpu-check", str(out_path)]


# ══════════════════════════════════════════════════════════════════════ #
#  Switching onnxruntime over                                            #
# ══════════════════════════════════════════════════════════════════════ #

class _RedirectFinder(importlib.abc.MetaPathFinder):
    """Load *package* (and its submodules) from *directory*, ahead of the bundle."""

    def __init__(self, package: str, directory: Path) -> None:
        self.package = package
        self.directory = directory

    def find_spec(self, fullname, path=None, target=None):  # noqa: D401
        if fullname == self.package:
            return importlib.machinery.PathFinder.find_spec(fullname, [str(self.directory)])
        if fullname.startswith(self.package + "."):
            parent = sys.modules.get(fullname.rpartition(".")[0])
            search = getattr(parent, "__path__", None)
            if search is None:
                return None
            return importlib.machinery.PathFinder.find_spec(fullname, list(search))
        return None


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.activated = False          # GPU onnxruntime wired in this session
        self.failed: str | None = None  # the card stopped working; CPU from now on
        self.device = ""
        self.reason = ""
        self.dll_dirs: list[Path] = []
        self.cuda_loaded = False
        self.finder: _RedirectFinder | None = None


_STATE = _State()


def _dll_dirs(base: Path) -> list[Path]:
    return sorted({p.parent for p in (base / "nvidia").rglob("*.dll")})


def activate(*, directory: Path | None = None, package: str = "onnxruntime") -> bool:
    """
    Make ``import onnxruntime`` load the downloaded GPU build.  Must run
    before anything imports onnxruntime; returns False (and changes nothing)
    when that already happened or the files are missing.

    Only puts the NVIDIA folders on the DLL search path: the ~1.7 GB of CUDA
    and cuDNN libraries load on first use (:func:`ensure_cuda_loaded`), not
    at every launch.  onnxruntime itself doesn't need them to import.
    """
    base = directory or pack_dir()
    with _STATE.lock:
        if _STATE.activated:
            return True
        if package in sys.modules:
            logger.warning("GPU support not activated: %s was already loaded", package)
            return False
        if not (base / package / "__init__.py").is_file():
            return False
        if sys.platform == "win32":
            dirs = _dll_dirs(base)
            for d in dirs:
                try:
                    os.add_dll_directory(str(d))
                except OSError:
                    logger.warning("Could not add DLL folder %s", d, exc_info=True)
            # cuDNN opens its engine libraries with plain LoadLibrary, which
            # searches PATH rather than add_dll_directory's list.
            os.environ["PATH"] = os.pathsep.join(
                [str(d) for d in dirs] + [os.environ.get("PATH", "")])
            # Number cards the way nvidia-smi lists them, so the card named in
            # Settings and checked for requirements is device 0.
            os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
            _STATE.dll_dirs = dirs
        finder = _RedirectFinder(package, base)
        sys.meta_path.insert(0, finder)
        _STATE.finder = finder
        _STATE.activated = True
        _STATE.failed = None
        logger.info("GPU support activated from %s", base)
        return True


def _deactivate(package: str = "onnxruntime") -> None:
    """Undo :func:`activate` after the GPU build failed to import."""
    with _STATE.lock:
        if _STATE.finder is not None and _STATE.finder in sys.meta_path:
            sys.meta_path.remove(_STATE.finder)
        _STATE.finder = None
        for name in [m for m in sys.modules if m == package or m.startswith(package + ".")]:
            del sys.modules[name]
        _STATE.activated = False


def ensure_cuda_loaded() -> None:
    """
    Load the CUDA and cuDNN libraries, by full path, before the first model
    is put on the card.  Once per session; called from a worker thread.
    """
    if sys.platform != "win32":
        return
    with _STATE.lock:
        if _STATE.cuda_loaded:
            return
        _STATE.cuda_loaded = True
        dirs = list(_STATE.dll_dirs)
    import ctypes

    for name in _PRELOAD_DLLS:
        hit = next((d / name for d in dirs if (d / name).is_file()), None)
        if hit is None:
            logger.warning("GPU support: %s not found", name)
            continue
        try:
            ctypes.WinDLL(str(hit))
        except OSError:
            logger.warning("GPU support: could not load %s", hit, exc_info=True)


def cuda_provider_loads() -> tuple[bool, str]:
    """
    Whether onnxruntime's CUDA provider DLL and everything it links against
    load (needs no card) — tells "a DLL is missing" apart from "no device".
    """
    if sys.platform != "win32":
        return False, "not Windows"
    ensure_cuda_loaded()
    import ctypes

    dll = pack_dir() / "onnxruntime" / "capi" / "onnxruntime_providers_cuda.dll"
    try:
        ctypes.WinDLL(str(dll))
    except OSError as exc:
        return False, str(exc)
    return True, ""


def decide(mode: str, installed: bool, check: dict | None) -> tuple[bool, str]:
    """
    (use the card?, why) for a gpu_mode setting.

    "auto" uses the card only when the speed check on this PC found it
    clearly faster than the processor (a weaker card loses to a strong
    processor and stays unused); "gpu" uses it whenever GPU support is
    installed, with the processor as the fallback if it fails; "cpu" never.
    """
    if mode == "cpu":
        return False, "the processor was chosen in Settings"
    if not installed:
        return False, "GPU support is not downloaded"
    if mode == "gpu":
        return True, "the graphics card was chosen in Settings"
    if not check:
        return False, "the speed check has not run yet"
    if not check.get("ok"):
        return False, "the graphics card failed its speed check"
    speedup = _speedup(check)
    if not check_recommends(check):
        return False, f"the processor was faster on this PC (card {speedup:.2f}x)"
    return True, f"the graphics card was {speedup:.1f}x faster"


def _speedup(check: dict) -> float:
    try:
        value = float(check.get("speedup", 0))
    except (TypeError, ValueError):
        return 0.0
    return value if value == value else 0.0      # NaN → 0


def startup(mode: str) -> bool:
    """
    Called once at launch, before onnxruntime is imported, with the gpu_mode
    setting.  Switches the GPU build in when :func:`decide` says so.  Never
    raises; a no-op off Windows.
    """
    if not SUPPORTED:
        return False
    try:
        _apply_pending_removal()
        check = load_check()
        installed = pack_installed()
        use, why = decide(mode, installed, check)
        if use and sys.version_info[:2] != PYTHON_TAG:
            use, why = False, "this build of SetupTTS can't use the downloaded GPU support"
        _STATE.reason = why
        logger.info("Offline voices: %s (%s)", "graphics card" if use else "processor", why)
        if not use or not activate():
            return False
        # Import it now: a pack that is damaged (a file quarantined by
        # antivirus) or doesn't fit this build must not leave every offline
        # voice unable to load — undo, and the bundled CPU build is used.
        try:
            import onnxruntime  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            _deactivate()
            _STATE.reason = f"GPU support failed to load ({exc})"
            mark_failed(_STATE.reason)
            logger.warning("GPU support failed to load; using the processor", exc_info=True)
            return False
        _STATE.device = str((check or {}).get("gpu_name") or "NVIDIA GPU")
        return True
    except Exception:  # noqa: BLE001 - never stop the app from starting
        logger.warning("GPU support could not be started; using the processor", exc_info=True)
        return False


def reason() -> str:
    """Why this session runs offline voices where it does (for Settings)."""
    return _STATE.reason


def active() -> bool:
    """Offline voices should be loaded on the card right now."""
    return SUPPORTED and _STATE.activated and _STATE.failed is None


def activated() -> bool:
    return _STATE.activated


def device_name() -> str:
    return _STATE.device


def failure() -> str | None:
    return _STATE.failed


def mark_failed(reason: str) -> None:
    """The card failed: every voice loaded from now on uses the processor."""
    with _STATE.lock:
        if _STATE.failed is None:
            _STATE.failed = reason or "unknown error"
            logger.warning("GPU stopped working (%s); offline voices continue on the processor",
                           _STATE.failed)


def cuda_provider_options() -> dict:
    return {
        "device_id": "0",
        # Every sentence is a new input length.  EXHAUSTIVE (the default)
        # benchmarks every convolution algorithm for each new shape, which
        # would make a book far slower on the card than on the processor.
        "cudnn_conv_algo_search": "HEURISTIC",
        # Grow GPU memory only as needed instead of doubling.
        "arena_extend_strategy": "kSameAsRequested",
    }
