"""Tests for the MusicBrainz API client's cache bypass during a refresh."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from music_assistant.providers.musicbrainz.api_client import (
    MusicBrainzAPIClient,
    refresh_musicbrainz,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

ENDPOINT = "work/3f62d0d8-8ccd-31ab-bfca-768a1ae5708d?inc=aliases"


@pytest.fixture(name="mass")
async def mass_fixture(mass_minimal: MusicAssistant) -> MusicAssistant:
    """Return a minimal instance with its cache database set up."""
    await mass_minimal.cache.setup(await mass_minimal.config.get_core_config("cache"))
    return mass_minimal


async def _settle() -> None:
    """Let background tasks that store answers in the cache database finish."""
    pending = asyncio.all_tasks() - {asyncio.current_task()}
    await asyncio.wait(pending, timeout=2)


async def test_lookups_are_cached_outside_a_refresh(mass: MusicAssistant) -> None:
    """Outside a refresh a repeated lookup is answered from the cache."""
    client = MusicBrainzAPIClient(mass)
    with patch.object(client, "_request", AsyncMock(return_value={"id": "1"})) as request:
        assert await client.get_data(ENDPOINT) == {"id": "1"}
        await _settle()
        assert await client.get_data(ENDPOINT) == {"id": "1"}

    assert request.await_count == 1


async def test_refresh_reads_past_the_cache(mass: MusicAssistant) -> None:
    """Inside refresh_musicbrainz a cached lookup is fetched again."""
    client = MusicBrainzAPIClient(mass)
    with patch.object(client, "_request", AsyncMock(return_value={"title": "old"})):
        await client.get_data(ENDPOINT)
        await _settle()

    with patch.object(client, "_request", AsyncMock(return_value={"title": "new"})) as request:
        async with refresh_musicbrainz():
            result = await client.get_data(ENDPOINT)
        await _settle()
        after = await client.get_data(ENDPOINT)

    assert result == {"title": "new"}
    assert after == {"title": "new"}
    assert request.await_count == 1
