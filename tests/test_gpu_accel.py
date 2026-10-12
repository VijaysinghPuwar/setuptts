"""
Windows-only NVIDIA GPU support for offline (Piper) voices.

Nothing here touches the network, a real graphics card or the real app-data
folder: downloads are served from memory, nvidia-smi and onnxruntime are
faked, and AppPaths points at a temporary folder.  The most important group
is the Mac-safety one: with SUPPORTED false nothing may change.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import shutil
import subprocess
import sys
import types
import zipfile
from collections import OrderedDict
from pathlib import Path

import pytest

from app import gpu_check
from app.services import gpu_accel, piper_tts


# ------------------------------------------------------------------ #
# Fixtures                                                            #
# ------------------------------------------------------------------ #

@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Temporary app-data folder; GPU state, meta_path and modules restored."""
    data = tmp_path / "appdata"
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(data))
    fields = ("activated", "failed", "device", "reason", "dll_dirs", "cuda_loaded", "finder")
    st = gpu_accel._STATE
    state = {f: getattr(st, f) for f in fields}
    st.activated, st.failed, st.device, st.reason = False, None, "", ""
    st.dll_dirs, st.cuda_loaded, st.finder = [], False, None
    meta_path = list(sys.meta_path)
    # startup()'s failure path drops onnxruntime* from sys.modules; put the
    # real (CPU) modules back so later tests don't re-import native code.
    ort_modules = {m: mod for m, mod in sys.modules.items()
                   if m == "onnxruntime" or m.startswith("onnxruntime.")}
    yield data
    sys.meta_path[:] = meta_path
    for name in [m for m in sys.modules if m.split(".")[0].startswith("fakeortpkg")]:
        del sys.modules[name]
    sys.modules.update(ort_modules)
    for f, v in state.items():
        setattr(st, f, v)


@pytest.fixture
def python_ok(monkeypatch):
    """The requirement check accepts whatever Python runs the tests (3.12 or 3.14)."""
    monkeypatch.setattr(gpu_accel, "PYTHON_TAG", tuple(sys.version_info[:2]))


def good_gpu(**kw) -> gpu_accel.GpuInfo:
    base = dict(name="NVIDIA GeForce RTX 4070", driver="581.42", memory_mb=12282, compute=(8, 9))
    base.update(kw)
    return gpu_accel.GpuInfo(**base)


# ------------------------------------------------------------------ #
# 1. nvidia-smi parsing                                               #
# ------------------------------------------------------------------ #

def test_parse_multi_gpu_output():
    text = ("NVIDIA GeForce RTX 4090, 581.42, 24564, 8.9\n"
            "NVIDIA GeForce RTX 3060, 581.42, 12288, 8.6\n")
    gpus = gpu_accel.parse_nvidia_smi(text)
    assert gpus == [
        gpu_accel.GpuInfo("NVIDIA GeForce RTX 4090", "581.42", 24564, (8, 9)),
        gpu_accel.GpuInfo("NVIDIA GeForce RTX 3060", "581.42", 12288, (8, 6)),
    ]


def test_parse_without_compute_column_and_na_compute():
    gpus = gpu_accel.parse_nvidia_smi("Quadro P2000, 552.22, 5120\nRTX A2000, 581.15, 6138, [N/A]")
    assert gpus[0].compute is None and gpus[0].memory_mb == 5120
    assert gpus[1].compute is None and gpus[1].name == "RTX A2000"


def test_parse_skips_garbage_and_handles_bad_memory():
    text = ("\n"
            "No devices were found\n"
            ", 581.42, 100, 8.6\n"            # no name
            "RTX 4060, 581.42, [N/A], 8.9\n"  # unreadable memory
            "RTX 4060 Ti, 581.42, 8188.0, 8.9\r\n"
            "a,b\n")
    gpus = gpu_accel.parse_nvidia_smi(text)
    assert [g.name for g in gpus] == ["RTX 4060", "RTX 4060 Ti"]
    assert gpus[0].memory_mb == 0
    assert gpus[1].memory_mb == 8188
    assert gpu_accel.parse_nvidia_smi("") == []
    assert gpu_accel.parse_nvidia_smi(None) == []


@pytest.mark.parametrize("driver,expected", [
    ("581.42", (581, 42)),
    ("566.36", (566, 36)),
    ("580", (580, 0)),
    ("[N/A]", (0, 0)),
    ("", (0, 0)),
    ("581.42.01", (581, 42)),
])
def test_driver_version(driver, expected):
    assert good_gpu(driver=driver).driver_version == expected


# ------------------------------------------------------------------ #
# 2. Requirements                                                     #
# ------------------------------------------------------------------ #

def test_requirement_no_gpu():
    assert "No NVIDIA" in gpu_accel.requirement_problem(None)


def test_requirement_wrong_python(monkeypatch):
    monkeypatch.setattr(gpu_accel, "PYTHON_TAG", (2, 7))
    assert "can't use" in gpu_accel.requirement_problem(good_gpu())


def test_requirement_old_driver(python_ok):
    problem = gpu_accel.requirement_problem(good_gpu(driver="566.36"))
    assert "too old" in problem and "566.36" in problem and "580" in problem


def test_requirement_old_compute(python_ok):
    problem = gpu_accel.requirement_problem(good_gpu(name="GeForce GTX 1080", compute=(6, 1)))
    assert "GTX 1080" in problem and "RTX 20" in problem


def test_requirement_volta_rejected_turing_accepted(python_ok):
    assert gpu_accel.requirement_problem(good_gpu(compute=(7, 0))) is not None
    assert gpu_accel.requirement_problem(good_gpu(compute=(7, 5))) is None


def test_requirement_unknown_compute_allowed(python_ok):
    assert gpu_accel.requirement_problem(good_gpu(compute=None)) is None


def test_requirement_ok(python_ok):
    assert gpu_accel.requirement_problem(good_gpu()) is None
    assert gpu_accel.requirement_problem(good_gpu(driver="580.00")) is None


# ------------------------------------------------------------------ #
# 3. detect_gpus                                                      #
# ------------------------------------------------------------------ #

def test_detect_gpus_unsupported_never_runs_anything(monkeypatch):
    monkeypatch.setattr(gpu_accel, "SUPPORTED", False)

    def boom(*a, **k):
        raise AssertionError("must not run")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(gpu_accel, "_nvidia_smi", boom)
    assert gpu_accel.detect_gpus() == []


def test_detect_gpus_retries_without_compute_cap(monkeypatch):
    monkeypatch.setattr(gpu_accel, "SUPPORTED", True)
    monkeypatch.setattr(gpu_accel, "_nvidia_smi", lambda: "nvidia-smi")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "compute_cap" in cmd[1]:
            return types.SimpleNamespace(returncode=2, stdout="", stderr="Field not valid")
        return types.SimpleNamespace(returncode=0, stdout="RTX 2060, 581.10, 6144\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    gpus = gpu_accel.detect_gpus()
    assert len(calls) == 2
    assert calls[1][1] == "--query-gpu=name,driver_version,memory.total"
    assert gpus == [gpu_accel.GpuInfo("RTX 2060", "581.10", 6144, None)]


def test_detect_gpus_first_query_succeeds(monkeypatch):
    monkeypatch.setattr(gpu_accel, "SUPPORTED", True)
    monkeypatch.setattr(gpu_accel, "_nvidia_smi", lambda: "nvidia-smi")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(
        returncode=0, stdout="RTX 4070, 581.42, 12282, 8.9\n"))
    assert gpu_accel.detect_gpus()[0].compute == (8, 9)
    assert len(calls) == 1


