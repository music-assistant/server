"""Tests for a Bose SoundTouch speaker attached on a running server, and for its discovery."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from music_assistant_models.enums import IdentifierType, MediaType, ProtocolRole

from music_assistant.constants import CONF_OUTPUT_CODEC
from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.models.player import PlayerMedia
from music_assistant.providers.bose_soundtouch.client.schema.models import Info
from music_assistant.providers.bose_soundtouch.player import BoseSoundTouchPlayer
from music_assistant.providers.bose_soundtouch.provider import BoseSoundTouchProvider
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

DEVICE_ID = "AABBCCDDEEFF"
PROTOCOL_ID = f"bose_soundtouch_{DEVICE_ID}"
MODULE = "music_assistant.providers.bose_soundtouch.provider"


@pytest.fixture
async def scenario(tmp_path: pathlib.Path) -> AsyncGenerator[Scenario]:
    """
    Boot a real server with fake player providers.

    :param tmp_path: Temporary directory for the server's storage.
    """
    scenario = Scenario(tmp_path)
    try:
        await scenario.start()
        yield scenario
    finally:
        await scenario.close()


def _info() -> Info:
    """Return the info the speaker reports about itself."""
    return Info(
        device_id=DEVICE_ID,
        name="Speaker A",
        mac_addresses=["AA:BB:CC:DD:EE:FF"],
        ip_addresses=["192.168.1.91"],
    )


async def _register(scenario: Scenario) -> BoseSoundTouchPlayer:
    """Register the protocol of a speaker the way discovery sets it up, without contacting it."""
    provider: Any = scenario.mass.get_provider("bose_soundtouch") or await scenario.add_provider(
        "bose_soundtouch"
    )
    provider.get_setup_value = MagicMock(return_value=None)
    client = MagicMock()
    client.session_config.ip = "192.168.1.91"
    client.get_presets = AsyncMock(return_value=MagicMock(presets=[]))
    protocol = BoseSoundTouchPlayer(provider, PROTOCOL_ID, client, _info())
    protocol.set_static_attributes(_info())
    protocol._attr_needs_poll = False
    await scenario.mass.players.register(protocol)
    await scenario.settle()
    return protocol


async def _listen_forever(_protocol: BoseSoundTouchPlayer) -> None:
    """Stand in for the notification channel of the speaker."""
    await asyncio.Event().wait()


async def test_a_new_speaker_is_the_native_protocol_of_a_minted_player(
    scenario: Scenario,
) -> None:
    """A speaker first seen now gets a player of its own, identified by its device id."""
    with patch.object(BoseSoundTouchPlayer, "_listen", _listen_forever):
        protocol = await _register(scenario)

    assert protocol.player is not None
    assert protocol.player.player_id.startswith(PLAYER_ID_PREFIX)
    assert protocol.player.get_role(PROTOCOL_ID) == ProtocolRole.NATIVE
    assert (IdentifierType.UUID, DEVICE_ID) in protocol.identifiers
    assert (IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:FF") in protocol.identifiers


async def test_the_speaker_is_listened_to_while_attached(scenario: Scenario) -> None:
    """The notification channel runs while the protocol is attached, again after a detach."""
    with patch.object(BoseSoundTouchPlayer, "_listen", _listen_forever):
        protocol = await _register(scenario)
        first_listener = protocol._listener_task
        assert first_listener is not None
        assert not first_listener.done()

        await scenario.mass.players.unregister(PROTOCOL_ID)
        await scenario.settle()
        assert first_listener.cancelled()
        assert protocol._listener_task is None

        await scenario.mass.players.register(protocol)
        await scenario.settle()
        second_listener: asyncio.Task[None] | None = protocol._listener_task
        assert second_listener is not None
        assert not second_listener.done()


async def test_a_preset_press_plays_on_the_queue_of_the_player(scenario: Scenario) -> None:
    """A preset pressed on a speaker with a minted player id plays on that player's queue."""
    with patch.object(BoseSoundTouchPlayer, "_listen", _listen_forever):
        protocol = await _register(scenario)
    assert protocol.player is not None
    cast_provider: Any = protocol.provider
    cast_provider.get_preset_media = MagicMock(return_value="library://track/1")

    with patch.object(scenario.mass.player_queues, "play_media", AsyncMock()) as play_media:
        await protocol._handle_preset(1)

    play_media.assert_awaited_once_with(
        queue_id=protocol.player.player_id, media="library://track/1"
    )


async def test_a_new_speaker_streams_in_the_codec_of_its_own_record(scenario: Scenario) -> None:
    """The output codec of a speaker with a minted player id is read from its protocol."""
    with patch.object(BoseSoundTouchPlayer, "_listen", _listen_forever):
        protocol = await _register(scenario)
        assert protocol.player is not None
        player_id = protocol.player.player_id
        await scenario.mass.config.save_protocol_config(PROTOCOL_ID, {CONF_OUTPUT_CODEC: "flac"})
        await scenario.mass.players.unregister(PROTOCOL_ID)
        await scenario.mass.players.register(protocol)
        await scenario.settle()
    media = PlayerMedia(
        uri="source://line_in",
        media_type=MediaType.AUDIO_SOURCE,
        source_id="line_in",
        queue_session_id="session_a",
    )
    avt = MagicMock(stop=AsyncMock(), set_url=AsyncMock(), play=AsyncMock())

    with patch.object(BoseSoundTouchPlayer, "avt", new_callable=PropertyMock, return_value=avt):
        await protocol.play_media(media)

    assert scenario.mass.config.get_player_value(player_id, CONF_OUTPUT_CODEC) is None
    assert avt.set_url.call_args.args[0].uri.endswith(f"/{PROTOCOL_ID}.flac")


async def test_discovery_offers_the_manual_addresses_without_an_attached_speaker() -> None:
    """A manually added speaker that is not attached is set up again, an attached one not."""
    provider = BoseSoundTouchProvider.__new__(BoseSoundTouchProvider)
    provider.config = MagicMock()
    provider.config.get_value.return_value = ["192.168.1.91", " 192.168.1.92 "]
    attached = MagicMock(spec=BoseSoundTouchPlayer, ip_address="192.168.1.91")
    provider.mass = MagicMock()
    provider.mass.players.iter_protocols.return_value = [attached]
    provider.try_add_player = AsyncMock()  # type: ignore[method-assign]

    await provider.discover_players()

    provider.try_add_player.assert_awaited_once_with("192.168.1.92")


async def test_a_speaker_the_server_does_not_want_is_not_set_up() -> None:
    """A disabled speaker found at an address is neither built nor attached."""
    provider = BoseSoundTouchProvider.__new__(BoseSoundTouchProvider)
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.mass.players.get_protocol.return_value = None
    provider.mass.players.wants_protocol.return_value = False
    client = MagicMock(get_info=AsyncMock(return_value=_info()))

    with (
        patch(f"{MODULE}.SoundtouchDevice", return_value=client),
        patch(f"{MODULE}.BoseSoundTouchPlayer") as player_cls,
    ):
        await provider.try_add_player("192.168.1.91")

    provider.mass.players.wants_protocol.assert_called_once_with(PROTOCOL_ID)
    player_cls.assert_not_called()
