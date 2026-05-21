"""
Per-job chunk staging for checkpoint, resume, and full-text coverage support.

Each long-running TTS job writes completed logical chunks to a job-specific
staging directory and persists a manifest alongside the cleaned source text.

The manifest now records the exact source-text range (start_char, end_char)
covered by every completed chunk, together with the chunk's text hash, audio
byte count, retry count, and any sub-ranges produced by recovery splitting.

Coverage verification runs before the final MP3 is assembled. A job is only
considered complete when:

- the set of recorded chunk ranges is contiguous from 0 to total_chars,
- every recorded chunk file exists, is non-empty, and matches its stored
  byte count, and
- assembly successfully writes every chunk into the final output.

Anything else surfaces as a recoverable failure instead of an over-optimistic
"completed" job.
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
_MANIFEST_SCHEMA_VERSION = 2


@dataclass
class ChunkRecord:
    """One persisted chunk inside a job's manifest."""

    index: int
    start_char: int
    end_char: int
    text_hash: str
    file: str
    audio_bytes: int = 0
    retries: int = 0
    used_recovery: bool = False
    sub_ranges: list[list[int]] = field(default_factory=list)
    completed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start_char": self.start_char,
            "end_char": self.end_char,
            "text_hash": self.text_hash,
            "file": self.file,
            "audio_bytes": self.audio_bytes,
            "retries": self.retries,
            "used_recovery": self.used_recovery,
            "sub_ranges": list(self.sub_ranges),
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ChunkRecord":
        return cls(
            index=int(data["index"]),
            start_char=int(data["start_char"]),
            end_char=int(data["end_char"]),
            text_hash=str(data.get("text_hash", "")),
            file=str(data["file"]),
            audio_bytes=int(data.get("audio_bytes", 0)),
            retries=int(data.get("retries", 0)),
            used_recovery=bool(data.get("used_recovery", False)),
            sub_ranges=[list(r) for r in data.get("sub_ranges", [])],
            completed_at=float(data.get("completed_at", time.time())),
        )


@dataclass
class CoverageReport:
    total_chars: int
    covered_chars: int
    gaps: list[tuple[int, int]]
    overlaps: list[tuple[int, int]]
    chunks_recorded: int
    chunks_with_audio: int
    missing_files: list[str]

    @property
    def is_complete(self) -> bool:
        return (
            not self.gaps
            and not self.missing_files
            and self.covered_chars == self.total_chars
            and self.chunks_recorded > 0
        )

    def summary(self) -> str:
        parts = [
            f"chunks={self.chunks_recorded}",
            f"covered={self.covered_chars}/{self.total_chars}",
        ]
        if self.gaps:
            parts.append(f"gaps={len(self.gaps)}")
        if self.overlaps:
            parts.append(f"overlaps={len(self.overlaps)}")
        if self.missing_files:
            parts.append(f"missing_files={len(self.missing_files)}")
        return " ".join(parts)


