"""Tests for setting up and attaching a discovered Chromecast."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pychromecast.const import CAST_TYPE_AUDIO

from music_assistant.providers.chromecast.helpers import ChromecastInfo
from music_assistant.providers.chromecast.provider import ChromecastProvider

if TYPE_CHECKING:
    from music_assistant.models.player_protocol import PlayerProtocol


@pytest.mark.parametrize("attaches", [True, False], ids=["attached", "skipped"])
async def test_a_discovered_device_connects_when_it_attaches(attaches: bool) -> None:
    """A device connects and gets its bridge at its attach; one the attach skips opens nothing."""
    cast_info = ChromecastInfo(
        services=set(),
        uuid=uuid4(),
        model_name="Test Model",
        friendly_name="Speaker A",
        host="192.168.1.71",
        port=8009,
        cast_type=CAST_TYPE_AUDIO,
        manufacturer="Google Inc.",
    )
    protocol_id = str(cast_info.uuid)
    provider = ChromecastProvider.__new__(ChromecastProvider)
    provider.mass = MagicMock()
    provider.mass.create_task = MagicMock(side_effect=lambda coro, **_: asyncio.ensure_future(coro))
    provider.config = MagicMock()
    provider.logger = MagicMock()
    provider.mz_mgr = MagicMock()
    provider._pending_discoveries = {protocol_id}
    provider.bridge_manager = MagicMock(evaluate_bridge=AsyncMock())

    async def _attach(protocol: PlayerProtocol) -> None:
        if attaches:
            await protocol.on_attach(MagicMock())

    provider.mass.players.register = AsyncMock(side_effect=_attach)

    with patch("pychromecast.get_chromecast_from_cast_info") as connect:
        await provider._create_and_register_player(protocol_id, cast_info)
        await asyncio.sleep(0)

    provider.mass.players.register.assert_awaited_once()
    assert connect.call_count == (1 if attaches else 0)
    assert connect.return_value.start.call_count == (1 if attaches else 0)
    assert provider.bridge_manager.evaluate_bridge.await_count == (1 if attaches else 0)
    assert protocol_id not in provider._pending_discoveries
