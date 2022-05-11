"""Model for a Background Job."""
from dataclasses import dataclass
from time import time
from typing import Coroutine

from music_assistant.models.enums import JobStatus


@dataclass
class BackgroundJob:
    """Description of a background job/task."""

    id: str
    coro: Coroutine
    name: str
    timestamp: float = time()
    status: JobStatus = JobStatus.PENDING

    def to_dict(self):
        """Return serializable dict from object."""
        return {
            "id": self.id,
            "name": self.name,
            "timestamp": self.status.value,
            "status": self.status.value,
        }
