"""Model for a Background Job."""
import asyncio
from dataclasses import dataclass, field
from time import time
from typing import Any, Coroutine

from music_assistant.models.enums import JobStatus


@dataclass
class BackgroundJob:
    """Description of a background job/task."""

    id: str
    coro: Coroutine
    name: str
    timestamp: float = time()
    status: JobStatus = JobStatus.PENDING
    result: Any = None
    _evt: asyncio.Event = field(init=False, default_factory=asyncio.Event)

    def to_dict(self):
        """Return serializable dict from object."""
        return {
            "id": self.id,
            "name": self.name,
            "timestamp": self.status.value,
            "status": self.status.value,
        }

    async def wait(self) -> None:
        """Wait for the job to complete."""
        await self._evt.wait()

    def done(self) -> None:
        """Mark job as done."""
        self._evt.set()