def test_detect_gpus_no_nvidia_smi(monkeypatch):
    monkeypatch.setattr(gpu_accel, "SUPPORTED", True)
    monkeypatch.setattr(gpu_accel, "_nvidia_smi", lambda: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    assert gpu_accel.detect_gpus() == []


def test_detect_gpus_nvidia_smi_fails_to_start(monkeypatch):
    monkeypatch.setattr(gpu_accel, "SUPPORTED", True)
    monkeypatch.setattr(gpu_accel, "_nvidia_smi", lambda: "nvidia-smi")

    def fake_run(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert gpu_accel.detect_gpus() == []


def test_detect_gpus_both_queries_fail(monkeypatch):
    monkeypatch.setattr(gpu_accel, "SUPPORTED", True)
    monkeypatch.setattr(gpu_accel, "_nvidia_smi", lambda: "nvidia-smi")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(
        returncode=9, stdout="NVIDIA-SMI has failed because it couldn't communicate with the driver"))
    assert gpu_accel.detect_gpus() == []


# ------------------------------------------------------------------ #
# 4. Mac safety                                                       #
# ------------------------------------------------------------------ #

def test_startup_unsupported_changes_nothing(monkeypatch, _isolated):
    monkeypatch.setattr(gpu_accel, "SUPPORTED", False)
    # Even a fully installed pack with a passing check and a pending removal
    # flag must be ignored.
    monkeypatch.setattr(gpu_accel, "activate", lambda **k: pytest.fail("must not activate"))
    monkeypatch.setattr(gpu_accel, "_apply_pending_removal", lambda: pytest.fail("must not run"))
    monkeypatch.setattr(gpu_accel, "pack_installed", lambda: pytest.fail("must not look"))
    meta_before = list(sys.meta_path)
    modules_before = set(sys.modules)
    for mode in ("auto", "gpu", "cpu"):
        assert gpu_accel.startup(mode) is False
    assert sys.meta_path == meta_before
    assert set(sys.modules) == modules_before
    assert not gpu_accel.activated()
    assert not gpu_accel.active()
    assert gpu_accel.device_name() == ""
    assert gpu_accel.reason() == ""


def test_active_false_when_unsupported_even_if_state_says_activated(monkeypatch):
    monkeypatch.setattr(gpu_accel, "SUPPORTED", False)
    gpu_accel._STATE.activated = True
    assert gpu_accel.active() is False


def test_download_refused_when_unsupported(monkeypatch, _isolated):
    monkeypatch.setattr(gpu_accel, "SUPPORTED", False)
    monkeypatch.setattr(gpu_accel, "_open", lambda *a, **k: pytest.fail("no network"))
    with pytest.raises(OSError, match="only available on Windows"):
        gpu_accel.download_pack()
    assert not (_isolated / "gpu_runtime").exists()


def test_wants_gpu_false_on_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(gpu_accel, "active", lambda: pytest.fail("must not be asked"))
    gpu_accel._STATE.activated = True
    assert piper_tts._wants_gpu() is False


def test_wants_gpu_follows_active_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(gpu_accel, "active", lambda: True)
    assert piper_tts._wants_gpu() is True
    monkeypatch.setattr(gpu_accel, "active", lambda: False)
    assert piper_tts._wants_gpu() is False


@pytest.fixture
def fresh_cache(monkeypatch):
    monkeypatch.setattr(piper_tts, "_LOADED", OrderedDict())
    monkeypatch.setattr(piper_tts, "_MAX_LOADED", 2)
    monkeypatch.setattr(piper_tts, "_PARALLEL_RUNS", 1)
    return piper_tts._LOADED


def test_load_cpu_path_calls_open_voice_without_gpu_kwarg(monkeypatch, tmp_path, fresh_cache):
    monkeypatch.setattr(piper_tts, "_wants_gpu", lambda: False)
    calls = []
    monkeypatch.setattr(piper_tts, "_open_voice",
                        lambda *a, **k: calls.append((a, k)) or types.SimpleNamespace())
    path = tmp_path / "en_US-x-medium.onnx"
    voice = piper_tts._load(path)
    assert calls == [((str(path),), {})]
    assert voice.setuptts_on_gpu is False
    assert list(fresh_cache) == [str(path)]


def test_open_voice_cpu_uses_only_cpu_provider(monkeypatch, tmp_path):
    import onnxruntime
    import piper
    import piper.config

    sessions = []
    disabled = []

    class FakeSession:
        def __init__(self, model, sess_options=None, providers=None):
            sessions.append((model, providers))
            self._providers = [p if isinstance(p, str) else p[0] for p in providers]

        def get_providers(self):
            return list(self._providers)

        def disable_fallback(self):
            disabled.append(self)

    class FakeVoice:
        def __init__(self, **kw):
            self.kw = kw

    monkeypatch.setattr(onnxruntime, "InferenceSession", FakeSession)
    monkeypatch.setattr(piper, "PiperVoice", FakeVoice)
    monkeypatch.setattr(piper.config.PiperConfig, "from_dict",
                        staticmethod(lambda d: types.SimpleNamespace(raw=d)))
    cuda_loads = []
    monkeypatch.setattr(gpu_accel, "ensure_cuda_loaded",
                        lambda: cuda_loads.append(len(sessions)))
    model = tmp_path / "m.onnx"
    (tmp_path / "m.onnx.json").write_text("{}", encoding="utf-8")

    voice = piper_tts._open_voice(str(model))
    assert sessions == [(str(model), ["CPUExecutionProvider"])]
    assert cuda_loads == []      # the CPU path never touches CUDA
    assert isinstance(voice, FakeVoice)
    assert disabled == []        # the CPU session keeps onnxruntime's defaults

    # The GPU path asks for CUDA first with the tuned options …
    sessions.clear()
    piper_tts._open_voice(str(model), gpu=True)
    assert cuda_loads == [0]     # CUDA libraries loaded before the session
    providers = sessions[0][1]
    assert providers[0][0] == "CUDAExecutionProvider"
    assert providers[0][1]["cudnn_conv_algo_search"] == "HEURISTIC"
    assert providers[1] == "CPUExecutionProvider"
    # A failing CUDA run must raise (so the app falls back and says so),
    # not be retried silently on the processor by onnxruntime.
    assert len(disabled) == 1

    # … and refuses a session where onnxruntime quietly dropped CUDA.
    class NoCuda(FakeSession):
        def get_providers(self):
            return ["CPUExecutionProvider"]

    monkeypatch.setattr(onnxruntime, "InferenceSession", NoCuda)
    with pytest.raises(piper_tts.GpuUnavailable, match="did not start"):
        piper_tts._open_voice(str(model), gpu=True)


# ------------------------------------------------------------------ #
# 5. Redirecting the import                                           #
# ------------------------------------------------------------------ #

def make_fake_package(root: Path, marker: str, name: str = "fakeortpkg") -> Path:
    pkg = root / name
    (pkg / "sub").mkdir(parents=True)
    (pkg / "__init__.py").write_text(f"MARKER = {marker!r}\n", encoding="utf-8")
    (pkg / "sub" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "sub" / "mod.py").write_text(f"VALUE = {marker!r} + '-mod'\n", encoding="utf-8")
    return pkg


def test_activate_redirects_package_and_submodules(monkeypatch, tmp_path):
    import importlib

    gpu_dir = tmp_path / "gpu"
    make_fake_package(gpu_dir, "gpu-build")
    # A same-named "bundled" package on sys.path must lose to the GPU build.
    bundled = tmp_path / "bundled"
    make_fake_package(bundled, "cpu-build")
    monkeypatch.syspath_prepend(str(bundled))
    importlib.invalidate_caches()

    assert gpu_accel.activate(directory=gpu_dir, package="fakeortpkg") is True
    assert gpu_accel.activated()
    assert isinstance(sys.meta_path[0], gpu_accel._RedirectFinder)

    pkg = importlib.import_module("fakeortpkg")
    mod = importlib.import_module("fakeortpkg.sub.mod")
    assert pkg.MARKER == "gpu-build"
    assert mod.VALUE == "gpu-build-mod"
    assert Path(pkg.__file__).resolve().is_relative_to(gpu_dir.resolve())
    assert Path(mod.__file__).resolve().is_relative_to(gpu_dir.resolve())
    assert Path(sys.modules["fakeortpkg.sub"].__file__).resolve().is_relative_to(gpu_dir.resolve())

    # A second call is a no-op that reports success.
    n = len(sys.meta_path)
    assert gpu_accel.activate(directory=gpu_dir, package="fakeortpkg") is True
    assert len(sys.meta_path) == n


def test_redirect_finder_ignores_other_modules(tmp_path):
    finder = gpu_accel._RedirectFinder("fakeortpkg", tmp_path)
    assert finder.find_spec("json") is None
    assert finder.find_spec("fakeortpkgother") is None
    # A submodule whose parent isn't imported yet is left to the normal finders.
    assert finder.find_spec("fakeortpkg.sub") is None


def test_activate_refuses_when_already_imported(monkeypatch, tmp_path):
    make_fake_package(tmp_path, "gpu-build")
    monkeypatch.setitem(sys.modules, "fakeortpkg", types.ModuleType("fakeortpkg"))
    before = list(sys.meta_path)
    assert gpu_accel.activate(directory=tmp_path, package="fakeortpkg") is False
    assert sys.meta_path == before
    assert not gpu_accel.activated()


def test_activate_refuses_when_files_missing(tmp_path):
    before = list(sys.meta_path)
    assert gpu_accel.activate(directory=tmp_path / "nope", package="fakeortpkg") is False
    (tmp_path / "empty" / "fakeortpkg").mkdir(parents=True)  # no __init__.py
    assert gpu_accel.activate(directory=tmp_path / "empty", package="fakeortpkg") is False
    assert sys.meta_path == before
    assert not gpu_accel.activated()


def test_activate_clears_previous_failure(tmp_path):
    make_fake_package(tmp_path, "gpu-build")
    gpu_accel._STATE.failed = "old"
    assert gpu_accel.activate(directory=tmp_path, package="fakeortpkg")
    assert gpu_accel.failure() is None


def test_mark_failed_keeps_first_reason():
    gpu_accel.mark_failed("first")
    gpu_accel.mark_failed("second")
    assert gpu_accel.failure() == "first"
    gpu_accel._STATE.failed = None
    gpu_accel.mark_failed("")
    assert gpu_accel.failure() == "unknown error"


# ------------------------------------------------------------------ #
# 6. Downloading                                                      #
# ------------------------------------------------------------------ #

def zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


ORT_ZIP = zip_bytes({
    "onnxruntime/__init__.py": b"__version__ = '1.30.0'\n",
    "onnxruntime/capi/x.dll": b"ort-dll" * 50,
    "onnxruntime/capi/onnxruntime_pybind11_state.pyd": b"pyd",
    "onnxruntime/capi/onnxruntime.dll": b"dll",
    "onnxruntime/capi/onnxruntime_providers_shared.dll": b"dll",
    "onnxruntime/capi/onnxruntime_providers_cuda.dll": b"dll",
    "onnxruntime_gpu-1.30.0.dist-info/METADATA": b"meta",
})
NV_ZIP = zip_bytes({
    "nvidia/cu13/bin/x86_64/a.dll": b"cuda-dll" * 50,
    "nvidia/cu13/include/h.h": b"/* header */",
    "nvidia/cu13/lib/x64/a.lib": b"lib",
    "nvidia_cuda_runtime-13.4.92.dist-info/RECORD": b"rec",
})


def wheel_for(filename: str, data: bytes, *, sha: str | None = None) -> gpu_accel._Wheel:
    return gpu_accel._Wheel(filename, f"https://example.invalid/{filename}",
                            sha or hashlib.sha256(data).hexdigest(), len(data))


class FakeServer:
    """Serves wheels from memory in small chunks; honours Range unless told not to."""

    def __init__(self, files: dict[str, bytes], *, chunk: int = 64, honour_range: bool = True):
        self.files = files
        self.chunk = chunk
        self.honour_range = honour_range
        self.requests: list[tuple[str, int]] = []

    def open(self, url, *, start=0, timeout=60):
        name = url.rsplit("/", 1)[1]
        self.requests.append((name, start))
        data = self.files[name]
        status = 200
        if start and self.honour_range:
            data, status = data[start:], 206
        chunk = self.chunk

        class Resp:
            def __init__(self):
                self.status = status
                self._buf = io.BytesIO(data)

            def read(self, n=-1):
                return self._buf.read(min(n, chunk) if n and n > 0 else chunk)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Resp()


@pytest.fixture
def pack_env(monkeypatch, _isolated):
    """Two tiny wheels, SUPPORTED on, small space requirement, in-memory server."""
    ort = wheel_for("onnxruntime_gpu-1.30.0-cp312-cp312-win_amd64.whl", ORT_ZIP)
    nv = wheel_for("nvidia_cuda_runtime-13.4.92-py3-none-win_amd64.whl", NV_ZIP)
    monkeypatch.setattr(gpu_accel, "SUPPORTED", True)
    monkeypatch.setattr(gpu_accel, "WHEELS", (ort, nv))
    monkeypatch.setattr(gpu_accel, "DOWNLOAD_BYTES", ort.size + nv.size)
    monkeypatch.setattr(gpu_accel, "FREE_SPACE_NEEDED", 1024)
    server = FakeServer({ort.filename: ORT_ZIP, nv.filename: NV_ZIP})
    monkeypatch.setattr(gpu_accel, "_open", server.open)
    return types.SimpleNamespace(ort=ort, nv=nv, server=server, root=_isolated / "gpu_runtime")


def test_download_installs_pack(pack_env):
    progress = []
    final = gpu_accel.download_pack(progress=lambda a, b: progress.append((a, b)))
    assert final == gpu_accel.pack_dir()
    assert gpu_accel.pack_installed()
    assert (final / ".complete").is_file()
    info = json.loads((final / ".complete").read_text(encoding="utf-8"))
    assert info["id"] == gpu_accel.PACK_ID
    assert (final / "onnxruntime" / "__init__.py").is_file()
    assert (final / "onnxruntime" / "capi" / "x.dll").read_bytes() == b"ort-dll" * 50
    assert (final / "nvidia" / "cu13" / "bin" / "x86_64" / "a.dll").is_file()
    # Headers, import libraries and wheel metadata are not unpacked.
    assert not (final / "nvidia" / "cu13" / "include").exists()
    assert not (final / "nvidia" / "cu13" / "lib").exists()
    assert not list(final.glob("*.dist-info"))
    assert not list(final.glob(".done-*"))
    # Staging and downloads are gone.
    assert not (pack_env.root / f"{gpu_accel.PACK_ID}.staging").exists()
    assert not (pack_env.root / "downloads").exists()
    # Progress climbs to the total.
    total = gpu_accel.DOWNLOAD_BYTES
    assert progress[-1] == (total, total)
    assert all(b == total for _, b in progress)
    assert [a for a, _ in progress] == sorted(a for a, _ in progress)
    assert gpu_accel._dll_dirs(final) == [final / "nvidia" / "cu13" / "bin" / "x86_64"]
    assert gpu_accel.pack_size_on_disk() > 0


def test_download_checksum_mismatch_leaves_no_pack(pack_env, monkeypatch):
    bad = wheel_for(pack_env.nv.filename, NV_ZIP, sha="0" * 64)
    monkeypatch.setattr(gpu_accel, "WHEELS", (pack_env.ort, bad))
    with pytest.raises(OSError, match="checksum"):
        gpu_accel.download_pack()
    assert not gpu_accel.pack_dir().exists()
    assert not gpu_accel.pack_installed()
    downloads = pack_env.root / "downloads"
    assert not (downloads / (bad.filename + ".part")).exists()
    assert not (downloads / bad.filename).exists()


def test_download_resumes_partial_part_with_range(pack_env):
    downloads = pack_env.root / "downloads"
    downloads.mkdir(parents=True)
    (downloads / (pack_env.ort.filename + ".part")).write_bytes(ORT_ZIP[:100])
    gpu_accel.download_pack()
    assert gpu_accel.pack_installed()
    assert (pack_env.ort.filename, 100) in pack_env.server.requests
    assert (pack_env.nv.filename, 0) in pack_env.server.requests


def test_download_restarts_when_server_ignores_range(pack_env):
    pack_env.server.honour_range = False
    downloads = pack_env.root / "downloads"
    downloads.mkdir(parents=True)
    # Garbage in the partial file: a correct restart must not keep it.
    (downloads / (pack_env.ort.filename + ".part")).write_bytes(b"X" * 100)
    progress = []
    gpu_accel.download_pack(progress=lambda a, b: progress.append(a))
    assert gpu_accel.pack_installed()
    assert (pack_env.ort.filename, 100) in pack_env.server.requests
    assert 0 in progress  # progress was reset when the server started over


def test_download_part_larger_than_wheel_restarts(pack_env):
    downloads = pack_env.root / "downloads"
    downloads.mkdir(parents=True)
    (downloads / (pack_env.ort.filename + ".part")).write_bytes(b"X" * (pack_env.ort.size + 10))
    gpu_accel.download_pack()
    assert gpu_accel.pack_installed()
    assert (pack_env.ort.filename, 0) in pack_env.server.requests


def test_download_cancel_then_resume(pack_env):
    reads = {"n": 0}

    def cancelled():
        reads["n"] += 1
        return reads["n"] > 3          # stop a few chunks into the first wheel

    with pytest.raises(gpu_accel.DownloadCancelled):
        gpu_accel.download_pack(cancelled=cancelled)
    part = pack_env.root / "downloads" / (pack_env.ort.filename + ".part")
    assert part.is_file() and 0 < part.stat().st_size < pack_env.ort.size
    have = part.stat().st_size
    assert not gpu_accel.pack_installed()

    gpu_accel.download_pack()
    assert gpu_accel.pack_installed()
    assert (pack_env.ort.filename, have) in pack_env.server.requests


def test_download_cancel_after_first_wheel_skips_it_on_resume(pack_env):
    state = {"cancel": False}
    original = gpu_accel._extract

    def extract(wheel, archive, into, cancelled):
        original(wheel, archive, into, cancelled)
        state["cancel"] = True     # cancel once the first wheel is unpacked

    import unittest.mock as mock
    with mock.patch.object(gpu_accel, "_extract", extract):
        with pytest.raises(gpu_accel.DownloadCancelled):
            gpu_accel.download_pack(cancelled=lambda: state["cancel"])
    pack_env.server.requests.clear()
    gpu_accel.download_pack()
    assert gpu_accel.pack_installed()
    assert [n for n, _ in pack_env.server.requests] == [pack_env.nv.filename]


@pytest.mark.parametrize("which,member", [
    ("ort", "onnxruntime/../../evil.py"),
    ("nv", "nvidia/../../evil.dll"),
    ("nv", "nvidia/cu13/../../../../evil.dll"),
])
def test_download_rejects_zip_slip(pack_env, monkeypatch, which, member):
    base = {"onnxruntime/__init__.py": b"x"} if which == "ort" else {"nvidia/ok.dll": b"x"}
    data = zip_bytes({**base, member: b"evil"})
    name = pack_env.ort.filename if which == "ort" else pack_env.nv.filename
    wheel = wheel_for(name, data)
    pack_env.server.files[name] = data
    wheels = (wheel, pack_env.nv) if which == "ort" else (pack_env.ort, wheel)
    monkeypatch.setattr(gpu_accel, "WHEELS", wheels)
    with pytest.raises(OSError, match="Unsafe path"):
        gpu_accel.download_pack()
    assert not gpu_accel.pack_installed()
    assert not list(pack_env.root.parent.rglob("evil*"))


def test_unwanted_traversal_member_is_simply_skipped(pack_env, monkeypatch):
    # "../evil.dll" isn't under nvidia/ so it is never even considered.
    data = zip_bytes({"nvidia/ok.dll": b"x", "../evil.dll": b"evil"})
    wheel = wheel_for(pack_env.nv.filename, data)
    pack_env.server.files[wheel.filename] = data
    monkeypatch.setattr(gpu_accel, "WHEELS", (pack_env.ort, wheel))
    gpu_accel.download_pack()
    assert gpu_accel.pack_installed()
    assert not list(pack_env.root.parent.rglob("evil*"))


def test_download_insufficient_disk_space(pack_env, monkeypatch):
    monkeypatch.setattr(gpu_accel, "FREE_SPACE_NEEDED", 10 * 1024 ** 3)
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(total=1, used=0, free=1024 ** 3))
    with pytest.raises(OSError) as info:
        gpu_accel.download_pack()
    assert info.value.errno == errno.ENOSPC == 28
    assert "free disk space" in str(info.value)
    assert pack_env.server.requests == []


