import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "SETUPTTS_DATA_DIR",
    str(Path(tempfile.gettempdir()) / "setuptts-test-data"),
)


import pytest


@pytest.fixture(autouse=True)
def _no_real_local_voices(request, monkeypatch):
    """
    Keep the voice list deterministic: by default tests see no voices
    installed on the machine running them (built-in or offline).  Tests that
    exercise the real engines opt in with ``@pytest.mark.real_local_voices``.
    """
    if request.node.get_closest_marker("real_local_voices"):
        return
    from app.workers import voice_loader
    monkeypatch.setattr(voice_loader, "list_local_voices", lambda: [])


@pytest.fixture(autouse=True)
def _private_kokoro_dir(request, tmp_path_factory, monkeypatch):
    """Each test starts with no Kokoro model or voices downloaded."""
    if request.node.get_closest_marker("real_local_voices"):
        return
    from app.services import kokoro_tts

    root = tmp_path_factory.mktemp("kokoro")

    def _user_dir():
        (root / "voices").mkdir(parents=True, exist_ok=True)
        return root

    monkeypatch.setattr(kokoro_tts, "user_dir", _user_dir)


@pytest.fixture(autouse=True)
def _service_always_reachable(request, monkeypatch):
    """
    Tests inject network failures; the outage probe must not open a real
    connection.  Tests of the outage wait itself patch it explicitly.
    """
    from app.workers import tts_worker

    async def _reachable() -> bool:
        return True

    monkeypatch.setattr(tts_worker, "_service_reachable", _reachable)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_local_voices: use the voices installed on this machine")
