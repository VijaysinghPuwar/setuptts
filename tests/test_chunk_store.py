import json

from app.workers.chunk_store import ChunkStore


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
    store.save_chunk(0, b"chunk-one")
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
    assert resumed.manifest.chars_consumed == 120

    manifest = json.loads((candidate.staging_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