def test_download_replaces_existing_pack(pack_env):
    old = gpu_accel.pack_dir()
    old.mkdir(parents=True)
    (old / "stale.txt").write_text("old")
    gpu_accel.download_pack()
    assert gpu_accel.pack_installed()
    assert not (old / "stale.txt").exists()


# ------------------------------------------------------------------ #
# 7. Removing                                                         #
# ------------------------------------------------------------------ #

def test_remove_pack_when_not_activated(pack_env):
    gpu_accel.download_pack()
    gpu_accel.save_check({"ok": True, "speedup": 3})
    assert gpu_accel.remove_pack() is True
    assert not pack_env.root.exists()
    assert not gpu_accel.pack_installed()
    assert gpu_accel.load_check() is None


def test_remove_pack_when_activated_is_scheduled(pack_env):
    gpu_accel.download_pack()
    gpu_accel._STATE.activated = True
    assert gpu_accel.remove_pack() is False
    assert (pack_env.root / ".remove").is_file()
    assert gpu_accel.pack_dir().is_dir()    # files stay this session …
    assert not gpu_accel.pack_installed()   # … but it already counts as removed

    # Next launch: the scheduled removal runs first.
    gpu_accel._STATE.activated = False
    gpu_accel._apply_pending_removal()
    assert not pack_env.root.exists()


