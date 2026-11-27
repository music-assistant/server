"""Tests for the volume/mute features a Sendspin player exposes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock, PropertyMock

import pytest
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand
from aiosendspin.server import VolumeChangedEvent
from music_assistant_models.enums import PlayerFeature

import music_assistant.providers.sendspin.player as player_module
from music_assistant.models.player_protocol import PlayerProtocol
from music_assistant.providers.sendspin.player import SendspinBasePlayer, SendspinPlayer

if TYPE_CHECKING:
    from aiosendspin.server.client import SendspinClient

    from music_assistant.models.player_provider import PlayerProvider
    from music_assistant.providers.sendspin.provider import SendspinProvider

CONTROL_FEATURES = {PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE}


@pytest.fixture
def player_role(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Return the player role every test player resolves, with no commands declared yet."""

    def _init_player(self: PlayerProtocol, provider: PlayerProvider, protocol_id: str) -> None:
        self._provider = provider
        self._protocol_id = protocol_id
        self._player = None

    role = SimpleNamespace(state_supported_commands=[])
    monkeypatch.setattr(PlayerProtocol, "__init__", _init_player)
    monkeypatch.setattr(SendspinBasePlayer, "_refresh_client_info", Mock())
    monkeypatch.setattr(SendspinBasePlayer, "_subscribe_client_callbacks", Mock())
    monkeypatch.setattr(SendspinPlayer, "_refresh_client_info", Mock())
    monkeypatch.setattr(SendspinPlayer, "_subscribe_client_callbacks", Mock())
    monkeypatch.setattr(player_module, "SendspinPlaybackSession", Mock())
    monkeypatch.setattr(SendspinBasePlayer, "_player_role", PropertyMock(return_value=role))
    return role


def _player(
    commands: list[PlayerCommand] | None, *, allows_legacy_clients: bool = True
) -> SendspinPlayer:
    """
    Create a player whose hello lists ``commands``, or none for a current client.

    :param commands: The commands the hello lists.
    :param allows_legacy_clients: The server accepts clients that are not spec-compliant.
    """
    support = ClientHelloPlayerSupport(
        supported_formats=[
            SupportedAudioFormat(codec=AudioCodec.PCM, channels=2, sample_rate=48000, bit_depth=16)
        ],
        buffer_capacity=1024,
        supported_commands=commands,
    )
    client = SimpleNamespace(
        info=SimpleNamespace(player_support=support), group=SimpleNamespace(clients=[])
    )
    provider = cast(
        "SendspinProvider",
        SimpleNamespace(
            instance_id="sendspin--test",
            logger=Mock(),
            server_api=SimpleNamespace(
                get_client=Mock(return_value=client),
                allow_noncompliant_clients=allows_legacy_clients,
            ),
        ),
    )
    player = SendspinPlayer(provider, "p1")
    player.update_state = Mock()  # type: ignore[method-assign,misc]
    return player


def _volume_event(player: SendspinPlayer) -> None:
    player.event_cb(cast("SendspinClient", Mock()), VolumeChangedEvent(volume=40, muted=False))


def test_current_client_features_follow_client_state(player_role: SimpleNamespace) -> None:
    """A client declaring its commands in client/state gets exactly those controls."""
    player = _player(None)
    assert not player.control_features_pinned
    assert not player.supported_features & CONTROL_FEATURES

    player_role.state_supported_commands = [PlayerCommand.VOLUME]
    _volume_event(player)
    assert player.supported_features & CONTROL_FEATURES == {PlayerFeature.VOLUME_SET}
    assert player.volume_level == 40

    player_role.state_supported_commands = [PlayerCommand.MUTE]
    _volume_event(player)
    assert player.supported_features & CONTROL_FEATURES == {PlayerFeature.VOLUME_MUTE}


@pytest.mark.usefixtures("player_role")
@pytest.mark.parametrize("allows_legacy_clients", [True, False])
def test_a_server_that_allows_legacy_clients_keeps_the_flow_stream(
    allows_legacy_clients: bool,
) -> None:
    """Players take a queue item by item only on a server for spec-compliant clients."""
    player = _player(None, allows_legacy_clients=allows_legacy_clients)

    assert player.requires_flow_mode is allows_legacy_clients
    assert player.replaces_enqueued_media is not allows_legacy_clients
    takes_items = {PlayerFeature.ENQUEUE, PlayerFeature.GAPLESS_PLAYBACK}
    assert player.supported_features & takes_items == (
        set() if allows_legacy_clients else takes_items
    )


@pytest.mark.usefixtures("player_role")
@pytest.mark.parametrize("bridge_cuts", [True, False])
def test_a_group_replaces_its_next_item_only_when_every_audio_role_can_cut(
    bridge_cuts: bool,
) -> None:
    """A bridge without timestamped clears in the group keeps the queue's lock on the next item."""
    player = _player(None, allows_legacy_clients=False)
    cuts = Mock(supports_timestamped_clear=Mock(return_value=True))
    bridge = Mock(supports_timestamped_clear=Mock(return_value=bridge_cuts))
    no_audio = Mock(
        get_audio_requirements=Mock(return_value=None),
        supports_timestamped_clear=Mock(return_value=False),
    )
    cast("Any", player.api.group).clients = [
        Mock(active_roles=(cuts, no_audio)),
        Mock(active_roles=(bridge,)),
    ]

    assert player.replaces_enqueued_media is bridge_cuts


def test_client_state_known_before_registration_seeds_the_features(
    player_role: SimpleNamespace,
) -> None:
    """Commands declared before the player registered are exposed right away."""
    player_role.state_supported_commands = [PlayerCommand.VOLUME, PlayerCommand.MUTE]
    player = _player(None)
    assert player.supported_features >= CONTROL_FEATURES


def test_hello_listed_commands_stay_pinned(player_role: SimpleNamespace) -> None:
    """A hello that lists its commands (bridges, older clients) fixes the controls."""
    player = _player([])
    assert player.control_features_pinned
    player_role.state_supported_commands = [PlayerCommand.VOLUME, PlayerCommand.MUTE]
    _volume_event(player)
    assert not player.supported_features & CONTROL_FEATURES

    listed = _player([PlayerCommand.VOLUME, PlayerCommand.MUTE])
    player_role.state_supported_commands = []
    _volume_event(listed)
    assert listed.supported_features >= CONTROL_FEATURES


def test_reregistration_keeps_only_pinned_features(player_role: SimpleNamespace) -> None:
    """A re-created player inherits the first registration's controls only when pinned."""
    pinned = _player([])
    reconnected = _player(None)
    reconnected._attr_supported_features.add(PlayerFeature.VOLUME_SET)
    reconnected.preserve_control_features_from(pinned)
    assert reconnected.control_features_pinned
    assert not reconnected.supported_features & CONTROL_FEATURES

    player_role.state_supported_commands = [PlayerCommand.MUTE]
    following = _player(None)
    previous = _player(None)
    previous._attr_supported_features.add(PlayerFeature.VOLUME_SET)
    following.preserve_control_features_from(previous)
    assert not following.control_features_pinned
    assert following.supported_features & CONTROL_FEATURES == {PlayerFeature.VOLUME_MUTE}