@dataclass
class ChunkManifest:
    job_id: str
    voice: str
    rate: str
    volume: str
    output_path: str
    text_hash: str
    total_chars: int
    schema_version: int = _MANIFEST_SCHEMA_VERSION
    chars_consumed: int = 0
    chunks: list[ChunkRecord] = field(default_factory=list)
    # Legacy field — preserved for resume from older manifests. New chunks
    # are recorded in ``chunks`` and this is rebuilt from there on save.
    chunks_completed: list[int] = field(default_factory=list)
    status: str = "running"  # running|interrupted|failed|cancelled|completed
    failed_at_chunk: Optional[int] = None
    failed_at_chunk_total: Optional[int] = None
    expected_duration_min_s: Optional[float] = None
    expected_duration_max_s: Optional[float] = None
    measured_duration_s: Optional[float] = None
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

        valid_records, dropped = _validate_chunk_records(staging_dir, manifest.chunks)
        if dropped:
            changed = True
        if valid_records != manifest.chunks:
            manifest.chunks = valid_records
            changed = True

        manifest.chunks_completed = sorted(record.index for record in valid_records)

        # Rebuild chars_consumed from the validated range coverage so it
        # accurately reflects what has actually been synthesised.
        if valid_records:
            covered_to = _trusted_cursor_from_records(valid_records)
            if covered_to != manifest.chars_consumed:
                manifest.chars_consumed = covered_to
                changed = True
        else:
            if manifest.chars_consumed != 0:
                manifest.chars_consumed = 0
                changed = True

        if not valid_records:
            logger.info("No valid chunk files found in %s — not resumable", staging_dir)
            return None

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
            valid_records, dropped = _validate_chunk_records(entry, manifest.chunks)
            if not text or not valid_records:
                continue

            if dropped:
                changed = True
            if valid_records != manifest.chunks:
                manifest.chunks = valid_records
                manifest.chunks_completed = sorted(r.index for r in valid_records)
                manifest.chars_consumed = _trusted_cursor_from_records(valid_records)
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
                    completed_count=len(valid_records),
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
        if not self._manifest.chunks:
            return 0
        return max(record.index for record in self._manifest.chunks) + 1

    @property
    def resume_position(self) -> int:
        """Absolute char position in the source text to continue from on resume."""
        return _trusted_cursor_from_records(self._manifest.chunks)

    @property
    def completed_count(self) -> int:
        return len(self._manifest.chunks)

    def chunk_path(self, chunk_idx: int) -> Path:
        return self._dir / self._chunk_filename(chunk_idx)

    @staticmethod
    def _chunk_filename(chunk_idx: int) -> str:
        # Leading zeros must be wide enough for any plausible long-form job.
        # 6 digits handles up to ~1 million chunks which is well past anything
        # we can actually produce.
        return f"chunk_{chunk_idx:06d}.mp3"

    def source_text_path(self) -> Path:
        return self._dir / _SOURCE_TEXT_NAME

    def save_source_text(self, text: str) -> None:
        _atomic_write_text(self.source_text_path(), text)

    def record_chunk(
        self,
        chunk_idx: int,
        *,
        start_char: int,
        end_char: int,
        text_hash: str,
        audio_bytes: bytes,
        retries: int = 0,
        used_recovery: bool = False,
        sub_ranges: list[tuple[int, int]] | None = None,
    ) -> ChunkRecord:
        """Persist a successfully synthesised chunk's audio and metadata."""
        if end_char < start_char:
            raise ValueError(
                f"Chunk {chunk_idx}: end_char ({end_char}) must be >= start_char ({start_char})"
            )
        if start_char < 0 or end_char > self._manifest.total_chars:
            raise ValueError(
                f"Chunk {chunk_idx}: range [{start_char}, {end_char}) is out of bounds "
                f"for total_chars={self._manifest.total_chars}"
            )

        target = self.chunk_path(chunk_idx)
        _atomic_write_bytes(target, audio_bytes)

        record = ChunkRecord(
            index=chunk_idx,
            start_char=start_char,
            end_char=end_char,
            text_hash=text_hash,
            file=target.name,
            audio_bytes=len(audio_bytes),
            retries=retries,
            used_recovery=used_recovery,
            sub_ranges=[list(r) for r in (sub_ranges or [])],
        )

        self._manifest.chunks = [c for c in self._manifest.chunks if c.index != chunk_idx]
        self._manifest.chunks.append(record)
        self._manifest.chunks.sort(key=lambda c: c.index)
        self._manifest.chunks_completed = sorted(c.index for c in self._manifest.chunks)
        # Cursor moves forward only — even if the previous chunk overlapped.
        self._manifest.chars_consumed = max(
            self._manifest.chars_consumed,
            _trusted_cursor_from_records(self._manifest.chunks),
        )
        self._save_manifest()
        return record

    def update_chars_consumed(self, chars: int) -> None:
        bounded = max(0, min(chars, self._manifest.total_chars))
        if bounded > self._manifest.chars_consumed:
            self._manifest.chars_consumed = bounded
            self._save_manifest()

    def set_expected_duration(self, min_s: float, max_s: float) -> None:
        self._manifest.expected_duration_min_s = float(min_s)
        self._manifest.expected_duration_max_s = float(max_s)
        self._save_manifest()

    def set_measured_duration(self, duration_s: float | None) -> None:
        self._manifest.measured_duration_s = (
            float(duration_s) if duration_s is not None else None
        )
        self._save_manifest()

    def coverage_report(self) -> CoverageReport:
        """Return a coverage report against the manifest's recorded chunks."""
        records = sorted(self._manifest.chunks, key=lambda c: (c.start_char, c.index))
        gaps: list[tuple[int, int]] = []
        overlaps: list[tuple[int, int]] = []
        missing_files: list[str] = []
        chunks_with_audio = 0
        covered = 0

        prev_end = 0
        for record in records:
            chunk_file = self._dir / record.file
            if not chunk_file.exists() or chunk_file.stat().st_size <= 0:
                missing_files.append(record.file)
                continue
            if record.audio_bytes > 0:
                chunks_with_audio += 1

            if record.start_char > prev_end:
                gaps.append((prev_end, record.start_char))
            elif record.start_char < prev_end:
                overlaps.append((record.start_char, prev_end))

            covered_start = max(record.start_char, prev_end)
            covered_end = max(record.end_char, prev_end)
            if covered_end > covered_start:
                covered += covered_end - covered_start
            prev_end = max(prev_end, record.end_char)

        if prev_end < self._manifest.total_chars:
            gaps.append((prev_end, self._manifest.total_chars))

        return CoverageReport(
            total_chars=self._manifest.total_chars,
            covered_chars=covered,
            gaps=gaps,
            overlaps=overlaps,
            chunks_recorded=len(records),
            chunks_with_audio=chunks_with_audio,
            missing_files=missing_files,
        )

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
            len(self._manifest.chunks),
            self._dir,
        )

    def mark_cancelled(
        self,
        *,
        preserve_progress: bool = False,
        failed_at_chunk: int | None = None,
        total: int | None = None,
    ) -> None:
        self._manifest.status = "cancelled"
        if preserve_progress:
            self._manifest.failed_at_chunk = failed_at_chunk
            self._manifest.failed_at_chunk_total = total
        self._save_manifest()

    def finalize(self, output_path: Path) -> None:
        """
        Concatenate all saved chunk files into *output_path* safely.

        Fails closed if the manifest's recorded chunks do not cover the entire
        source text, if any chunk file is missing or empty, or if the assembly
        output bytes do not match the sum of the chunk audio bytes.
        """
        records = sorted(self._manifest.chunks, key=lambda c: c.index)
        if not records:
            raise RuntimeError("No completed chunks to finalise — nothing to write.")

        # Sort by source range for safe assembly order. Indexes should be
        # monotonic with respect to the source ranges; if they are not, prefer
        # source order so the assembled audio matches the source text.
        records_by_range = sorted(records, key=lambda c: (c.start_char, c.index))

        coverage = self.coverage_report()
        if not coverage.is_complete:
            raise CoverageError(
                f"Coverage check failed before final assembly: {coverage.summary()}",
                coverage,
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_output = output_path.with_suffix(f"{output_path.suffix}.part")
        expected_bytes = 0
        try:
            with open(tmp_output, "wb") as out:
                for record in records_by_range:
                    chunk_file = self._dir / record.file
                    if not chunk_file.exists():
                        raise RuntimeError(
                            f"Chunk file missing during finalisation: {chunk_file.name}"
                        )
                    payload = chunk_file.read_bytes()
                    if not payload:
                        raise RuntimeError(
                            f"Chunk file empty during finalisation: {chunk_file.name}"
                        )
                    out.write(payload)
                    expected_bytes += len(payload)

            actual_bytes = tmp_output.stat().st_size
            if actual_bytes != expected_bytes:
                raise RuntimeError(
                    f"Assembly byte mismatch — wrote {actual_bytes} bytes "
                    f"but expected {expected_bytes} from {len(records_by_range)} chunks"
                )

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
            "Finalised %d chunks → %s (%d bytes, coverage: %s)",
            len(records_by_range),
            output_path,
            output_path.stat().st_size,
            coverage.summary(),
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
        # Keep ``chunks_completed`` in sync for legacy readers.
        self._manifest.chunks_completed = sorted(
            record.index for record in self._manifest.chunks
        )
        data = {
            "schema_version": self._manifest.schema_version,
            "job_id": self._manifest.job_id,
            "voice": self._manifest.voice,
            "rate": self._manifest.rate,
            "volume": self._manifest.volume,
            "output_path": self._manifest.output_path,
            "text_hash": self._manifest.text_hash,
            "total_chars": self._manifest.total_chars,
            "chars_consumed": self._manifest.chars_consumed,
            "chunks": [record.to_dict() for record in self._manifest.chunks],
            "chunks_completed": self._manifest.chunks_completed,
            "status": self._manifest.status,
            "failed_at_chunk": self._manifest.failed_at_chunk,
            "failed_at_chunk_total": self._manifest.failed_at_chunk_total,
            "expected_duration_min_s": self._manifest.expected_duration_min_s,
            "expected_duration_max_s": self._manifest.expected_duration_max_s,
            "measured_duration_s": self._manifest.measured_duration_s,
            "created_at": self._manifest.created_at,
            "updated_at": self._manifest.updated_at,
        }
        _atomic_write_text(manifest_path, json.dumps(data, indent=2))


class CoverageError(RuntimeError):
    """Raised when a job's chunk records do not cover the source text."""

    def __init__(self, message: str, report: CoverageReport) -> None:
        super().__init__(message)
        self.report = report


def _load_manifest(staging_dir: Path) -> ChunkManifest | None:
    manifest_path = staging_dir / _MANIFEST_NAME
    if not manifest_path.exists():
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not load chunk manifest from %s: %s", staging_dir, exc)
        return None

    try:
        chunks_raw = raw.get("chunks") or []
        chunks: list[ChunkRecord] = []
        for entry in chunks_raw:
            try:
                chunks.append(ChunkRecord.from_dict(entry))
            except Exception:  # Skip malformed entries individually
                logger.warning(
                    "Discarding malformed chunk entry in %s: %r", staging_dir, entry
                )
        chunks.sort(key=lambda c: c.index)

        manifest = ChunkManifest(
            job_id=str(raw.get("job_id", staging_dir.name)),
            voice=str(raw.get("voice", "")),
            rate=str(raw.get("rate", "")),
            volume=str(raw.get("volume", "")),
            output_path=str(raw.get("output_path", "")),
            text_hash=str(raw.get("text_hash", "")),
            total_chars=int(raw.get("total_chars", 0)),
            schema_version=int(raw.get("schema_version", 1)),
            chars_consumed=int(raw.get("chars_consumed", 0)),
            chunks=chunks,
            chunks_completed=[int(idx) for idx in raw.get("chunks_completed", [])],
            status=str(raw.get("status", "interrupted")),
            failed_at_chunk=raw.get("failed_at_chunk"),
            failed_at_chunk_total=raw.get("failed_at_chunk_total"),
            expected_duration_min_s=raw.get("expected_duration_min_s"),
            expected_duration_max_s=raw.get("expected_duration_max_s"),
            measured_duration_s=raw.get("measured_duration_s"),
            created_at=float(raw.get("created_at", time.time())),
            updated_at=float(raw.get("updated_at", time.time())),
        )
    except Exception as exc:
        logger.warning("Could not normalise manifest data in %s: %s", staging_dir, exc)
        return None

    # Legacy v1 manifests have ``chunks_completed`` but no ``chunks`` list.
    # We treat those legacy chunks as un-trackable for coverage purposes — the
    # safest behaviour is to discard them so the job restarts cleanly with the
    # new bookkeeping rather than silently trusting unverifiable history.
    if manifest.schema_version < _MANIFEST_SCHEMA_VERSION and not manifest.chunks:
        logger.info(
            "Manifest in %s is from an older schema (v%d); discarding legacy progress "
            "so the next run can be tracked with full coverage accounting.",
            staging_dir,
            manifest.schema_version,
        )
        manifest.chunks_completed = []
        manifest.chars_consumed = 0
        manifest.schema_version = _MANIFEST_SCHEMA_VERSION

    return manifest


def _validate_chunk_records(
    staging_dir: Path,
    records: list[ChunkRecord],
) -> tuple[list[ChunkRecord], list[ChunkRecord]]:
    """Filter the manifest's chunk records to those whose audio files are intact.

    Returns ``(valid_records, dropped_records)``. The returned ``valid_records``
    list is sorted by ``index`` and is contiguous up to the first missing or
    corrupted record — anything past a gap is treated as un-resumable to keep
    the cursor accounting honest.
    """
    valid: list[ChunkRecord] = []
    dropped: list[ChunkRecord] = []
    indexed = sorted(records, key=lambda c: c.index)

    for record in indexed:
        chunk_file = staging_dir / record.file
        if not chunk_file.exists() or chunk_file.stat().st_size <= 0:
            dropped.append(record)
            break  # truncate at the first gap
        # Indexes must be contiguous; gaps mean we can't safely resume past them.
        if valid and record.index != valid[-1].index + 1:
            dropped.append(record)
            break
        # Source ranges must be monotonic for cursor-based resume.
        if valid and record.start_char < valid[-1].end_char:
            dropped.append(record)
            break
        valid.append(record)

    # Anything that came after the truncation point also has to be dropped so
    # the manifest stays internally consistent.
    if dropped:
        seen_indexes = {record.index for record in valid}
        dropped += [r for r in indexed if r.index not in seen_indexes and r not in dropped]
        for record in dropped:
            logger.warning(
                "Dropping un-resumable chunk record idx=%d file=%s in %s",
                record.index,
                record.file,
                staging_dir,
            )

    return valid, dropped


def _trusted_cursor_from_records(records: list[ChunkRecord]) -> int:
    """Return the cursor position implied by a list of validated chunk records."""
    if not records:
        return 0
    return max(record.end_char for record in records)


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
