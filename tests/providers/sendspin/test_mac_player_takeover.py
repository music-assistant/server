"""Tests for moving a device's MAC address player over to its encrypted client id."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.sendspin.provider import SendspinProvider

if TYPE_CHECKING:
    from aiosendspin.server import SendspinServer
    from aiosendspin.server.client import SendspinClient

    from music_assistant.mass import MusicAssistant

MAC_PLAYER_ID = "20:F8:3B:09:42:05"
NEW_CLIENT_ID = "jyu3dS9mC2xHq0yGm7Hn3oUuZz8kPq1wLr5tXv0aB4c"


def _client(*, encrypted: bool = True, connected: bool = True) -> SendspinClient:
    return cast(
        "SendspinClient",
        SimpleNamespace(
            connection=SimpleNamespace(is_encrypted=encrypted),
            is_connected=connected,
            info_or_none=SimpleNamespace(
                device_info=SimpleNamespace(mac_address=MAC_PLAYER_ID.lower())
            ),
        ),
    )


def _make_provider(
    mass: MagicMock, mac_client: SendspinClient | None = None, allow_unencrypted: bool = True
) -> SendspinProvider:
    stored_mac_player = {"provider": "sendspin", "enabled": True}
    mass.config.get_raw_player_config_value.side_effect = lambda player_id, key, default=None: (
        stored_mac_player.get(key, default) if player_id == MAC_PLAYER_ID else default
    )
    mass.players.unregister = AsyncMock()
    provider = SendspinProvider.__new__(SendspinProvider)
    provider.mass = cast("MusicAssistant", mass)
    provider.config = SimpleNamespace(instance_id="sendspin")  # type: ignore[assignment]
    provider.server_api = cast(
        "SendspinServer",
        SimpleNamespace(
            get_client=lambda _client_id: mac_client, allow_unencrypted=allow_unencrypted
        ),
    )
    provider.logger = logging.getLogger("test.sendspin.takeover")
    return provider


async def test_encrypted_client_takes_over_its_mac_player() -> None:
    """The stored player whose id is the reported MAC address moves to the new client id."""
    mass = MagicMock()
    provider = _make_provider(mass)

    taken_over = await provider._take_over_mac_player(NEW_CLIENT_ID, _client())

    assert taken_over == MAC_PLAYER_ID
    mass.players.unregister.assert_awaited_once_with(MAC_PLAYER_ID)
    mass.players.carry_over_player_config.assert_called_once_with(MAC_PLAYER_ID, NEW_CLIENT_ID)


async def test_unencrypted_client_does_not_take_over() -> None:
    """An unauthenticated client can not claim a device by reporting its MAC address."""
    mass = MagicMock()
    provider = _make_provider(mass)

    taken_over = await provider._take_over_mac_player("legacy-client", _client(encrypted=False))

    assert taken_over is None
    mass.players.carry_over_player_config.assert_not_called()


async def test_connected_mac_client_is_left_alone() -> None:
    """Another client on the same device that still uses the MAC address keeps its player."""
    mass = MagicMock()
    provider = _make_provider(mass, mac_client=_client(encrypted=False, connected=True))

    taken_over = await provider._take_over_mac_player(NEW_CLIENT_ID, _client())

    assert taken_over is None
    mass.players.unregister.assert_not_awaited()


async def test_no_takeover_once_unencrypted_clients_are_refused() -> None:
    """Without legacy clients nobody else can claim the MAC id, so neither can a new client."""
    mass = MagicMock()
    provider = _make_provider(mass, allow_unencrypted=False)

    taken_over = await provider._take_over_mac_player(NEW_CLIENT_ID, _client())

    assert taken_over is None
    mass.players.unregister.assert_not_awaited()
