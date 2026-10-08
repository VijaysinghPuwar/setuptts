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


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_local_voices: use the voices installed on this machine")
