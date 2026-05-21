import json

import pytest

from app.workers.chunk_store import ChunkStore, CoverageError


def _record(store: ChunkStore, index: int, *, start: int, end: int, payload: bytes = b"audio") -> None:
    store.record_chunk(
        index,
        start_char=start,
        end_char=end,
        text_hash=f"hash{index}",
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
