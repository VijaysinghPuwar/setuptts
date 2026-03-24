"""Job data model for history tracking."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class JobStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    id: int | None
    text_preview: str           # first 80 chars of input text
    voice: str                  # ShortName e.g. "en-US-AvaNeural"
    rate: str                   # e.g. "+5%"
    output_path: str            # absolute path to the generated MP3
    created_at: datetime = field(default_factory=datetime.now)
    duration_seconds: float = 0.0
    status: JobStatus = JobStatus.COMPLETED
    error_message: str = ""

    @property
    def output_filename(self) -> str:
        from pathlib import Path
        return Path(self.output_path).name

    @property
    def created_at_display(self) -> str:
        """Human-friendly relative time."""
        delta = datetime.now() - self.created_at
        if delta.total_seconds() < 60:
            return "Just now"
        if delta.total_seconds() < 3600:
            mins = int(delta.total_seconds() // 60)
            return f"{mins}m ago"
        if delta.days == 0:
            hours = int(delta.total_seconds() // 3600)
            return f"{hours}h ago"
        if delta.days == 1:
            return "Yesterday"
        return self.created_at.strftime("%b %d, %Y")
