"""
Hermetic end-to-end fixtures backed by the fake `test` + demo player providers.

These boot a real MusicAssistant with only the fake music (`test`) and fake player
(`_demo_player_provider`) providers, with all real network discovery disabled, so
end-to-end behaviour (grouping, queue, current-media propagation) can be exercised
without any hardware or LAN access.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from typing import cast
from unittest.mock import AsyncMock, MagicMock, NonCallableMagicMock, patch

import pytest
from zeroconf.asyncio import AsyncZeroconf

from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.player import Player
from tests.common import suppress_auto_loaded_providers, use_ephemeral_server_ports

NUM_DEMO_PLAYERS = 3


async def wait_for(predicate: Callable[[], bool], timeout: float = 25.0) -> bool:
    """Poll ``predicate`` until it is truthy or ``timeout`` elapses; return the final value."""
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(0.25)
        elapsed += 0.25
    return predicate()


def demo_players(mass: MusicAssistant) -> list[Player]:
    """Return the registered demo players, sorted by id."""
    return sorted(
        (p for p in mass.players if p.player_id.startswith("demo_")),
        key=lambda p: p.player_id,
    )


async def group_players(mass: MusicAssistant, leader: Player, members: list[Player]) -> None:
    """
    Form a sync group of demo players under ``leader``.

    :param leader: The player that becomes the sync leader.
    :param members: The players to join to the leader.
    """
    # refresh every player's state so can_group_with re-expands now that all players
    # are registered (the cross-player refresh is otherwise debounced)
    for player in [leader, *members]:
        player.update_state(force_update=True)
    await asyncio.sleep(0.5)
    for member in members:
        await mass.players.cmd_group(member.player_id, leader.player_id)
    assert await wait_for(lambda: len(leader.state.group_members) >= 1 + len(members)), (
        f"group did not form: {leader.state.group_members}"
    )


async def play_test_track(mass: MusicAssistant, queue_id: str, track_id: str = "0_0_0") -> None:
    """Play a fake `test`-provider track (which carries artwork) on the given queue."""
    test_prov = cast("MusicProvider", mass.get_provider("test"))
    assert test_prov is not None
    track = await test_prov.get_track(track_id)
    await mass.player_queues.play_media(queue_id, track)


def _create_mock_zeroconf() -> MagicMock:
    """Create a mock AsyncZeroconf that prevents real mDNS network I/O."""
    mock_zc = MagicMock(spec=AsyncZeroconf)
    mock_inner_zc = NonCallableMagicMock()
    mock_inner_zc.cache = NonCallableMagicMock()
    mock_inner_zc.cache.cache = {}  # empty cache - no discovered services
    mock_zc.zeroconf = mock_inner_zc
    mock_zc.async_register_service = AsyncMock()
    mock_zc.async_update_service = AsyncMock()
    mock_zc.async_unregister_service = AsyncMock()
    mock_zc.async_close = AsyncMock()
    return mock_zc


@pytest.fixture
async def e2e_mass(tmp_path: pathlib.Path) -> AsyncGenerator[MusicAssistant]:
    """
    Boot a hermetic MusicAssistant with only the fake `test` + demo player providers.

    No real network discovery happens: mDNS (zeroconf) and SSDP are mocked, the
    default device providers (dlna/sonos/...) are suppressed and so is local_audio,
    which would otherwise register the host's sound devices as players. The `test`
    music provider and three grouped-capable demo players are configured and ready.
    """
    storage_path = tmp_path / "data"
    cache_path = tmp_path / "cache"
    storage_path.mkdir(parents=True)
    cache_path.mkdir(parents=True)
    logging.getLogger("aiosqlite").level = logging.INFO

    mass_instance = MusicAssistant(str(storage_path), str(cache_path))
    # load the `_`-prefixed demo player provider (gated on dev mode)
    mass_instance.dev_mode = True

    with (
        use_ephemeral_server_ports(),
        patch(
            "music_assistant.controllers.discovery.controller.AsyncZeroconf",
            return_value=_create_mock_zeroconf(),
        ),
        patch(
            "music_assistant.controllers.discovery.controller.AsyncServiceBrowser",
            return_value=NonCallableMagicMock(),
        ),
        patch(
            "music_assistant.controllers.streams.controller.check_ffmpeg_version",
            new=AsyncMock(),
        ),
        # hermetic: no real SSDP search
        patch(
            "music_assistant.controllers.discovery.controller.async_upnp_search",
            new=AsyncMock(),
        ),
        # hermetic: no auto-loaded device providers and no host-audio bridging
        suppress_auto_loaded_providers(),
    ):
        try:
            await mass_instance.start()
            # configure the fake music + player providers
            await mass_instance.config._create_provider_instance("test", {})
            await mass_instance.config._create_provider_instance(
                "_demo_player_provider", {"number_of_players": NUM_DEMO_PLAYERS}
            )
            await wait_for(lambda: len(demo_players(mass_instance)) >= NUM_DEMO_PLAYERS)
            yield mass_instance
        finally:
            # also stop after a failed boot, or the half-started server's open database
            # connections keep their non-daemon threads alive and hang the interpreter
            # at exit (see the same note in tests/conftest.py)
            with suppress(AttributeError):
                await mass_instance.stop()
