"""
Test that a Sonos speaker reboot triggers a forced reconnect.

Covers issue #5792: after a speaker restart (e.g. firmware update) the
websocket to the player can die silently while MA still considers the
player connected, so playback events never arrive and the queue freezes.
The mDNS announcement of the rebooted speaker carries an increased
``bootseq``, which must trigger a forced reconnect.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from zeroconf import ServiceStateChange

from music_assistant.providers.sonos.player import SonosPlayer
from music_assistant.providers.sonos.provider import SonosPlayerProvider

PLAYER_ID = "RINCON_TEST123"
PLAYER_IP = "192.168.1.50"


def _mdns_info(bootseq: str) -> MagicMock:
    info = MagicMock()
    info.decoded_properties = {"uuid": PLAYER_ID, "bootseq": bootseq}
    return info


def _provider_with_player(*, connected: bool, last_bootseq: int | None) -> MagicMock:
    provider = MagicMock()
    sonos_player = MagicMock(spec=SonosPlayer)
    sonos_player.connected = connected
    sonos_player.last_bootseq = last_bootseq
    sonos_player.client = MagicMock()
    sonos_player.device_info.ip_address = PLAYER_IP
    provider.mass.players.get_player.return_value = sonos_player
    provider._sonos_player = sonos_player
    return provider


async def _handle_mdns(provider: MagicMock, bootseq: str) -> None:
    with patch(
        "music_assistant.providers.sonos.provider.get_primary_ip_address",
        return_value=PLAYER_IP,
    ):
        await SonosPlayerProvider.on_mdns_service_state_change(
            provider, "test._sonos._tcp.local.", ServiceStateChange.Updated, _mdns_info(bootseq)
        )


@pytest.mark.asyncio
async def test_bootseq_increase_forces_reconnect() -> None:
    """A bootseq increase while connected forces a reconnect of the stale connection."""
    provider = _provider_with_player(connected=True, last_bootseq=10)

    await _handle_mdns(provider, "11")

    sonos_player = provider._sonos_player
    sonos_player.force_reconnect.assert_called_once()
    sonos_player.reconnect.assert_not_called()
    assert sonos_player.last_bootseq == 11


@pytest.mark.asyncio
async def test_unchanged_bootseq_does_not_reconnect() -> None:
    """An mDNS update without a bootseq change leaves the connection alone."""
    provider = _provider_with_player(connected=True, last_bootseq=10)

    await _handle_mdns(provider, "10")

    sonos_player = provider._sonos_player
    sonos_player.force_reconnect.assert_not_called()
    sonos_player.reconnect.assert_not_called()


@pytest.mark.asyncio
async def test_first_seen_bootseq_is_only_recorded() -> None:
    """The first observed bootseq is recorded without triggering a reconnect."""
    provider = _provider_with_player(connected=True, last_bootseq=None)

    await _handle_mdns(provider, "10")

    sonos_player = provider._sonos_player
    sonos_player.force_reconnect.assert_not_called()
    assert sonos_player.last_bootseq == 10


@pytest.mark.asyncio
async def test_disconnected_player_uses_regular_reconnect() -> None:
    """A disconnected player keeps using the regular reconnect path."""
    provider = _provider_with_player(connected=False, last_bootseq=10)

    await _handle_mdns(provider, "11")

    sonos_player = provider._sonos_player
    sonos_player.reconnect.assert_called_once()
    sonos_player.force_reconnect.assert_not_called()
