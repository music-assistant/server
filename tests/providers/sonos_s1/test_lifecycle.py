"""Tests for a Sonos S1 speaker attached to its player on a running server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import IdentifierType, MediaType, ProtocolRole
from music_assistant_models.player import PlayerMedia

from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.sonos_s1.player import SonosPlayer
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

UID = "RINCON_000E58AAAAAA01400"


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


async def _attach_speaker(scenario: Scenario, unsubscribe: AsyncMock | None = None) -> SonosPlayer:
    """
    Attach a speaker the way discovery sets it up.

    :param unsubscribe: Stands in for the release of the speaker's event subscriptions.
    """
    provider: Any = await scenario.add_provider("sonos_s1")
    soco = MagicMock()
    soco.uid = UID
    soco.household_id = "Sonos_household"
    soco.player_name = "Speaker A"
    soco.ip_address = "192.168.1.51"
    soco.speaker_info = {"model_name": "Sonos Play:1"}
    protocol = SonosPlayer(provider, soco, fixed_volume=False)
    with (
        patch.object(protocol, "update_groups"),
        patch.object(protocol, "poll_media"),
        patch.object(protocol, "subscribe", AsyncMock()),
        patch.object(protocol, "unsubscribe", unsubscribe or AsyncMock()),
    ):
        await protocol.setup()
    await scenario.settle()
    return protocol


async def test_a_new_speaker_is_the_native_protocol_of_a_minted_player(
    scenario: Scenario,
) -> None:
    """A speaker first seen now gets a player of its own, identified by its MAC and its uid."""
    protocol = await _attach_speaker(scenario)

    player = protocol.player
    assert player is not None
    assert player.player_id.startswith(PLAYER_ID_PREFIX)
    assert player.get_role(UID) == ProtocolRole.NATIVE
    assert (IdentifierType.UUID, UID) in protocol.identifiers
    assert (IdentifierType.MAC_ADDRESS, "00:0E:58:AA:AA:AA") in protocol.identifiers


async def test_a_new_speaker_streams_live_sources_as_its_own_setting_asks(
    scenario: Scenario,
) -> None:
    """The speaker's own default of the WAV setting is read for it under its protocol id."""
    protocol = await _attach_speaker(scenario)
    assert protocol.player is not None
    assert protocol.player.player_id != UID
    media = PlayerMedia(
        uri="source://line_in",
        media_type=MediaType.AUDIO_SOURCE,
        source_id="line_in",
        queue_session_id="session_a",
    )

    with patch.object(protocol, "schedule_poll"):
        await protocol.play_media(media)

    stream_url = protocol.soco.play_uri.call_args.args[0]
    assert stream_url.endswith(f"/source/session_a/line_in/{UID}.wav")


async def test_a_speaker_of_a_disabled_player_releases_its_subscriptions(
    scenario: Scenario,
) -> None:
    """A speaker set up for a player the user disabled does not keep its event subscriptions."""
    airplay = await scenario.add_provider("airplay")
    audio = airplay.protocol(
        "ap000e58aaaaaa",
        "Speaker A",
        role="audio",
        identifiers={IdentifierType.MAC_ADDRESS: "00:0E:58:AA:AA:AA"},
    )
    await scenario.register(audio)
    player_id = scenario.player_of(audio)
    assert player_id is not None
    await scenario.disable(player_id)
    unsubscribe = AsyncMock()

    protocol = await _attach_speaker(scenario, unsubscribe)

    assert protocol.player is None
    unsubscribe.assert_awaited_once_with()


async def test_a_speaker_taken_off_its_player_follows_its_events_again(
    scenario: Scenario,
) -> None:
    """A speaker moved to a player of its own subscribes to its events and reports again."""
    protocol = await _attach_speaker(scenario)
    audio = (await scenario.add_provider("airplay")).protocol(
        "ap000e58aaaaaa",
        "Speaker A",
        role="audio",
        identifiers={IdentifierType.MAC_ADDRESS: "00:0E:58:AA:AA:AA"},
    )
    await scenario.register(audio)
    assert protocol.player is not None
    old_player_id = protocol.player.player_id
    assert scenario.player_of(audio) == old_player_id
    subscribe = AsyncMock()

    with (
        patch.object(protocol, "subscribe", subscribe),
        patch.object(protocol, "unsubscribe", AsyncMock()),
    ):
        new_player_id = await scenario.mass.players.unlink_protocol(UID)
        await scenario.settle()

    assert protocol.player is not None
    assert protocol.player.player_id == new_player_id != old_player_id
    subscribe.assert_awaited_once_with()
    with patch.object(protocol, "_update_attributes") as update_attributes:
        protocol.update_player(signal_update=False)
    update_attributes.assert_called_once_with()
