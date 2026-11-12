"""What a player keeps for a moment: the source it played after it stops, a power on under way."""

from __future__ import annotations

from music_assistant_models.enums import IdentifierType, PlaybackState
from music_assistant_models.player import PlayerMedia

from .harness import FakeProtocol, Scenario

DEN = {IdentifierType.MAC_ADDRESS: "00:11:22:33:44:55", IdentifierType.IP_ADDRESS: "192.168.1.30"}
KITCHEN = {
    IdentifierType.MAC_ADDRESS: "00:11:22:33:44:66",
    IdentifierType.IP_ADDRESS: "192.168.1.31",
}

# media of a Music Assistant source that is not the player's own queue (a group's queue)
MEDIA = PlayerMedia(uri="http://ma.local/stream/track.flac", source_id="group_queue")


async def _stopped_player(scenario: Scenario) -> tuple[FakeProtocol, str]:
    """Return a speaker that played MEDIA and just stopped, and the id of its player."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_DEN", "Den", identifiers=DEN)
    await scenario.attach(speaker)
    await scenario.idle()
    player_id = scenario.single_player(speaker).player_id
    await scenario.mass.players.play_media(player_id, MEDIA)
    await scenario.settle()
    await scenario.report(speaker, playback_state=PlaybackState.IDLE)
    return speaker, player_id


def _active_source(scenario: Scenario, player_id: str) -> str | None:
    """Return the source the player reports to be playing."""
    player = scenario.player(player_id)
    assert player is not None
    return player.active_source


async def test_a_stopped_player_keeps_its_source_for_5_seconds(scenario: Scenario) -> None:
    """A player that stops still names the source it played for 5 s, then its own queue."""
    _speaker, player_id = await _stopped_player(scenario)

    await scenario.advance(4.9)
    assert _active_source(scenario, player_id) == "group_queue"

    await scenario.advance(0.1)
    assert _active_source(scenario, player_id) == player_id


async def test_playing_again_within_5_seconds_keeps_the_source(scenario: Scenario) -> None:
    """A stop for a moment while the playback is replaced does not lose the source."""
    speaker, player_id = await _stopped_player(scenario)

    await scenario.advance(3)
    await scenario.report(speaker, playback_state=PlaybackState.PLAYING)
    await scenario.idle()
    # a playing device keeps reporting, long after the 5 s
    await scenario.report(speaker, volume_level=30)

    assert _active_source(scenario, player_id) == "group_queue"


async def test_stopping_again_starts_the_5_seconds_over(scenario: Scenario) -> None:
    """The 5 s count from the last stop: a player that played in between gets them anew."""
    speaker, player_id = await _stopped_player(scenario)
    await scenario.advance(3)
    await scenario.report(speaker, playback_state=PlaybackState.PLAYING)
    await scenario.advance(1)
    await scenario.report(speaker, playback_state=PlaybackState.IDLE)

    await scenario.advance(4.9)
    assert _active_source(scenario, player_id) == "group_queue"

    await scenario.advance(0.1)
    assert _active_source(scenario, player_id) == player_id


async def test_a_source_selected_while_stopped_is_kept(scenario: Scenario) -> None:
    """A source selected after the player stopped is not forgotten 5 s after that stop."""
    _speaker, player_id = await _stopped_player(scenario)
    kitchen = scenario.provider("sonos").protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.attach(kitchen)
    other_queue = scenario.single_player(kitchen).player_id

    await scenario.advance(1)
    await scenario.mass.players.select_source(player_id, other_queue)
    await scenario.idle()

    assert _active_source(scenario, player_id) == other_queue
