"""
Download the offline voice that ships inside SetupTTS into app/assets/piper/.

Run before building (CI does):  python scripts/fetch_bundled_voice.py

The model is 63 MB, so it is fetched at build time rather than kept in git.
Size and MD5 are pinned: a changed or truncated file fails the build instead
of shipping a broken voice.  Already-present, verified files are kept.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "app" / "assets" / "piper"
BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium"

FILES = {
    "en_US-lessac-medium.onnx": (63_201_294, "2fc642b535197b6305c7c8f92dc8b24f"),
    "en_US-lessac-medium.onnx.json": (4_885, "c1f2b7bddefe113f3255ff9ef234cfd3"),
    "MODEL_CARD": (351, "42f2dd4a98149e12fc70b301d9579dfd"),
}


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    for name, (size, md5) in FILES.items():
        target = DEST / name
        if target.exists() and target.stat().st_size == size and _md5(target) == md5:
            print(f"ok      {name}")
            continue
        part = target.with_name(name + ".part")
        print(f"fetch   {name} ({size / 1e6:.1f} MB)")
        with urllib.request.urlopen(f"{BASE}/{name}?download=true", timeout=120) as resp, \
                open(part, "wb") as fh:
            for block in iter(lambda: resp.read(1 << 20), b""):
                fh.write(block)
        if part.stat().st_size != size or _md5(part) != md5:
            part.unlink()
            print(f"::error::{name} failed verification", file=sys.stderr)
            return 1
        part.replace(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