def test_startup_applies_pending_removal(pack_env, monkeypatch):
    gpu_accel.download_pack()
    (pack_env.root / ".remove").write_text("1")
    gpu_accel.save_check({"ok": True, "speedup": 4})
    monkeypatch.setattr(gpu_accel, "activate", lambda **k: pytest.fail("pack was removed"))
    assert gpu_accel.startup("gpu") is False
    assert not pack_env.root.exists()


def test_apply_pending_removal_without_flag_keeps_pack(pack_env):
    gpu_accel.download_pack()
    gpu_accel._apply_pending_removal()
    assert gpu_accel.pack_installed()


def test_download_refused_while_activated(pack_env):
    gpu_accel._STATE.activated = True
    with pytest.raises(OSError, match="in use"):
        gpu_accel.download_pack()
    assert pack_env.server.requests == []


def test_download_fails_cleanly_when_old_pack_cannot_be_removed(pack_env, monkeypatch):
    old = gpu_accel.pack_dir()
    old.mkdir(parents=True)
    (old / "locked.dll").write_bytes(b"x")
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)   # Windows: DLL in use
    with pytest.raises(OSError, match="Could not replace the old GPU support folder"):
        gpu_accel.download_pack()
    assert (old / "locked.dll").is_file()
    assert not gpu_accel.pack_installed()
    # The unpacked staging copy is kept, so a retry needn't download again.
    monkeypatch.undo()
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(pack_env.root.parent))
    monkeypatch.setattr(gpu_accel, "SUPPORTED", True)
    monkeypatch.setattr(gpu_accel, "WHEELS", (pack_env.ort, pack_env.nv))
    monkeypatch.setattr(gpu_accel, "DOWNLOAD_BYTES", pack_env.ort.size + pack_env.nv.size)
    monkeypatch.setattr(gpu_accel, "FREE_SPACE_NEEDED", 1024)
    monkeypatch.setattr(gpu_accel, "_open", pack_env.server.open)
    gpu_accel.download_pack()
    assert gpu_accel.pack_installed()


def test_download_after_scheduled_removal_survives_restart(pack_env, monkeypatch):
    gpu_accel.download_pack()
    real_rmtree = shutil.rmtree
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)   # a file was locked
    assert gpu_accel.remove_pack() is False                       # scheduled
    assert (pack_env.root / ".remove").is_file()
    monkeypatch.setattr(shutil, "rmtree", real_rmtree)
    gpu_accel.download_pack()                    # user downloads it again
    gpu_accel._apply_pending_removal()           # next launch
    assert gpu_accel.pack_installed()


# ------------------------------------------------------------------ #
# 8. Speed-check results                                              #
# ------------------------------------------------------------------ #

def test_save_and_load_check_roundtrip():
    gpu_accel.save_check({"ok": True, "speedup": 2.5, "gpu_name": "RTX"})
    loaded = gpu_accel.load_check()
    assert loaded["pack"] == gpu_accel.PACK_ID
    assert loaded["speedup"] == 2.5


def test_load_check_ignores_other_pack_and_junk():
    path = gpu_accel._check_path()
    assert gpu_accel.load_check() is None              # missing
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ok": True, "speedup": 5, "pack": "ort-1.0-cuda11"}))
    assert gpu_accel.load_check() is None
    path.write_text("{not json")
    assert gpu_accel.load_check() is None
    path.write_text("[1, 2]")
    assert gpu_accel.load_check() is None


@pytest.mark.parametrize("result,expected", [
    (None, False),
    ({}, False),
    ({"ok": False, "speedup": 10}, False),
    ({"ok": True}, False),
    ({"ok": True, "speedup": gpu_accel.MIN_SPEEDUP}, True),
    ({"ok": True, "speedup": gpu_accel.MIN_SPEEDUP - 0.01}, False),
    ({"ok": True, "speedup": gpu_accel.MIN_SPEEDUP + 0.01}, True),
    ({"ok": True, "speedup": "3.0"}, True),
    ({"ok": True, "speedup": "fast"}, False),
    ({"ok": True, "speedup": None}, False),
    ({"ok": True, "speedup": [2]}, False),
    ({"ok": True, "speedup": float("nan")}, False),
])
def test_check_recommends(result, expected):
    assert gpu_accel.check_recommends(result) is expected


# ------------------------------------------------------------------ #
# 9. startup (SUPPORTED on)                                           #
# ------------------------------------------------------------------ #

@pytest.fixture
def win(monkeypatch, python_ok):
    monkeypatch.setattr(gpu_accel, "SUPPORTED", True)
    calls = []

    def fake_activate(**kw):
        # Like the real one, minus the import redirect: the (already
        # imported) CPU onnxruntime then satisfies startup's import.
        calls.append(kw)
        finder = gpu_accel._RedirectFinder("fakeortpkg-none", Path("/nonexistent"))
        sys.meta_path.insert(0, finder)
        gpu_accel._STATE.finder = finder
        gpu_accel._STATE.activated = True
        return True

    monkeypatch.setattr(gpu_accel, "activate", fake_activate)
    return calls


def install_fake_pack():
    d = gpu_accel.pack_dir()
    for f in gpu_accel._KEY_FILES:
        (d / f).parent.mkdir(parents=True, exist_ok=True)
        (d / f).write_text("")
    (d / ".complete").write_text(json.dumps({"id": gpu_accel.PACK_ID,
                                             "wheels": [w.filename for w in gpu_accel.WHEELS]}))


MIN = gpu_accel.MIN_SPEEDUP

@pytest.mark.parametrize("mode,installed,check,use", [
    # "cpu" never uses the card, whatever else is true.
    ("cpu", True, {"ok": True, "speedup": 10}, False),
    ("cpu", False, None, False),
    # Nothing works without the download.
    ("gpu", False, {"ok": True, "speedup": 10}, False),
    ("auto", False, {"ok": True, "speedup": 10}, False),
    # "gpu" needs no speed check at all.
    ("gpu", True, None, True),
    ("gpu", True, {"ok": False}, True),
    ("gpu", True, {"ok": True, "speedup": 0.5}, True),
    # "auto" follows the speed check.
    ("auto", True, None, False),
    ("auto", True, {}, False),
    ("auto", True, {"ok": False, "speedup": 10}, False),
    ("auto", True, {"ok": True, "speedup": MIN}, True),
    ("auto", True, {"ok": True, "speedup": MIN - 0.01}, False),
    ("auto", True, {"ok": True, "speedup": MIN + 0.01}, True),
    ("auto", True, {"ok": True, "speedup": 4.35}, True),
    ("auto", True, {"ok": True, "speedup": 0.6}, False),
    ("auto", True, {"ok": True, "speedup": "3.0"}, True),
    ("auto", True, {"ok": True, "speedup": float("nan")}, False),
    ("auto", True, {"ok": True}, False),
    ("auto", True, {"ok": True, "speedup": "fast"}, False),
    ("auto", True, {"ok": True, "speedup": None}, False),
    # An unknown mode behaves like "auto".
    ("banana", True, {"ok": True, "speedup": 4}, True),
    ("banana", True, None, False),
])
def test_decide(mode, installed, check, use):
    got, why = gpu_accel.decide(mode, installed, check)
    assert got is use
    assert isinstance(why, str) and why


