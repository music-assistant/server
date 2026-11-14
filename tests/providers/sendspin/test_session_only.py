"""Tests for which Sendspin players report that they only exist while they are connected."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from music_assistant_models.enums import IdentifierType, PlayerType, ProtocolRole
from music_assistant_models.player import DeviceInfo

from music_assistant.providers.sendspin.player import (
    SendspinBasePlayer,
    SendspinPlayer,
    SendspinSourcePlayer,
    SendspinVisualizerPlayer,
)
from music_assistant.providers.sendspin.provider import SendspinProvider
from tests.common import MockProtocol

if TYPE_CHECKING:
    from aiosendspin.models.core import ClientHelloPayload
    from aiosendspin.server.client import SendspinClient

CLIENT_ID = "client_1"


def _refreshed_player(
    product_name: str | None,
    manufacturer: str | None = None,
    *,
    virtual: bool = False,
) -> SendspinPlayer:
    """
    Return a Sendspin player after the hello of its client was read.

    :param product_name: Product name the client reports, None for no device info at all.
    :param manufacturer: Manufacturer the client reports.
    :param virtual: Whether the player is a virtual player of the provider.
    """
    player = SendspinPlayer.__new__(SendspinPlayer)
    player._protocol_id = CLIENT_ID
    player._player = None
    player._provider = cast(
        "SendspinProvider", SimpleNamespace(is_virtual_player=lambda _player_id: virtual)
    )
    player._attr_device_info = DeviceInfo()
    device_info = (
        SendspinDeviceInfo(product_name=product_name, manufacturer=manufacturer)
        if product_name or manufacturer
        else None
    )
    client = cast(
        "SendspinClient",
        SimpleNamespace(
            info=SimpleNamespace(name="Client", device_info=device_info, player_support=None)
        ),
    )
    player._refresh_client_info(client)
    return player


@pytest.mark.parametrize(
    ("product_name", "manufacturer"),
    [
        ("Web Browser", None),
        ("Web Player", None),
        ("Mobile Application", None),
        ("PWA", None),
        ("Companion", "Music Assistant"),
    ],
)
def test_a_web_or_app_player_is_session_only(product_name: str, manufacturer: str | None) -> None:
    """A browser tab or an app is a player only while it is connected."""
    player = _refreshed_player(product_name, manufacturer)

    assert player.session_only is True
    assert player.type is PlayerType.PLAYER


def test_a_virtual_player_is_session_only() -> None:
    """A virtual player of the provider is session-only, whatever its client reports."""
    assert _refreshed_player("Virtual Player", "Some Plugin", virtual=True).session_only is True


@pytest.mark.parametrize(
    ("product_name", "manufacturer"),
    [("ESP32 Speaker", "Espressif"), (None, None)],
)
def test_a_device_with_a_sendspin_client_is_not_session_only(
    product_name: str | None, manufacturer: str | None
) -> None:
    """A client on a device is that device's Sendspin protocol, which is kept track of."""
    player = _refreshed_player(product_name, manufacturer)

    assert player.session_only is False
    assert player.type is PlayerType.PLAYER


def test_a_web_player_that_is_restored_as_a_bridge_is_not_session_only() -> None:
    """A receiver app that reports as a browser is the Sendspin protocol of its device."""
    player = _refreshed_player("Web Browser")
    player._attr_underlying_protocol_id = "cast-protocol"
    cast_device = DeviceInfo(model="Nest Hub", manufacturer="Google")
    cast_device.add_identifier(IdentifierType.CAST_UUID, "cast-uuid")

    player.restore_bridge_identity(cast_device)

    assert player.session_only is False


async def test_a_web_player_that_is_claimed_as_a_bridge_is_not_session_only() -> None:
    """A bridge that claims an already connected browser client turns it into a protocol."""
    player = _refreshed_player("Web Browser")
    player._attr_supported_features = set()
    player._cache = {"display_name": "Client"}
    provider = SendspinProvider.__new__(SendspinProvider)
    provider.logger = Mock()
    provider.mass = Mock()
    provider.mass.players.register = AsyncMock()
    provider.mass.players.unregister = AsyncMock()
    provider.get_protocol = Mock(return_value=player)  # type: ignore[method-assign]
    player._attr_underlying_protocol_id = None
    hello = cast("ClientHelloPayload", SimpleNamespace(player_support=None))

    assert await provider.apply_bridge_claim(CLIENT_ID, {}, hello, "cast-protocol") is True

    assert player.session_only is False
    # an audio output of the player of its Cast device from now on: it leaves the player of
    # its own first
    assert player.role is ProtocolRole.AUDIO
    assert player.underlying_protocol_id == "cast-protocol"
    provider.mass.players.unregister.assert_awaited_once_with(CLIENT_ID)
    provider.mass.players.register.assert_awaited_once_with(player)


@pytest.mark.parametrize("player_class", [SendspinVisualizerPlayer, SendspinSourcePlayer])
def test_a_player_without_audio_output_is_session_only(
    player_class: type[SendspinBasePlayer],
) -> None:
    """A display, visualizer, light or audio source only exists while it is connected."""
    assert player_class.__new__(player_class).session_only is True


@pytest.mark.parametrize(
    ("negotiated_role_ids", "bridge_type", "expected_type"),
    [
        (["metadata@v1"], None, PlayerType.DISPLAY),
        (["visualizer@v1"], None, PlayerType.VISUALIZER),
        # how the Sendspin lights and the Hue Entertainment bridge register their clients
        (["visualizer@v1"], PlayerType.LIGHT, PlayerType.LIGHT),
        (["source@v1"], None, PlayerType.SOURCE),
    ],
)
def test_the_player_of_a_client_without_a_player_role_is_session_only(
    monkeypatch: pytest.MonkeyPatch,
    negotiated_role_ids: list[str],
    bridge_type: PlayerType | None,
    expected_type: PlayerType,
) -> None:
    """The player a display, visualizer, light or source client gets reports session-only."""
    for player_class in (SendspinVisualizerPlayer, SendspinSourcePlayer):
        monkeypatch.setattr(player_class, "__init__", lambda *_args, **_kwargs: None)
    provider = SendspinProvider.__new__(SendspinProvider)
    provider.mass = Mock()
    provider.mass.config.get_protocol_value.return_value = None
    provider._bridge_identifiers = {}
    provider._bridge_player_types = {}
    provider._bridge_underlying_protocols = {}
    provider._bridge_static_delay_defaults = {}
    if bridge_type:
        provider.register_bridge_player_type(CLIENT_ID, bridge_type)
    client = cast("SendspinClient", SimpleNamespace(negotiated_role_ids=negotiated_role_ids))

    player = provider._create_player(CLIENT_ID, client, None)

    assert player.session_only is True
    assert player.type is expected_type
    # a player of its own while it is connected
    assert player.role is ProtocolRole.NATIVE


def test_a_player_is_not_session_only_unless_it_reports_so() -> None:
    """Any other player is one to keep track of while its device is away."""
    assert MockProtocol.__new__(MockProtocol).session_only is False
