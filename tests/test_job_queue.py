from app.workers.job_queue import JobQueue


def test_job_queue_rejects_duplicate_output_paths(tmp_path, monkeypatch):
    def fake_start_job(self, item):
        self._running[item.id] = (item, None)

    monkeypatch.setattr(JobQueue, "_start_job", fake_start_job)

    queue = JobQueue()
    output = str(tmp_path / "book.mp3")

    queue.submit(
        text="hello",
        voice="en-US-AvaNeural",
        voice_display="Ava · English (US)",
        rate="+0%",
        volume="+0%",
        output_path=output,
    )

    try:
        queue.submit(
            text="hello again",
            voice="en-US-AvaNeural",
            voice_display="Ava · English (US)",
            rate="+0%",
            volume="+0%",
            output_path=output,
        )
    except ValueError as exc:
        assert "already running or queued" in str(exc)
    else:
        raise AssertionError("Expected duplicate output path submission to fail")