def test_decide_reasons():
    assert "processor was chosen" in gpu_accel.decide("cpu", True, None)[1]
    assert "not downloaded" in gpu_accel.decide("auto", False, None)[1]
    assert "chosen in Settings" in gpu_accel.decide("gpu", True, None)[1]
    assert "has not run" in gpu_accel.decide("auto", True, None)[1]
    assert "failed" in gpu_accel.decide("auto", True, {"ok": False})[1]
    assert "processor was faster" in gpu_accel.decide("auto", True, {"ok": True, "speedup": 1.1})[1]
    assert "4.0x faster" in gpu_accel.decide("auto", True, {"ok": True, "speedup": 4})[1]


def test_startup_cpu_mode(win):
    install_fake_pack()
    gpu_accel.save_check({"ok": True, "speedup": 4})
    assert gpu_accel.startup("cpu") is False
    assert win == []
    assert "processor" in gpu_accel.reason()


def test_startup_not_installed(win):
    gpu_accel.save_check({"ok": True, "speedup": 4})
    assert gpu_accel.startup("auto") is False
    assert gpu_accel.startup("gpu") is False
    assert win == []
    assert "not downloaded" in gpu_accel.reason()


def test_startup_auto_check_not_passed(win):
    install_fake_pack()
    assert gpu_accel.startup("auto") is False          # no check at all
    gpu_accel.save_check({"ok": True, "speedup": 1.0})
    assert gpu_accel.startup("auto") is False          # not fast enough
    gpu_accel.save_check({"ok": False, "speedup": 9})
    assert gpu_accel.startup("auto") is False          # failed
    assert win == []


def test_startup_auto_all_good(win):
    install_fake_pack()
    gpu_accel.save_check({"ok": True, "speedup": 4, "gpu_name": "NVIDIA GeForce RTX 4070"})
    assert gpu_accel.startup("auto") is True
    assert win == [{}]
    assert gpu_accel.device_name() == "NVIDIA GeForce RTX 4070"
    assert "faster" in gpu_accel.reason()
    assert gpu_accel.active()


def test_startup_gpu_mode_without_check(win):
    install_fake_pack()
    assert gpu_accel.startup("gpu") is True
    assert win == [{}]
    assert gpu_accel.device_name() == "NVIDIA GPU"
    assert "chosen in Settings" in gpu_accel.reason()


def test_startup_activate_refused(win, monkeypatch):
    install_fake_pack()
    gpu_accel.save_check({"ok": True, "speedup": 4})
    monkeypatch.setattr(gpu_accel, "activate", lambda **k: False)
    assert gpu_accel.startup("auto") is False
    assert gpu_accel.device_name() == ""


@pytest.mark.parametrize("broken", ["_apply_pending_removal", "pack_installed", "load_check",
                                    "activate", "decide"])
def test_startup_never_raises(win, monkeypatch, broken):
    install_fake_pack()
    gpu_accel.save_check({"ok": True, "speedup": 4})

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(gpu_accel, broken, boom)
    assert gpu_accel.startup("auto") is False
    assert gpu_accel.startup("gpu") is False


# ------------------------------------------------------------------ #
# 10. piper_tts on the card                                           #
# ------------------------------------------------------------------ #

class FakeVoice:
    def __init__(self, gpu: bool, fail: BaseException | None = None):
        self.gpu = gpu
        self.fail = fail
        self.runs = 0
        self.config = types.SimpleNamespace(sample_rate=22_050, length_scale=1.0)

    def phonemize(self, text):
        return [list("hello world.") for _ in text.split(".") if _.strip()]

    def phonemes_to_ids(self, phonemes):
        return [1, 2, 3]

    def phoneme_ids_to_audio(self, ids, cfg):
        import numpy as np

        self.runs += 1
        if self.fail is not None:
            raise self.fail
        return np.full(220, 0.5 if not self.gpu else 0.25, dtype=np.float32)


@pytest.fixture
def piper_env(monkeypatch, tmp_path, fresh_cache):
    """Fake voices: one per (path, device); GPU support 'active'."""
    env = types.SimpleNamespace(opened=[], gpu_open_error=None, gpu_run_error=None,
                                cpu_open_error=None, cpu_run_error=None,
                                path=tmp_path / "en_US-test-medium.onnx", cache=fresh_cache)

    def fake_open(model, gpu=False):
        env.opened.append((model, gpu))
        if gpu and env.gpu_open_error is not None:
            raise env.gpu_open_error
        if not gpu and env.cpu_open_error is not None:
            raise env.cpu_open_error
        return FakeVoice(gpu, env.gpu_run_error if gpu else env.cpu_run_error)

    monkeypatch.setattr(piper_tts, "_open_voice", fake_open)
    monkeypatch.setattr(piper_tts, "_wants_gpu", lambda: gpu_accel.failure() is None)
    monkeypatch.setattr(piper_tts, "model_path", lambda key: env.path)
    return env


def test_load_gpu_voice(piper_env):
    voice = piper_tts._load(piper_env.path)
    assert voice.gpu and voice.setuptts_on_gpu is True
    assert list(piper_env.cache) == [f"{piper_env.path}#gpu"]
    assert piper_env.opened == [(str(piper_env.path), True)]
    assert piper_tts._load(piper_env.path) is voice     # cached


def test_load_gpu_failure_falls_back_to_cpu(piper_env, monkeypatch):
    piper_env.gpu_open_error = piper_tts.GpuUnavailable("no CUDA")
    marked = []
    real = gpu_accel.mark_failed
    monkeypatch.setattr(gpu_accel, "mark_failed", lambda r: marked.append(r) or real(r))
    voice = piper_tts._load(piper_env.path)
    assert not voice.gpu and voice.setuptts_on_gpu is False
    assert len(marked) == 1 and "no CUDA" in marked[0]
    assert gpu_accel.failure() is not None
    assert list(piper_env.cache) == [str(piper_env.path)]
    assert piper_env.opened == [(str(piper_env.path), True), (str(piper_env.path), False)]


def test_load_gpu_and_cpu_both_fail_gpu_not_blamed(piper_env, monkeypatch):
    # A broken model fails everywhere: that is not the card's fault.
    piper_env.gpu_open_error = RuntimeError("gpu: bad model")
    piper_env.cpu_open_error = RuntimeError("cpu: bad model")
    monkeypatch.setattr(gpu_accel, "mark_failed", lambda r: pytest.fail("must not mark"))
    with pytest.raises(RuntimeError, match="cpu: bad model"):
        piper_tts._load(piper_env.path)
    assert gpu_accel.failure() is None
    assert not piper_env.cache


def test_load_gpu_failure_reuses_cached_cpu_voice(piper_env):
    cpu = piper_tts._load(piper_env.path, gpu=False)
    lock = cpu.setuptts_run_lock
    piper_env.gpu_open_error = piper_tts.GpuUnavailable("no CUDA")
    assert piper_tts._load(piper_env.path) is cpu
    # The cached voice keeps its run limiter (jobs already hold it).
    assert cpu.setuptts_run_lock is lock
    assert gpu_accel.failure() is not None


def test_load_forced_gpu_failure_propagates(piper_env, monkeypatch):
    piper_env.gpu_open_error = RuntimeError("cudaErrorNoDevice")
    monkeypatch.setattr(gpu_accel, "mark_failed", lambda r: pytest.fail("must not mark"))
    with pytest.raises(RuntimeError, match="cudaErrorNoDevice"):
        piper_tts._load(piper_env.path, gpu=True)
    assert not piper_env.cache


def test_load_forced_cpu_ignores_gpu_support(piper_env):
    voice = piper_tts._load(piper_env.path, gpu=False)
    assert not voice.gpu and voice.setuptts_on_gpu is False
    assert list(piper_env.cache) == [str(piper_env.path)]


def test_synth_falls_back_to_cpu_mid_job(piper_env):
    piper_env.gpu_run_error = RuntimeError("CUDA failure 700: illegal memory access")
    synth = piper_tts.PiperSynthesizer("piper:en_US-test-medium")
    assert synth.on_gpu
    pcm = synth.synthesize("One sentence. Another sentence.", "+0%")
    assert pcm                                   # the job carried on
    assert not synth.on_gpu
    assert "illegal memory access" in gpu_accel.failure()
    assert f"{piper_env.path}#gpu" not in piper_env.cache
    assert str(piper_env.path) in piper_env.cache
    # Later sentences go straight to the processor.
    assert synth.synthesize("Third.", "+0%")
    assert [o for o in piper_env.opened if o[1]] == [(str(piper_env.path), True)]
    # New jobs start on the processor too.
    assert not piper_tts.PiperSynthesizer("piper:en_US-test-medium").on_gpu


def test_synth_fallback_with_parallel_runs(piper_env, monkeypatch):
    monkeypatch.setattr(piper_tts, "_PARALLEL_RUNS", 2)
    piper_env.gpu_run_error = RuntimeError("CUDA error")
    with piper_tts.PiperSynthesizer("piper:en_US-test-medium") as synth:
        pcm = synth.synthesize("A. B. C. D. E. F.", "+0%")
    assert len(pcm) > 0
    assert not synth.on_gpu
    # Only one CPU voice was loaded even though several sentences failed.
    assert [o for o in piper_env.opened if not o[1]] == [(str(piper_env.path), False)]


