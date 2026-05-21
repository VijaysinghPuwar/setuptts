import json
import hashlib

import pytest

from app.workers.chunk_store import ChunkRecord, ChunkStore, CoverageError


def _record(store: ChunkStore, index: int, *, start: int, end: int, payload: bytes = b"audio") -> None:
    source_text = store.source_text_path().read_text(encoding="utf-8")
    store.record_chunk(
        index,
        start_char=start,
        end_char=end,
        text_hash=hashlib.sha256(
            source_text[start:end].encode("utf-8", errors="replace")
        ).hexdigest()[:16],
        audio_bytes=payload,
    )


def test_chunk_store_lists_and_resumes_preserved_jobs(tmp_path):
    text = "alpha beta gamma delta epsilon " * 20
    store = ChunkStore.create(
        tmp_path,
        "job123",
        voice="en-US-AvaNeural",
        rate="+0%",
        volume="+0%",
        output_path=str(tmp_path / "book.mp3"),
        text=text,
    )
    _record(store, 0, start=0, end=120, payload=b"chunk-one")
    store.update_chars_consumed(120)
    store.mark_failed(2, 4)

    candidates = ChunkStore.list_resume_candidates(tmp_path)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.job_id == "job123"
    assert candidate.text == text
    assert candidate.completed_count == 1
    assert candidate.failed_at_chunk == 2

    resumed = ChunkStore.try_resume(candidate.staging_dir, text, "en-US-AvaNeural")
    assert resumed is not None
    assert resumed.resume_from_chunk == 1
    assert resumed.resume_position == 120
    assert resumed.manifest.chars_consumed == 120

    manifest = json.loads((candidate.staging_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["schema_version"] >= 2
    assert manifest["chunks"][0]["start_char"] == 0
    assert manifest["chunks"][0]["end_char"] == 120


def test_coverage_report_detects_gaps(tmp_path):
    text = "a" * 300
    store = ChunkStore.create(
        tmp_path,
        "gaps",
        voice="en-US-AvaNeural",
        rate="+0%",
        volume="+0%",
        output_path=str(tmp_path / "x.mp3"),
        text=text,
    )
    _record(store, 0, start=0, end=100)
    _record(store, 1, start=150, end=300)

    report = store.coverage_report()
    assert not report.is_complete
    assert report.gaps == [(100, 150)]


def test_coverage_report_detects_overlaps(tmp_path):
    text = "b" * 300
    store = ChunkStore.create(
        tmp_path,
        "overlap",
        voice="en-US-AvaNeural",
        rate="+0%",
        volume="+0%",
        output_path=str(tmp_path / "x.mp3"),
        text=text,
    )
    _record(store, 0, start=0, end=160)
    _record(store, 1, start=120, end=300)

    report = store.coverage_report()
    assert not report.is_complete
    assert report.overlaps == [(120, 160)]
    with pytest.raises(CoverageError):
        store.finalize(tmp_path / "x.mp3")


def test_coverage_report_detects_duplicate_chunk_records(tmp_path):
    text = "c" * 200
    store = ChunkStore.create(
        tmp_path,
        "duplicate",
        voice="en-US-AvaNeural",
        rate="+0%",
        volume="+0%",
        output_path=str(tmp_path / "x.mp3"),
        text=text,
    )
    _record(store, 0, start=0, end=100, payload=b"AAA")
    _record(store, 1, start=100, end=200, payload=b"BBB")

    duplicate_path = store.staging_dir / "chunk_duplicate.mp3"
    duplicate_path.write_bytes(b"CCC")
    store.manifest.chunks.append(
        ChunkRecord(
            index=1,
            start_char=100,
            end_char=200,
            text_hash=hashlib.sha256(text[100:200].encode("utf-8")).hexdigest()[:16],
            file=duplicate_path.name,
            audio_bytes=3,
        )
    )

    report = store.coverage_report()
    assert not report.is_complete
    assert report.duplicate_indexes == [1]
    with pytest.raises(CoverageError):
        store.finalize(tmp_path / "x.mp3")


def test_coverage_report_detects_corrupt_chunk_size(tmp_path):
    text = "d" * 200
    store = ChunkStore.create(
        tmp_path,
        "corrupt-size",
        voice="en-US-AvaNeural",
        rate="+0%",
        volume="+0%",
        output_path=str(tmp_path / "x.mp3"),
        text=text,
    )
    _record(store, 0, start=0, end=100, payload=b"AAAA")
    _record(store, 1, start=100, end=200, payload=b"BBBB")
    (store.staging_dir / "chunk_000001.mp3").write_bytes(b"B")

    report = store.coverage_report()
    assert not report.is_complete
    assert report.invalid_files == ["chunk_000001.mp3"]
    with pytest.raises(CoverageError):
        store.finalize(tmp_path / "x.mp3")


def test_coverage_report_detects_text_hash_mismatch(tmp_path):
    text = "e" * 200
    store = ChunkStore.create(
        tmp_path,
        "hash-mismatch",
        voice="en-US-AvaNeural",
        rate="+0%",
        volume="+0%",
        output_path=str(tmp_path / "x.mp3"),
        text=text,
    )
    store.record_chunk(
        0,
        start_char=0,
        end_char=200,
        text_hash="wronghash",
        audio_bytes=b"AAAA",
    )

    report = store.coverage_report()
    assert not report.is_complete
    assert report.text_hash_mismatches == [0]
    with pytest.raises(CoverageError):
        store.finalize(tmp_path / "x.mp3")


def test_finalize_refuses_to_assemble_when_coverage_is_incomplete(tmp_path):
    text = "a" * 500
    store = ChunkStore.create(
        tmp_path,
        "incomplete",
        voice="en-US-AvaNeural",
        rate="+0%",
        volume="+0%",
        output_path=str(tmp_path / "out.mp3"),
        text=text,
    )
    _record(store, 0, start=0, end=200)
    _record(store, 1, start=200, end=400)  # missing 400..500

    with pytest.raises(CoverageError):
        store.finalize(tmp_path / "out.mp3")
    assert not (tmp_path / "out.mp3").exists()


def test_finalize_refuses_missing_middle_chunk_file(tmp_path):
    text = "m" * 300
    store = ChunkStore.create(
        tmp_path,
        "missing-middle",
        voice="en-US-AvaNeural",
        rate="+0%",
        volume="+0%",
        output_path=str(tmp_path / "out.mp3"),
        text=text,
    )
    _record(store, 0, start=0, end=100, payload=b"AAA")
    _record(store, 1, start=100, end=200, payload=b"BBB")
    _record(store, 2, start=200, end=300, payload=b"CCC")
    (store.staging_dir / "chunk_000001.mp3").unlink()

    with pytest.raises(CoverageError):
        store.finalize(tmp_path / "out.mp3")


def test_finalize_concatenates_in_source_order(tmp_path):
    text = "abc" * 100  # 300 chars
    store = ChunkStore.create(
        tmp_path,
        "ordered",
        voice="en-US-AvaNeural",
        rate="+0%",
        volume="+0%",
        output_path=str(tmp_path / "out.mp3"),
        text=text,
    )
    _record(store, 0, start=0, end=100, payload=b"AAA")
    _record(store, 1, start=100, end=200, payload=b"BBB")
    _record(store, 2, start=200, end=300, payload=b"CCC")

    store.finalize(tmp_path / "out.mp3")
    assert (tmp_path / "out.mp3").read_bytes() == b"AAABBBCCC"


def test_record_chunk_rejects_out_of_bounds_ranges(tmp_path):
    text = "x" * 100
    store = ChunkStore.create(
        tmp_path,
        "bounds",
        voice="v",
        rate="+0%",
        volume="+0%",
        output_path=str(tmp_path / "x.mp3"),
        text=text,
    )
    with pytest.raises(ValueError):
        store.record_chunk(0, start_char=0, end_char=200, text_hash="h", audio_bytes=b"a")
    with pytest.raises(ValueError):
        store.record_chunk(0, start_char=50, end_char=10, text_hash="h", audio_bytes=b"a")
    with pytest.raises(ValueError):
        store.record_chunk(0, start_char=10, end_char=10, text_hash="h", audio_bytes=b"a")
    with pytest.raises(ValueError):
        store.record_chunk(0, start_char=0, end_char=10, text_hash="h", audio_bytes=b"")


def test_resume_drops_records_after_missing_file(tmp_path):
    text = "abc" * 100
    store = ChunkStore.create(
        tmp_path,
        "drop",
        voice="v",
        rate="+0%",
        volume="+0%",
        output_path=str(tmp_path / "x.mp3"),
        text=text,
    )
    _record(store, 0, start=0, end=100, payload=b"AAA")
    _record(store, 1, start=100, end=200, payload=b"BBB")
    _record(store, 2, start=200, end=300, payload=b"CCC")
    store.mark_failed(3, 3)

    # Simulate a corrupted chunk 1 by deleting it.
    (store.staging_dir / "chunk_000001.mp3").unlink()

    resumed = ChunkStore.try_resume(store.staging_dir, text, "v")
    assert resumed is not None
    # Only chunk 0 should survive validation; chunk 2 must be dropped because
    # we don't trust a resume that skips a missing range.
    assert resumed.completed_count == 1
    assert resumed.resume_position == 100


def test_resume_truncates_at_first_source_range_gap(tmp_path):
    text = "abc" * 100
    store = ChunkStore.create(
        tmp_path,
        "range-gap",
        voice="v",
        rate="+0%",
        volume="+0%",
        output_path=str(tmp_path / "x.mp3"),
        text=text,
    )
    _record(store, 0, start=0, end=100, payload=b"AAA")
    _record(store, 1, start=150, end=250, payload=b"BBB")
    store.mark_failed(2, 3)

    resumed = ChunkStore.try_resume(store.staging_dir, text, "v")
    assert resumed is not None
    assert resumed.completed_count == 1
    assert resumed.resume_position == 100


def test_resume_rejects_corrupt_chunk_file(tmp_path):
    text = "abc" * 100
    store = ChunkStore.create(
        tmp_path,
        "resume-corrupt",
        voice="v",
        rate="+0%",
        volume="+0%",
        output_path=str(tmp_path / "x.mp3"),
        text=text,
    )
    _record(store, 0, start=0, end=100, payload=b"AAA")
    _record(store, 1, start=100, end=200, payload=b"BBB")
    (store.staging_dir / "chunk_000001.mp3").write_bytes(b"B")
    store.mark_failed(2, 3)

    resumed = ChunkStore.try_resume(store.staging_dir, text, "v")
    assert resumed is not None
    assert resumed.completed_count == 1
    assert resumed.resume_position == 100


def test_try_resume_ignores_partially_written_manifest(tmp_path):
    staging_dir = tmp_path / "partial"
    staging_dir.mkdir()
    (staging_dir / "manifest.json").write_text("{not-json", encoding="utf-8")
    (staging_dir / "source.txt").write_text("abc", encoding="utf-8")
    (staging_dir / "chunk_000000.mp3").write_bytes(b"audio")

    assert ChunkStore.try_resume(staging_dir, "abc", "v") is None


def test_resume_after_crash_with_complete_running_manifest_can_finalize(tmp_path):
    text = "xyz" * 100
    store = ChunkStore.create(
        tmp_path,
        "complete-running",
        voice="v",
        rate="+0%",
        volume="+0%",
        output_path=str(tmp_path / "x.mp3"),
        text=text,
    )
    _record(store, 0, start=0, end=150, payload=b"AAA")
    _record(store, 1, start=150, end=300, payload=b"BBB")
    # Simulate process exit after all chunks were saved but before final
    # assembly/status completion.
    store.manifest.status = "running"
    store._save_manifest()

    resumed = ChunkStore.try_resume(store.staging_dir, text, "v")
    assert resumed is not None
    assert resumed.resume_position == len(text)
    assert resumed.manifest.status == "interrupted"

    resumed.finalize(tmp_path / "x.mp3")
    assert (tmp_path / "x.mp3").read_bytes() == b"AAABBB"


def test_recovery_sub_ranges_must_exactly_cover_parent_range(tmp_path):
    text = "r" * 240
    store = ChunkStore.create(
        tmp_path,
        "bad-subranges",
        voice="v",
        rate="+0%",
        volume="+0%",
        output_path=str(tmp_path / "x.mp3"),
        text=text,
    )

    with pytest.raises(ValueError):
        store.record_chunk(
            0,
            start_char=0,
            end_char=240,
            text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            audio_bytes=b"audio",
            used_recovery=True,
            sub_ranges=[(0, 120), (121, 240)],
        )
