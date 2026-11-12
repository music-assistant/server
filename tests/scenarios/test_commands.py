"""Commands that meet on one player: each waits for its turn, or fails when it does not come."""

from __future__ import annotations

import asyncio
import logging

import pytest
from music_assistant_models.enums import IdentifierType, PlayerFeature
from music_assistant_models.errors import ResourceBusyError
from music_assistant_models.player import PlayerMedia, PlayerSource

from .harness import DEFAULT_FEATURES, FakeProtocol, Scenario

DEN = {IdentifierType.MAC_ADDRESS: "00:11:22:33:44:55", IdentifierType.IP_ADDRESS: "192.168.1.30"}

MEDIA = PlayerMedia(uri="http://ma.local/stream/track.flac")


async def _speaker_with_an_input(scenario: Scenario) -> tuple[FakeProtocol, str]:
    """Return a native speaker with an HDMI input, and the id of its player."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_DEN", "Den", identifiers=DEN)
    await scenario.attach(speaker)
    await scenario.report(
        speaker,
        supported_features={*DEFAULT_FEATURES, PlayerFeature.SELECT_SOURCE},
        source_list=[PlayerSource(id="hdmi", name="HDMI")],
    )
    await scenario.idle()
    return speaker, scenario.single_player(speaker).player_id


def _names(speaker: FakeProtocol) -> list[str]:
    """Return the names of the commands the device received, in order."""
    return [command[0] for command in speaker.commands]


async def test_a_command_waits_for_the_command_that_holds_the_player(
    scenario: Scenario,
) -> None:
    """
    A command sent while another one still works on the player reaches the device after it.

    The play takes 40 s at the device; the input switch sent right behind it must not reach
    the device while the play is still under way, where the play would switch it back.
    """
    speaker, player_id = await _speaker_with_an_input(scenario)

    async def _slow_play_media(media: PlayerMedia) -> None:
        await asyncio.sleep(40)
        await FakeProtocol.play_media(speaker, media)

    speaker.play_media = _slow_play_media  # type: ignore[method-assign]
    play = await scenario.command(scenario.mass.players.play_media(player_id, MEDIA))
    switch = await scenario.command(scenario.mass.players.select_source(player_id, "hdmi"))

    await scenario.advance(39)
    assert "select_source" not in _names(speaker)
    assert not switch.done()

    await scenario.advance(1)
    await play
    await switch

    commands = _names(speaker)
    assert commands.index("play_media") < commands.index("select_source")


async def test_a_command_behind_a_command_that_never_returns_fails_as_busy(
    scenario: Scenario, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A command that waits 120 s for a player that stays busy fails, and sends nothing.

    The error tells the user the player is still busy, the log names what the command that
    holds the player waits for, and the player takes commands again once that one returns.
    """
    speaker, player_id = await _speaker_with_an_input(scenario)
    answered = asyncio.Event()

    async def _play_media_that_never_returns(media: PlayerMedia) -> None:
        await answered.wait()
        await FakeProtocol.play_media(speaker, media)

    speaker.play_media = _play_media_that_never_returns  # type: ignore[method-assign]
    scenario.expect_log("Gave up waiting 120s for the playback lock of player Den")
    play = await scenario.command(scenario.mass.players.play_media(player_id, MEDIA))
    switch = await scenario.command(scenario.mass.players.select_source(player_id, "hdmi"))

    with caplog.at_level(logging.WARNING, logger="music_assistant"):
        await scenario.advance(119)
        assert not switch.done()
        await scenario.advance(1)
        with pytest.raises(ResourceBusyError) as busy:
            await switch

    assert busy.value.translation_key == "player_busy"
    assert busy.value.translation_owner == "core.players"
    assert busy.value.translation_args == ["Den"]
    assert "select_source" not in _names(speaker)
    assert "_play_media_that_never_returns" in caplog.text

    answered.set()
    await scenario.settle()
    await play
    await scenario.mass.players.select_source(player_id, "hdmi")
    await scenario.settle()

    assert _names(speaker)[-1] == "select_source"