def test_synth_cpu_retry_also_fails_keeps_gpu(piper_env, monkeypatch):
    # The same sentence fails on the processor too: a model/input problem,
    # so the card is not blamed and stays in use.
    piper_env.gpu_run_error = RuntimeError("gpu: bad input")
    piper_env.cpu_run_error = RuntimeError("cpu: bad input")
    monkeypatch.setattr(gpu_accel, "mark_failed", lambda r: pytest.fail("must not mark"))
    synth = piper_tts.PiperSynthesizer("piper:en_US-test-medium")
    with pytest.raises(RuntimeError, match="cpu: bad input"):
        synth.synthesize("Hello.", "+0%")
    assert gpu_accel.failure() is None
    assert synth.on_gpu
    assert f"{piper_env.path}#gpu" in piper_env.cache


def test_synth_forced_gpu_error_propagates(piper_env, monkeypatch):
    piper_env.gpu_run_error = RuntimeError("CUDA error")
    monkeypatch.setattr(gpu_accel, "mark_failed", lambda r: pytest.fail("must not mark"))
    synth = piper_tts.PiperSynthesizer("piper:en_US-test-medium", gpu=True)
    assert synth.on_gpu
    with pytest.raises(RuntimeError, match="CUDA error"):
        synth.synthesize("Hello.", "+0%")
    assert gpu_accel.failure() is None


def test_synth_cancel_is_not_swallowed(piper_env, monkeypatch):
    piper_env.gpu_run_error = piper_tts.PiperCancelled("cancelled")
    monkeypatch.setattr(gpu_accel, "mark_failed", lambda r: pytest.fail("must not mark"))
    synth = piper_tts.PiperSynthesizer("piper:en_US-test-medium")
    with pytest.raises(piper_tts.PiperCancelled):
        synth.synthesize("Hello.", "+0%")
    assert synth.on_gpu
    assert not [o for o in piper_env.opened if not o[1]]


def test_synth_cancel_during_gpu_failure_still_cancels(piper_env):
    synth = piper_tts.PiperSynthesizer("piper:en_US-test-medium")

    class CancelThenFail(Exception):
        pass

    def fail_and_cancel(ids, cfg):
        synth.cancel()
        raise CancelThenFail("CUDA error")

    synth._voice.phoneme_ids_to_audio = fail_and_cancel
    with pytest.raises(piper_tts.PiperCancelled):
        synth.synthesize("Hello.", "+0%")


def test_synth_load_failure_wrapped(piper_env, monkeypatch):
    piper_env.gpu_open_error = RuntimeError("no device")
    with pytest.raises(piper_tts.PiperError, match="Could not load"):
        piper_tts.PiperSynthesizer("piper:en_US-test-medium", gpu=True)


def test_benchmark_reports_device(piper_env):
    result = piper_tts.benchmark("piper:en_US-test-medium", gpu=True, text="One. Two.", repeats=1)
    assert result["on_gpu"] is True
    assert result["chars_per_s"] > 0 and result["audio_s"] > 0
    result = piper_tts.benchmark("piper:en_US-test-medium", gpu=False, text="One. Two.", repeats=1)
    assert result["on_gpu"] is False


def test_remove_voice_evicts_cpu_and_gpu_entries(monkeypatch, tmp_path, fresh_cache):
    monkeypatch.setattr(piper_tts, "user_dir", lambda: tmp_path)
    for name in ("gone.onnx", "gone.onnx.json"):
        (tmp_path / name).write_bytes(b"x")
    fresh_cache[str(tmp_path / "gone.onnx")] = object()
    fresh_cache[str(tmp_path / "gone.onnx") + "#gpu"] = object()
    fresh_cache[str(tmp_path / "kept.onnx") + "#gpu"] = object()
    assert piper_tts.remove_voice("gone") is True
    assert list(fresh_cache) == [str(tmp_path / "kept.onnx") + "#gpu"]
    assert not (tmp_path / "gone.onnx").exists()


def test_cache_key_roundtrip(tmp_path):
    p = tmp_path / "en_US-a-medium.onnx"
    assert piper_tts._cache_key(p, False) == str(p)
    assert piper_tts._cache_key(p, True) == f"{p}#gpu"
    assert piper_tts._key_path(piper_tts._cache_key(p, True)) == p
    assert piper_tts._key_path(piper_tts._cache_key(p, False)) == p


# ------------------------------------------------------------------ #
# 11. The speed-check process                                         #
# ------------------------------------------------------------------ #

def test_check_unsupported(monkeypatch):
    monkeypatch.setattr(gpu_accel, "SUPPORTED", False)
    monkeypatch.setattr(gpu_accel, "detect_gpus", lambda: pytest.fail("must not detect"))
    result = gpu_check.check(["--gpu-check", "x.json"])
    assert result["ok"] is False and result["recommended"] is False
    assert "only available on Windows" in result["error"]


def test_check_no_gpu(monkeypatch, python_ok):
    monkeypatch.setattr(gpu_accel, "SUPPORTED", True)
    monkeypatch.setattr(gpu_accel, "detect_gpus", lambda: [])
    result = gpu_check.check(["--gpu-check", "x.json"])
    assert result["error"] == gpu_accel.requirement_problem(None)
    assert result["ok"] is False


def test_check_old_driver_reports_card(monkeypatch, python_ok):
    monkeypatch.setattr(gpu_accel, "SUPPORTED", True)
    monkeypatch.setattr(gpu_accel, "detect_gpus", lambda: [good_gpu(driver="566.36")])
    result = gpu_check.check(["--gpu-check", "x.json"])
    assert "too old" in result["error"]
    assert result["gpu_name"] == "NVIDIA GeForce RTX 4070"
    assert result["driver"] == "566.36" and result["compute"] == "8.9"


def test_check_force_without_pack(monkeypatch):
    monkeypatch.setattr(gpu_accel, "SUPPORTED", True)
    monkeypatch.setattr(gpu_accel, "detect_gpus", lambda: [])
    result = gpu_check.check(["--gpu-check", "x.json", "--force"])
    assert "not downloaded" in result["error"]


def test_check_activation_refused(monkeypatch, python_ok):
    monkeypatch.setattr(gpu_accel, "SUPPORTED", True)
    monkeypatch.setattr(gpu_accel, "detect_gpus", lambda: [good_gpu()])
    monkeypatch.setattr(gpu_accel, "pack_installed", lambda: True)
    monkeypatch.setattr(gpu_accel, "activate", lambda **k: False)
    assert "could not be loaded" in gpu_check.check(["--gpu-check", "x"])["error"]


