"""
Per-job chunk staging for checkpoint and resume support.

Each long-running TTS job writes completed logical chunks to a job-specific
staging directory and persists a small manifest alongside the cleaned source
text. Failed, cancelled, or interrupted jobs can later resume from the first
unfinished chunk without regenerating earlier audio.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MANIFEST_NAME = "manifest.json"
_SOURCE_TEXT_NAME = "source.txt"


@dataclass
class ChunkManifest:
    job_id: str
    voice: str
    rate: str
    volume: str
    output_path: str
    text_hash: str
    total_chars: int
    chars_consumed: int = 0
    chunks_completed: list[int] = field(default_factory=list)
    status: str = "running"  # running|interrupted|failed|cancelled|completed
    failed_at_chunk: Optional[int] = None
    failed_at_chunk_total: Optional[int] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class ResumeCandidate:
    job_id: str
    staging_dir: Path
    voice: str
    rate: str
    volume: str
    output_path: str
    text: str
    text_preview: str
    completed_count: int
    failed_at_chunk: int | None
    failed_at_chunk_total: int | None
    chars_consumed: int
    total_chars: int
    status: str
    updated_at: float


class ChunkStore:
    """
    Manages the staging area for one TTS generation job.

    Directory layout::

        {staging_root}/{job_id}/
            manifest.json
            source.txt
            chunk_0000.mp3
            chunk_0001.mp3
            ...
    """

    def __init__(
        self,
        staging_dir: Path,
        manifest: ChunkManifest,
        *,
        save_manifest: bool = True,
    ) -> None:
        self._dir = staging_dir
        self._manifest = manifest
        staging_dir.mkdir(parents=True, exist_ok=True)
        if save_manifest:
            self._save_manifest()

    @classmethod
    def create(
        cls,
        staging_root: Path,
        job_id: str,
        *,
        voice: str,
        rate: str,
        volume: str,
        output_path: str,
        text: str,
    ) -> "ChunkStore":
        """Create a fresh staging store for a new job."""
        staging_dir = staging_root / job_id
        staging_dir.mkdir(parents=True, exist_ok=True)

        text_hash = _text_hash(text)
        manifest = ChunkManifest(
            job_id=job_id,
            voice=voice,
            rate=rate,
            volume=volume,
            output_path=output_path,
            text_hash=text_hash,
            total_chars=len(text),
        )
        store = cls(staging_dir, manifest, save_manifest=False)
        store.save_source_text(text)
        store._save_manifest()
        return store

    @classmethod
    def try_resume(
        cls,
        staging_dir: Path,
        text: str | None = None,
        voice: str | None = None,
    ) -> "Optional[ChunkStore]":
        """
        Try to load a resumable store from an existing staging directory.

        Returns ``None`` if:
        - the directory does not exist
        - the manifest is missing or corrupt
        - the manifest does not have preserved chunk files
        - the stored text or voice do not match the requested resume job
        """
        manifest = _load_manifest(staging_dir)
        if manifest is None:
            return None

        changed = False
        if manifest.status == "completed":
            return None
        if manifest.status == "running":
            manifest.status = "interrupted"
            changed = True

        stored_text = cls.load_source_text(staging_dir)
        compare_text = text if text is not None else stored_text
        if compare_text is None:
            logger.info("No source text found in %s — not resumable", staging_dir)
            return None
        if manifest.text_hash != _text_hash(compare_text):
            logger.info("Source text changed for %s — not resuming", staging_dir)
            return None
        if voice is not None and manifest.voice != voice:
            logger.info("Voice changed for %s — not resuming", staging_dir)
            return None

        valid = _validated_chunks(staging_dir, manifest.chunks_completed)
        if not valid:
            logger.info("No valid chunk files found in %s — not resumable", staging_dir)
            return None

        if valid != manifest.chunks_completed:
            manifest.chunks_completed = valid
            changed = True

        max_consumed = manifest.total_chars
        if manifest.chars_consumed < 0 or manifest.chars_consumed > max_consumed:
            manifest.chars_consumed = max(0, min(manifest.chars_consumed, max_consumed))
            changed = True

        store = cls(staging_dir, manifest, save_manifest=False)
        if changed:
            store._save_manifest()
        return store

    @staticmethod
    def load_source_text(staging_dir: Path) -> str | None:
        source_path = staging_dir / _SOURCE_TEXT_NAME
        if not source_path.exists():
            return None
        try:
            return source_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("Could not read stored source text from %s: %s", source_path, exc)
            return None

    @classmethod
    def list_resume_candidates(cls, staging_root: Path) -> list[ResumeCandidate]:
        """Return resumable jobs sorted by most recently updated first."""
        if not staging_root.is_dir():
            return []

        candidates: list[ResumeCandidate] = []
        for entry in staging_root.iterdir():
            if not entry.is_dir():
                continue

            manifest = _load_manifest(entry)
            if manifest is None or manifest.status == "completed":
                continue

            changed = False
            if manifest.status == "running":
                manifest.status = "interrupted"
                changed = True

            text = cls.load_source_text(entry)
            valid_chunks = _validated_chunks(entry, manifest.chunks_completed)
            if not text or not valid_chunks:
                continue

            if valid_chunks != manifest.chunks_completed:
                manifest.chunks_completed = valid_chunks
                changed = True

            if changed:
                store = cls(entry, manifest, save_manifest=False)
                store._save_manifest()

            preview = text.strip().replace("\n", " ")
            candidates.append(
                ResumeCandidate(
                    job_id=manifest.job_id,
                    staging_dir=entry,
                    voice=manifest.voice,
                    rate=manifest.rate,
                    volume=manifest.volume,
                    output_path=manifest.output_path,
                    text=text,
                    text_preview=preview[:96],
                    completed_count=len(valid_chunks),
                    failed_at_chunk=manifest.failed_at_chunk,
                    failed_at_chunk_total=manifest.failed_at_chunk_total,
                    chars_consumed=manifest.chars_consumed,
                    total_chars=manifest.total_chars,
                    status=manifest.status,
                    updated_at=manifest.updated_at,
                )
            )

        candidates.sort(key=lambda item: item.updated_at, reverse=True)
        return candidates

    @property
    def manifest(self) -> ChunkManifest:
        return self._manifest

    @property
    def staging_dir(self) -> Path:
        return self._dir

    @property
    def resume_from_chunk(self) -> int:
        """0-indexed chunk number to resume from."""
        return max(self._manifest.chunks_completed) + 1 if self._manifest.chunks_completed else 0

    @property
    def completed_count(self) -> int:
        return len(self._manifest.chunks_completed)

    def chunk_path(self, chunk_idx: int) -> Path:
        return self._dir / f"chunk_{chunk_idx:04d}.mp3"

    def source_text_path(self) -> Path:
        return self._dir / _SOURCE_TEXT_NAME

    def save_source_text(self, text: str) -> None:
        _atomic_write_text(self.source_text_path(), text)

    def save_chunk(self, chunk_idx: int, audio_bytes: bytes) -> None:
        """Write a completed chunk to the staging area atomically."""
        target = self.chunk_path(chunk_idx)
        _atomic_write_bytes(target, audio_bytes)

        if chunk_idx not in self._manifest.chunks_completed:
            self._manifest.chunks_completed.append(chunk_idx)
            self._manifest.chunks_completed.sort()
        self._save_manifest()

    def update_chars_consumed(self, chars: int) -> None:
        bounded = max(0, min(chars, self._manifest.total_chars))
        if bounded > self._manifest.chars_consumed:
            self._manifest.chars_consumed = bounded
            self._save_manifest()

    def mark_failed(
        self,
        failed_at_chunk: int,
        total: Optional[int] = None,
    ) -> None:
        self._manifest.status = "failed"
        self._manifest.failed_at_chunk = failed_at_chunk
        self._manifest.failed_at_chunk_total = total
        self._save_manifest()
        logger.info(
            "Job %s marked as failed at chunk %d/%s — %d chunk(s) preserved in %s",
            self._manifest.job_id,
            failed_at_chunk,
            str(total) if total else "?",
            len(self._manifest.chunks_completed),
            self._dir,
        )

    def mark_cancelled(
        self,
        *,
        preserve_progress: bool = False,
        failed_at_chunk: int | None = None,
        total: int | None = None,
    ) -> None:
        self._manifest.status = "cancelled" if preserve_progress else "cancelled"
        if preserve_progress:
            self._manifest.failed_at_chunk = failed_at_chunk
            self._manifest.failed_at_chunk_total = total
        self._save_manifest()

    def finalize(self, output_path: Path) -> None:
        """
        Concatenate all saved chunk files into *output_path* safely.

        The final MP3 only replaces the destination after every staged chunk
        has been copied into a temporary ``.part`` file successfully.
        """
        completed = sorted(self._manifest.chunks_completed)
        if not completed:
            raise RuntimeError("No completed chunks to finalise — nothing to write.")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_output = output_path.with_suffix(f"{output_path.suffix}.part")
        try:
            with open(tmp_output, "wb") as out:
                for idx in completed:
                    chunk_file = self.chunk_path(idx)
                    if not chunk_file.exists():
                        raise RuntimeError(
                            f"Chunk file missing during finalisation: {chunk_file.name}"
                        )
                    out.write(chunk_file.read_bytes())
            tmp_output.replace(output_path)
        except Exception:
            try:
                tmp_output.unlink(missing_ok=True)
            except Exception:
                pass
            raise

        self._manifest.status = "completed"
        self._save_manifest()
        logger.info(
            "Finalised %d chunks → %s (%d bytes)",
            len(completed),
            output_path,
            output_path.stat().st_size,
        )

    def cleanup(self) -> None:
        """Remove the staging directory after successful completion."""
        try:
            shutil.rmtree(self._dir, ignore_errors=True)
        except Exception as exc:
            logger.warning("Could not clean up staging dir %s: %s", self._dir, exc)

    def _save_manifest(self) -> None:
        self._manifest.updated_at = time.time()
        manifest_path = self._dir / _MANIFEST_NAME
        data = {
            "job_id": self._manifest.job_id,
            "voice": self._manifest.voice,
            "rate": self._manifest.rate,
            "volume": self._manifest.volume,
            "output_path": self._manifest.output_path,
            "text_hash": self._manifest.text_hash,
            "total_chars": self._manifest.total_chars,
            "chars_consumed": self._manifest.chars_consumed,
            "chunks_completed": self._manifest.chunks_completed,
            "status": self._manifest.status,
            "failed_at_chunk": self._manifest.failed_at_chunk,
            "failed_at_chunk_total": self._manifest.failed_at_chunk_total,
            "created_at": self._manifest.created_at,
            "updated_at": self._manifest.updated_at,
        }
        _atomic_write_text(manifest_path, json.dumps(data, indent=2))


def _load_manifest(staging_dir: Path) -> ChunkManifest | None:
    manifest_path = staging_dir / _MANIFEST_NAME
    if not manifest_path.exists():
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        known = {name for name in ChunkManifest.__dataclass_fields__}
        filtered = {key: value for key, value in raw.items() if key in known}
        return ChunkManifest(**filtered)
    except Exception as exc:
        logger.warning("Could not load chunk manifest from %s: %s", staging_dir, exc)
        return None


def _validated_chunks(staging_dir: Path, chunk_indexes: list[int]) -> list[int]:
    valid: list[int] = []
    for idx in sorted(chunk_indexes):
        chunk_file = staging_dir / f"chunk_{idx:04d}.mp3"
        if chunk_file.exists() and chunk_file.stat().st_size > 0:
            valid.append(idx)
            continue
        logger.warning("Missing or empty chunk file %s — truncating resume point", chunk_file)
        break
    return valid


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    try:
        tmp.write_bytes(payload)
        tmp.replace(path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def cleanup_stale_staging(staging_root: Path, max_age_days: int = 7) -> None:
    """
    Remove stale staging directories older than *max_age_days*.

    This keeps abandoned checkpoint data from accumulating forever while still
    leaving recent resumable jobs intact across app restarts.
    """
    if not staging_root.is_dir():
        return

    cutoff = time.time() - max_age_days * 86_400
    for entry in staging_root.iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                logger.info("Removed stale staging dir: %s", entry)
        except Exception as exc:
            logger.warning("Could not check/remove staging dir %s: %s", entry, exc)
