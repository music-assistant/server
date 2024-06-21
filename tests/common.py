"""Common test helpers for Music Assistant tests."""

import asyncio
import contextlib
import pathlib
from collections.abc import AsyncGenerator

import aiofiles.os

from music_assistant.common.models.enums import EventType
from music_assistant.common.models.event import MassEvent
from music_assistant.server.server import MusicAssistant


def _get_fixture_folder(provider: str | None = None) -> pathlib.Path:
    tests_base = pathlib.Path(__file__).parent
    if provider:
        return tests_base / "server" / "providers" / provider / "fixtures"
    return tests_base / "fixtures"


async def get_fixtures_dir(
    subdir: str, provider: str | None = None
) -> AsyncGenerator[tuple[str, bytes], None]:
    """Yield the contents of every fixture in a fixtures folder."""
    dir_path = _get_fixture_folder(provider) / subdir
    for file in await aiofiles.os.listdir(dir_path):
        async with aiofiles.open(dir_path / file, "rb") as fp:
            yield (file, await fp.read())


@contextlib.asynccontextmanager
async def wait_for_sync_completion(mass: MusicAssistant) -> AsyncGenerator[None, None]:
    """Wait for a sync to finish."""
    flag = asyncio.Event()

    def _event(event: MassEvent) -> None:
        if not event.data:
            flag.set()

    release_cb = mass.subscribe(_event, EventType.SYNC_TASKS_UPDATED)

    try:
        yield
    finally:
        await flag.wait()
        release_cb()