@pytest.fixture
def check_env(monkeypatch, python_ok):
    import onnxruntime

    monkeypatch.setattr(gpu_accel, "SUPPORTED", True)
    monkeypatch.setattr(gpu_accel, "detect_gpus", lambda: [good_gpu()])
    monkeypatch.setattr(gpu_accel, "pack_installed", lambda: True)
    monkeypatch.setattr(gpu_accel, "activate", lambda **k: True)
    monkeypatch.setattr(onnxruntime, "get_available_providers",
                        lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"])
    monkeypatch.setattr(gpu_accel, "cuda_provider_loads", lambda: (True, ""))
    monkeypatch.setattr(piper_tts, "installed_keys", lambda: {piper_tts.BUNDLED_VOICE, "de_DE-x-low"})
    env = types.SimpleNamespace(cpu={"chars_per_s": 100.0, "audio_s": 10.0, "on_gpu": False},
                                gpu={"chars_per_s": 400.0, "audio_s": 10.2, "on_gpu": True},
                                gpu_error=None, calls=[])

    def bench(short, *, gpu):
        env.calls.append((short, gpu))
        if gpu and env.gpu_error:
            raise env.gpu_error
        return dict(env.gpu if gpu else env.cpu)

    monkeypatch.setattr(piper_tts, "benchmark", bench)
    return env


def test_check_success(check_env):
    result = gpu_check.check(["--gpu-check", "x"])
    assert result["ok"] is True and result["recommended"] is True
    assert result["speedup"] == 4.0
    assert result["onnxruntime_file"].endswith("__init__.py")
    assert result["voice"] == piper_tts.BUNDLED_VOICE
    assert check_env.calls == [("piper:" + piper_tts.BUNDLED_VOICE, False),
                               ("piper:" + piper_tts.BUNDLED_VOICE, True)]


def test_check_card_not_faster(check_env):
    check_env.gpu["chars_per_s"] = 110.0
    result = gpu_check.check(["--gpu-check", "x"])
    assert result["ok"] is True and result["recommended"] is False
    assert result["speedup"] == 1.1


def test_check_voice_option(check_env):
    result = gpu_check.check(["--gpu-check", "x", "--voice", "piper:de_DE-x-low"])
    assert result["voice"] == "de_DE-x-low"
    result = gpu_check.check(["--gpu-check", "x", "--voice", "nope"])
    assert "No offline voice" in result["error"]


def test_check_gpu_errors(check_env):
    check_env.gpu_error = RuntimeError("CUDA init failed")
    result = gpu_check.check(["--gpu-check", "x"])
    assert "could not run the voice" in result["error"] and not result["ok"]
    check_env.gpu_error = None
    check_env.gpu["on_gpu"] = False
    assert "did not run on the graphics card" in gpu_check.check(["--gpu-check", "x"])["error"]
    check_env.gpu["on_gpu"] = True
    check_env.gpu["audio_s"] = 3.0
    assert "unexpected audio" in gpu_check.check(["--gpu-check", "x"])["error"]


def test_check_cuda_libraries_do_not_load(check_env, monkeypatch):
    monkeypatch.setattr(gpu_accel, "cuda_provider_loads",
                        lambda: (False, "cudnn64_9.dll: The specified module could not be found"))
    result = gpu_check.check(["--gpu-check", "x"])
    assert result["cuda_provider_loads"] is False
    assert result["error"].startswith("The CUDA libraries in GPU support did not load: cudnn64_9.dll")
    assert check_env.calls == []                       # no voice was run


def test_check_records_cuda_provider_loads(check_env):
    assert gpu_check.check(["--gpu-check", "x"])["cuda_provider_loads"] is True


def test_check_no_cuda_provider(check_env, monkeypatch):
    import onnxruntime

    monkeypatch.setattr(onnxruntime, "get_available_providers", lambda: ["CPUExecutionProvider"])
    assert "no CUDA provider" in gpu_check.check(["--gpu-check", "x"])["error"]


def test_run_writes_json(monkeypatch, tmp_path, _isolated):
    import logging

    configured = []
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: configured.append(kw))
    monkeypatch.setattr(gpu_check, "check",
                        lambda argv: {"ok": True, "speedup": 3.2, "recommended": True, "error": ""})
    out = tmp_path / "result.json"
    assert gpu_check.run(["main.py", "--gpu-check", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["speedup"] == 3.2
    assert Path(configured[0]["filename"]).resolve().is_relative_to(_isolated.resolve())


def test_run_survives_crashing_check(monkeypatch, tmp_path):
    import logging

    monkeypatch.setattr(logging, "basicConfig", lambda **kw: None)

    def boom(argv):
        raise ValueError("kaput")

    monkeypatch.setattr(gpu_check, "check", boom)
    out = tmp_path / "result.json"
    assert gpu_check.run(["main.py", "--gpu-check", str(out)]) == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ok"] is False and "kaput" in data["error"]


def test_arg_parsing():
    assert gpu_check._arg(["a", "--gpu-check", "out.json"], "--gpu-check") == "out.json"
    assert gpu_check._arg(["a", "--gpu-check", "--force"], "--gpu-check") is None
    assert gpu_check._arg(["a", "--gpu-check"], "--gpu-check") is None
    assert gpu_check._arg(["a"], "--voice") is None


# ------------------------------------------------------------------ #
# 12. check_command                                                   #
# ------------------------------------------------------------------ #

def test_check_command_unfrozen(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    out = tmp_path / "r.json"
    repo = Path(__file__).resolve().parents[1]
    assert gpu_accel.check_command(out) == [sys.executable, str(repo / "main.py"),
                                            "--gpu-check", str(out)]
    assert (repo / "main.py").is_file()


def test_check_command_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    out = tmp_path / "r.json"
    assert gpu_accel.check_command(out) == [sys.executable, "--gpu-check", str(out)]


def test_settings_gpu_mode(_isolated):
    from app.config.settings import GPU_MODES, AppSettings
    from app.utils.paths import AppPaths

    assert GPU_MODES == ("auto", "gpu", "cpu")
    settings = AppSettings(AppPaths())
    assert settings.gpu_mode == "auto"
    for mode in GPU_MODES:
        settings.gpu_mode = mode
        assert settings.gpu_mode == mode
    settings.gpu_mode = "turbo"
    assert settings.gpu_mode == "auto"
    settings.gpu_mode = None
    assert settings.gpu_mode == "auto"

    settings.gpu_mode = "cpu"
    settings.save()
    assert AppSettings(AppPaths()).gpu_mode == "cpu"


@pytest.mark.parametrize("stored", ["turbo", 1, None, True, ["gpu"]])
def test_settings_gpu_mode_invalid_on_disk_reads_auto(_isolated, stored):
    from app.config.settings import AppSettings
    from app.utils.paths import AppPaths

    path = AppPaths().settings_path
    path.write_text(json.dumps({"gpu_mode": stored}), encoding="utf-8")
    assert AppSettings(AppPaths()).gpu_mode == "auto"


# ------------------------------------------------------------------ #
# 13. A Windows PC with no NVIDIA card (CPU only, AMD or Intel)       #
# ------------------------------------------------------------------ #

@pytest.fixture
def no_nvidia(monkeypatch, python_ok):
    monkeypatch.setattr(gpu_accel, "SUPPORTED", True)
    return monkeypatch


def test_no_nvidia_smi_means_no_card(no_nvidia):
    no_nvidia.setattr(gpu_accel, "_nvidia_smi", lambda: None)
    no_nvidia.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    assert gpu_accel.detect_gpus() == []
    assert gpu_accel.requirement_problem(None) == "No NVIDIA graphics card was found on this PC."


def test_leftover_nvidia_smi_without_card(no_nvidia):
    no_nvidia.setattr(gpu_accel, "_nvidia_smi", lambda: r"C:\Windows\System32\nvidia-smi.exe")
    no_nvidia.setattr(subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(
        returncode=6, stdout="No devices were found\n", stderr=""))
    assert gpu_accel.detect_gpus() == []


def test_nvidia_smi_timeout(no_nvidia):
    no_nvidia.setattr(gpu_accel, "_nvidia_smi", lambda: "nvidia-smi")

    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    no_nvidia.setattr(subprocess, "run", hang)
    assert gpu_accel.detect_gpus(timeout=0.01) == []


@pytest.mark.parametrize("mode", ["auto", "gpu", "cpu"])
def test_startup_on_pc_without_nvidia(no_nvidia, mode):
    no_nvidia.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no nvidia-smi at launch"))
    no_nvidia.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("no process at launch"))
    no_nvidia.setattr(gpu_accel, "detect_gpus", lambda *a, **k: pytest.fail("no detection at launch"))
    before = list(sys.meta_path)
    assert gpu_accel.startup(mode) is False
    assert sys.meta_path == before
    assert not gpu_accel.activated() and not gpu_accel.active()


def test_synth_on_pc_without_nvidia_uses_cpu(no_nvidia, tmp_path, fresh_cache):
    opened = []

    def fake_open(*a, **k):
        opened.append((a, k))
        return FakeVoice(gpu=bool(k.get("gpu")))

    path = tmp_path / "en_US-test-medium.onnx"
    no_nvidia.setattr(sys, "platform", "win32")      # the real _wants_gpu
    no_nvidia.setattr(piper_tts, "_open_voice", fake_open)
    no_nvidia.setattr(piper_tts, "model_path", lambda key: path)
    assert gpu_accel.startup("auto") is False
    synth = piper_tts.PiperSynthesizer("piper:en_US-test-medium")
    assert not synth.on_gpu
    assert opened == [((str(path),), {})]
    assert list(fresh_cache) == [str(path)]
    assert not any(k.endswith("#gpu") for k in fresh_cache)
    no_nvidia.setattr(sys, "platform", "darwin")
    assert synth.synthesize("Hello there.", "+0%")


def test_gpu_check_without_nvidia(no_nvidia):
    no_nvidia.setattr(gpu_accel, "_nvidia_smi", lambda: None)
    no_nvidia.setattr(gpu_accel, "download_pack", lambda *a, **k: pytest.fail("no download"))
    no_nvidia.setattr(gpu_accel, "activate", lambda **k: pytest.fail("no activation"))
    no_nvidia.setattr(gpu_accel, "pack_installed", lambda: pytest.fail("not even looked at"))
    before = list(sys.meta_path)
    result = gpu_check.check(["main.py", "--gpu-check", "out.json"])
    assert result["ok"] is False and result["recommended"] is False
    assert result["error"] == "No NVIDIA graphics card was found on this PC."
    assert result["gpu_name"] == ""
    assert sys.meta_path == before


# ------------------------------------------------------------------ #
# 14. Pack integrity, rename retry, partial removal                   #
# ------------------------------------------------------------------ #

def test_pack_installed_needs_every_key_file():
    install_fake_pack()
    assert gpu_accel.pack_installed()
    for f in gpu_accel._KEY_FILES:
        key = gpu_accel.pack_dir() / f
        key.unlink()
        assert not gpu_accel.pack_installed(), f
        key.write_text("")
    assert gpu_accel.pack_installed()


@pytest.mark.parametrize("manifest", [
    "",                                        # empty
    "{not json",
    "[1, 2]",
    json.dumps({"id": gpu_accel.PACK_ID}),    # no wheel list
    json.dumps({"wheels": ["onnxruntime_gpu-1.29.0-cp312-cp312-win_amd64.whl"]}),
    json.dumps({"wheels": [w.filename for w in gpu_accel.WHEELS][:-1]}),   # one missing
    json.dumps({"wheels": [w.filename for w in reversed(gpu_accel.WHEELS)]}),
])
def test_pack_installed_rejects_bad_manifest(manifest):
    install_fake_pack()
    (gpu_accel.pack_dir() / ".complete").write_text(manifest)
    assert not gpu_accel.pack_installed()


def test_pack_installed_missing_manifest():
    install_fake_pack()
    (gpu_accel.pack_dir() / ".complete").unlink()
    assert not gpu_accel.pack_installed()


def test_download_rename_failure_resumes_without_redownload(pack_env, monkeypatch):
    real = gpu_accel.replace_with_retry
    failures = {"left": 1}

    def flaky(src, dst, *a, **k):
        if Path(src).is_dir() and failures["left"]:
            failures["left"] -= 1
            raise PermissionError(13, "Access is denied", str(src))
        return real(src, dst, *a, **k)

    monkeypatch.setattr(gpu_accel, "replace_with_retry", flaky)
    with pytest.raises(PermissionError):
        gpu_accel.download_pack()
    assert not gpu_accel.pack_installed()
    staging = pack_env.root / f"{gpu_accel.PACK_ID}.staging"
    assert len(list(staging.glob(".done-*"))) == 2       # flags kept

    pack_env.server.requests.clear()
    gpu_accel.download_pack()
    assert pack_env.server.requests == []                # nothing downloaded again
    assert gpu_accel.pack_installed()
    assert not list(gpu_accel.pack_dir().glob(".done-*"))
    assert not staging.exists()


def test_download_dir_rename_uses_generous_retries(pack_env, monkeypatch):
    calls = []
    real = gpu_accel.replace_with_retry
    monkeypatch.setattr(gpu_accel, "replace_with_retry",
                        lambda src, dst, **k: calls.append((Path(src).is_dir(), k)) or real(src, dst, **k))
    gpu_accel.download_pack()
    assert (True, {"attempts": 30}) in calls
    assert sum(1 for is_dir, _ in calls if not is_dir) == 2   # each .part → wheel


def test_pending_removal_keeps_marker_while_something_is_locked(pack_env, monkeypatch):
    gpu_accel.download_pack()
    gpu_accel.save_check({"ok": True, "speedup": 3})
    (pack_env.root / ".remove").write_text("1")
    locked = gpu_accel._check_path()
    real_unlink = Path.unlink

    def unlink(self, *a, **k):
        if self == locked:
            raise PermissionError(13, "in use", str(self))
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", unlink)
    gpu_accel._apply_pending_removal()
    assert locked.exists()
    assert (pack_env.root / ".remove").exists()        # retried next start
    assert not gpu_accel.pack_dir().exists()           # the rest is gone
    assert not gpu_accel.pack_installed()

    monkeypatch.setattr(Path, "unlink", real_unlink)   # next start: unlocked
    gpu_accel._apply_pending_removal()
    assert not pack_env.root.exists()


def test_pending_removal_keeps_marker_when_a_folder_is_locked(pack_env, monkeypatch):
    gpu_accel.download_pack()
    (pack_env.root / ".remove").write_text("1")
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)   # a DLL in use
    gpu_accel._apply_pending_removal()
    assert (pack_env.root / ".remove").exists()
    assert gpu_accel.pack_dir().exists()
    assert not gpu_accel.pack_installed()


# ------------------------------------------------------------------ #
# 15. startup importing the GPU build; activate/deactivate details   #
# ------------------------------------------------------------------ #

def test_startup_import_failure_undoes_activation(win, monkeypatch):
    install_fake_pack()
    monkeypatch.setitem(sys.modules, "onnxruntime", None)   # import raises ImportError
    assert gpu_accel.startup("gpu") is False
    assert win == [{}]
    assert gpu_accel._STATE.finder is None
    assert not any(isinstance(f, gpu_accel._RedirectFinder) for f in sys.meta_path)
    assert not gpu_accel.activated() and not gpu_accel.active()
    assert gpu_accel.reason().startswith("GPU support failed to load")
    assert gpu_accel.failure() and gpu_accel.failure().startswith("GPU support failed to load")
    assert gpu_accel.device_name() == ""


def test_startup_import_ok_keeps_finder(win):
    install_fake_pack()
    assert gpu_accel.startup("gpu") is True
    assert gpu_accel._STATE.finder in sys.meta_path
    assert gpu_accel.failure() is None


def test_startup_wrong_python_never_activates(win, monkeypatch):
    install_fake_pack()
    monkeypatch.setattr(gpu_accel, "PYTHON_TAG", (3, 0))
    assert gpu_accel.startup("gpu") is False
    assert win == []
    assert "can't use the downloaded GPU support" in gpu_accel.reason()


def test_deactivate_removes_finder_and_modules(tmp_path):
    import importlib

    make_fake_package(tmp_path, "gpu-build")
    importlib.invalidate_caches()
    assert gpu_accel.activate(directory=tmp_path, package="fakeortpkg")
    finder = gpu_accel._STATE.finder
    assert finder is sys.meta_path[0]
    importlib.import_module("fakeortpkg.sub.mod")
    gpu_accel._deactivate("fakeortpkg")
    assert finder not in sys.meta_path
    assert gpu_accel._STATE.finder is None
    assert not gpu_accel.activated()
    assert not [m for m in sys.modules if m.startswith("fakeortpkg")]
    gpu_accel._deactivate("fakeortpkg")      # idempotent


def test_activate_off_windows_records_no_dll_dirs(tmp_path, monkeypatch):
    make_fake_package(tmp_path, "gpu-build")
    (tmp_path / "nvidia" / "bin").mkdir(parents=True)
    (tmp_path / "nvidia" / "bin" / "a.dll").write_bytes(b"x")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("CUDA_DEVICE_ORDER", raising=False)
    path_before = __import__("os").environ.get("PATH")
    assert gpu_accel.activate(directory=tmp_path, package="fakeortpkg")
    assert gpu_accel._STATE.dll_dirs == []
    assert "CUDA_DEVICE_ORDER" not in __import__("os").environ
    assert __import__("os").environ.get("PATH") == path_before


def test_ensure_cuda_loaded_and_provider_check_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    gpu_accel.ensure_cuda_loaded()
    assert gpu_accel._STATE.cuda_loaded is False
    assert gpu_accel.cuda_provider_loads() == (False, "not Windows")


def test_ensure_cuda_loaded_once_per_session(monkeypatch, tmp_path):
    import ctypes

    monkeypatch.setattr(sys, "platform", "win32")
    d = tmp_path / "bin"
    d.mkdir()
    for name in gpu_accel._PRELOAD_DLLS[:2]:
        (d / name).write_bytes(b"x")
    gpu_accel._STATE.dll_dirs = [d]
    loaded = []

    def windll(path):
        loaded.append(Path(path).name)
        if len(loaded) == 2:
            raise OSError("bad image")      # one failing DLL is only logged
        return object()

    monkeypatch.setattr(ctypes, "WinDLL", windll, raising=False)
    gpu_accel.ensure_cuda_loaded()
    assert loaded == list(gpu_accel._PRELOAD_DLLS[:2])   # missing ones skipped
    gpu_accel.ensure_cuda_loaded()
    assert len(loaded) == 2                               # once only
    assert gpu_accel._STATE.cuda_loaded


# ------------------------------------------------------------------ #
# 16. selftest --gpu-startup                                          #
# ------------------------------------------------------------------ #

def test_selftest_gpu_startup(monkeypatch, tmp_path):
    from app import selftest
    from app.services import local_tts, system_tts
    from app.utils import mp3_duration

    calls = []

    def fake_startup(mode):
        calls.append(mode)
        gpu_accel._STATE.reason = "the graphics card was chosen in Settings"
        return False

    monkeypatch.setattr(gpu_accel, "startup", fake_startup)
    # Keep the other checks cheap and offline.
    model = tmp_path / "v.onnx"
    monkeypatch.setattr(piper_tts, "piper_available", lambda: True)
    monkeypatch.setattr(piper_tts, "model_path", lambda key: model)

    def fake_mp3(short, text, rate, out, **kw):
        gpu_accel.mark_failed("CUDA error: no device")
        Path(out).write_bytes(b"x")
        return 3.0

    monkeypatch.setattr(local_tts, "synthesize_to_mp3", fake_mp3)
    monkeypatch.setattr(mp3_duration, "mp3_duration_seconds", lambda p: 3.0)
    monkeypatch.setattr(system_tts, "list_system_voices", lambda: [])
    out = tmp_path / "selftest.json"
    selftest.run(["SetupTTS", "--selftest", str(out), "--gpu-startup"])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert calls == ["gpu"]
    assert data["gpu_startup"]["activated"] is False
    assert data["gpu_startup"]["reason"] == "the graphics card was chosen in Settings"
    assert data["checks"]["offline_voice"]["ok"], data["checks"]["offline_voice"]
    assert data["gpu_startup"]["failure_after_voice"] == "CUDA error: no device"


def test_selftest_without_flag_never_starts_gpu(monkeypatch, tmp_path):
    from app import selftest
    from app.services import system_tts

    monkeypatch.setattr(gpu_accel, "startup", lambda mode: pytest.fail("must not start"))
    monkeypatch.setattr(piper_tts, "model_path", lambda key: None)
    monkeypatch.setattr(system_tts, "list_system_voices", lambda: [])
    out = tmp_path / "selftest.json"
    selftest.run(["SetupTTS", "--selftest", str(out)])
    assert "gpu_startup" not in json.loads(out.read_text(encoding="utf-8"))
