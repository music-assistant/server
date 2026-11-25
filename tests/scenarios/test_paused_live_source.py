"""A live source that pauses leaves its player paused, ready to play on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import (
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    ProviderFeature,
    SourceControl,
)
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.media_items import AudioSource, ProviderMapping
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.controllers.player_queues.constants import DEFAULT_PAUSE_TIMEOUT
from music_assistant.models.plugin import PluginProvider

from .harness import DEFAULT_FEATURES, FakeProtocol, Scenario
from .test_group_power_on import _announce

if TYPE_CHECKING:
    from music_assistant_models.player import Player as PlayerState
    from music_assistant_models.player import PlayerMedia

INSTANCE_ID = "spotify_connect--test"
SOURCE_ID = "main"
SOURCE_URI = f"{INSTANCE_ID}://audio_source/{SOURCE_ID}"


class LiveSource:
    """A plugin whose live source plays on a player, pauses and plays on."""

    def __init__(self, scenario: Scenario, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        Let the server know the plugin.

        :param scenario: The scenario the plugin is loaded in.
        :param monkeypatch: Fixture that undoes it.
        """
        self.scenario = scenario
        self.source = AudioSource(
            item_id=SOURCE_ID,
            provider=INSTANCE_ID,
            name="Spotify Connect",
            provider_mappings={
                ProviderMapping(
                    item_id=SOURCE_ID,
                    provider_domain="spotify_connect",
                    provider_instance=INSTANCE_ID,
                )
            },
            can_play_pause=True,
        )
        self.provider = MagicMock(spec=PluginProvider, instance_id=INSTANCE_ID)
        self.provider.supported_features = {ProviderFeature.AUDIO_SOURCE}
        self.provider.on_source_control = AsyncMock()
        self.provider.on_source_released = AsyncMock()
        mass = scenario.mass
        get_provider = mass.get_provider
        resolve = mass.players._resolve_audio_source_uri

        def _get_provider(instance_id: str, *args: Any, **kwargs: Any) -> Any:
            if instance_id == INSTANCE_ID:
                return self.provider
            return get_provider(instance_id, *args, **kwargs)

        async def _resolve(uri: str) -> Any:
            return (self.source, self.provider) if uri == SOURCE_URI else await resolve(uri)

        monkeypatch.setattr(mass, "get_provider", _get_provider)
        monkeypatch.setattr(mass.players, "_resolve_audio_source_uri", _resolve)

    async def play(self, player_id: str) -> None:
        """
        Play the source on a player, as its plugin does on a start and on a resume.

        :param player_id: The player it plays on.
        """
        players = self.scenario.mass.players
        await players.select_source(player_id, SOURCE_URI)
        session = players.get_audio_source_session(player_id)
        assert session is not None
        # no fake device fetches the stream, which is what claims the source
        assert players.claim_audio_source_session(
            session, session.playback_session_id, f"stream-{session.playback_session_id}"
        )
        players.update_source_metadata(
            player_id, SOURCE_ID, INSTANCE_ID, StreamMetadata(title="Song", artist="Artist")
        )
        await self.scenario.advance(1)

    async def pause(self, player_id: str, source_id: str = SOURCE_ID) -> None:
        """
        Pause the source, as its plugin does when it pauses upstream.

        :param player_id: The player it plays on.
        :param source_id: The source the plugin says paused.
        """
        await self.scenario.mass.players.pause_audio_source(player_id, source_id, INSTANCE_ID)


@pytest.fixture
def live_source(scenario: Scenario, monkeypatch: pytest.MonkeyPatch) -> LiveSource:
    """Return a plugin with a live source the server knows."""
    return LiveSource(scenario, monkeypatch)


async def _speakers(scenario: Scenario, *names: str) -> tuple[list[FakeProtocol], list[str]]:
    """
    Return speakers that can group and have no pause of their own, and their player ids.

    :param names: The names of the speakers.
    """
    provider = await scenario.add_provider("sonos")
    speakers = [
        provider.protocol(
            f"sonos_{name.lower()}",
            name,
            identifiers={IdentifierType.MAC_ADDRESS: f"00:11:22:33:44:0{index}"},
            supported_features={*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS}
            - {PlayerFeature.PAUSE},
            can_group_with={provider.instance_id},
        )
        for index, name in enumerate(names)
    ]
    for speaker in speakers:
        await scenario.register(speaker)
    await scenario.settle()
    return speakers, [cast("str", scenario.player_of(speaker)) for speaker in speakers]


async def _speaker(scenario: Scenario) -> tuple[FakeProtocol, str]:
    """Return the Kitchen speaker, without a pause of its own, and the id of its player."""
    speakers, player_ids = await _speakers(scenario, "Kitchen")
    return speakers[0], player_ids[0]


async def _airplay_speakers(
    scenario: Scenario, *names: str
) -> tuple[list[FakeProtocol], list[str]]:
    """
    Return speakers that play over AirPlay and can group, and their player ids.

    :param names: The names of the speakers.
    """
    provider = await scenario.add_provider("airplay")
    speakers = [
        provider.protocol(
            f"ap_{name.lower()}",
            name,
            role="audio",
            identifiers={IdentifierType.MAC_ADDRESS: f"00:11:22:33:44:0{index}"},
            supported_features={*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS},
            can_group_with={provider.instance_id},
        )
        for index, name in enumerate(names)
    ]
    for speaker in speakers:
        await scenario.register(speaker)
    await scenario.settle()
    return speakers, [cast("str", scenario.player_of(speaker)) for speaker in speakers]


def _state(scenario: Scenario, player_id: str) -> PlayerState:
    """Return what a client sees of a player."""
    state = scenario.player(player_id)
    assert state is not None
    return state


def _names(speaker: FakeProtocol) -> list[str]:
    """Return the names of the playback commands a speaker got, in order."""
    return [x[0] for x in speaker.commands if x[0] in ("play_media", "play", "pause", "stop")]


def _seen_states(scenario: Scenario, player_id: str) -> list[PlaybackState]:
    """Return the playback states clients were told, each once it changed."""
    states: list[PlaybackState] = []
    for update in scenario.player_updates(player_id):
        if not states or states[-1] != update.playback_state:
            states.append(update.playback_state)
    return states


async def test_a_paused_live_source_leaves_its_player_paused(
    scenario: Scenario, live_source: LiveSource
) -> None:
    """The speaker is silent at once, and clients go from playing to paused."""
    speaker, player_id = await _speaker(scenario)
    await live_source.play(player_id)
    speaker.commands.clear()
    scenario.clear_events()

    await live_source.pause(player_id)
    await scenario.advance(5)

    assert _names(speaker) == ["stop"]
    state = _state(scenario, player_id)
    assert state.playback_state == PlaybackState.PAUSED
    assert state.active_source == SOURCE_URI
    assert state.current_media is not None
    assert state.current_media.title == "Song"
    assert PlaybackState.IDLE not in _seen_states(scenario, player_id)
    live_source.provider.on_source_released.assert_not_awaited()
    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    assert player.stop_called


async def test_play_on_a_paused_live_source_goes_to_the_source(
    scenario: Scenario, live_source: LiveSource
) -> None:
    """Play resumes the source itself, and clients go from paused to playing."""
    speaker, player_id = await _speaker(scenario)
    await live_source.play(player_id)
    await live_source.pause(player_id)
    await scenario.advance(5)
    scenario.clear_events()

    await scenario.mass.players.cmd_play(player_id)
    live_source.provider.on_source_control.assert_awaited_once_with(SOURCE_ID, SourceControl.PLAY)
    await live_source.play(player_id)
    # past the pause timeout of the pause before
    await scenario.advance(DEFAULT_PAUSE_TIMEOUT)

    assert _seen_states(scenario, player_id) == [PlaybackState.PLAYING]
    assert _names(speaker) == ["play_media", "stop", "play_media"]
    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    assert not player.stop_called
    # its provider sees the stream end now, and lets go of the source itself
    await scenario.mass.players.cmd_stop(player_id)
    live_source.provider.on_source_released.assert_not_awaited()


async def test_a_resumed_live_source_that_ends_leaves_its_player_idle(
    scenario: Scenario, live_source: LiveSource
) -> None:
    """The pause is over once the player plays, so a stream that ends is no pause again."""
    speaker, player_id = await _speaker(scenario)
    await live_source.play(player_id)
    await live_source.pause(player_id)
    await scenario.advance(5)
    await live_source.play(player_id)

    await scenario.report(speaker, playback_state=PlaybackState.IDLE)

    assert _state(scenario, player_id).playback_state == PlaybackState.IDLE


async def test_a_player_synced_over_an_audio_protocol_pauses_with_its_leader(
    scenario: Scenario, live_source: LiveSource
) -> None:
    """Every speaker of an AirPlay group shows as paused, and as idle after the pause timeout."""
    _, (kitchen_id, office_id) = await _airplay_speakers(scenario, "Kitchen", "Office")
    await live_source.play(kitchen_id)
    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[office_id])
    await scenario.advance(5)
    assert _state(scenario, office_id).synced_to == kitchen_id

    await live_source.pause(kitchen_id)
    await scenario.advance(5)

    assert _state(scenario, kitchen_id).playback_state == PlaybackState.PAUSED
    assert _state(scenario, office_id).playback_state == PlaybackState.PAUSED
    await scenario.advance(DEFAULT_PAUSE_TIMEOUT)
    assert _state(scenario, kitchen_id).playback_state == PlaybackState.IDLE
    assert _state(scenario, office_id).playback_state == PlaybackState.IDLE


async def test_a_paused_live_source_goes_idle_at_the_pause_timeout(
    scenario: Scenario, live_source: LiveSource
) -> None:
    """The player goes idle at the pause timeout, and play still goes to the source."""
    _, player_id = await _speaker(scenario)
    await live_source.play(player_id)
    await live_source.pause(player_id)

    await scenario.advance(DEFAULT_PAUSE_TIMEOUT - 1)
    assert _state(scenario, player_id).playback_state == PlaybackState.PAUSED
    await scenario.advance(2)
    state = _state(scenario, player_id)
    assert state.playback_state == PlaybackState.IDLE
    assert state.active_source == SOURCE_URI
    live_source.provider.on_source_released.assert_not_awaited()

    await scenario.mass.players.cmd_play(player_id)
    live_source.provider.on_source_control.assert_awaited_once_with(SOURCE_ID, SourceControl.PLAY)


@pytest.mark.parametrize(
    "waits", [5, DEFAULT_PAUSE_TIMEOUT + 5], ids=["while_paused", "after_the_pause_timeout"]
)
async def test_a_stop_of_a_paused_live_source_lets_it_go(
    scenario: Scenario, live_source: LiveSource, waits: int
) -> None:
    """
    A stop by the user ends the source, and the player is back on its own queue.

    :param waits: How long after the pause the user stops the player.
    """
    _, player_id = await _speaker(scenario)
    await live_source.play(player_id)
    await live_source.pause(player_id)
    await scenario.advance(waits)

    await scenario.mass.players.cmd_stop(player_id)
    await scenario.advance(1)

    live_source.provider.on_source_released.assert_awaited_once_with(SOURCE_ID, player_id)
    state = _state(scenario, player_id)
    assert state.playback_state == PlaybackState.IDLE
    assert state.active_source == player_id


async def test_a_source_that_ends_while_paused_leaves_its_player_idle(
    scenario: Scenario, live_source: LiveSource
) -> None:
    """A session that ends upstream during the pause is not shown as paused any longer."""
    _, player_id = await _speaker(scenario)
    await live_source.play(player_id)
    await live_source.pause(player_id)
    await scenario.advance(5)
    session = scenario.mass.players.get_audio_source_session(player_id)
    assert session is not None

    await scenario.mass.players.deselect_source(
        player_id,
        stop_playback=False,
        provider_instance_id=INSTANCE_ID,
        source_id=SOURCE_ID,
        playback_session_id=session.playback_session_id,
    )
    await scenario.advance(1)

    assert _state(scenario, player_id).playback_state == PlaybackState.IDLE


async def test_a_paused_live_source_that_moves_leaves_its_player_idle(
    scenario: Scenario, live_source: LiveSource
) -> None:
    """A source that plays on elsewhere is not shown as paused on the player it left."""
    _, (kitchen_id, office_id) = await _speakers(scenario, "Kitchen", "Office")
    await live_source.play(kitchen_id)
    await live_source.pause(kitchen_id)
    await scenario.advance(5)

    await live_source.play(office_id)

    assert _state(scenario, office_id).playback_state == PlaybackState.PLAYING
    state = _state(scenario, kitchen_id)
    assert state.playback_state == PlaybackState.IDLE
    assert state.active_source == kitchen_id


@pytest.mark.parametrize("leader_state", [PlaybackState.PLAYING, PlaybackState.PAUSED])
async def test_a_paused_player_that_joins_a_group_plays_along(
    scenario: Scenario, live_source: LiveSource, leader_state: PlaybackState
) -> None:
    """
    The pause timeout does not stop a player that plays along with another one by then.

    :param leader_state: What the other one does when the pause timeout passes.
    """
    (kitchen, office), (kitchen_id, office_id) = await _speakers(scenario, "Kitchen", "Office")
    await live_source.play(kitchen_id)
    await live_source.pause(kitchen_id)
    await scenario.advance(5)
    await scenario.mass.players.cmd_set_members(office_id, player_ids_to_add=[kitchen_id])
    await scenario.advance(5)
    await scenario.play(office_id)
    await scenario.report(office, playback_state=leader_state)
    kitchen.commands.clear()

    # past the pause timeout of Kitchen, and not of Office
    await scenario.advance(DEFAULT_PAUSE_TIMEOUT - 8)

    assert _names(kitchen) == []
    assert _state(scenario, kitchen_id).playback_state == leader_state
    await scenario.mass.players.cmd_stop(office_id)
    await scenario.idle()


async def test_other_media_takes_the_place_of_a_paused_live_source(
    scenario: Scenario, live_source: LiveSource
) -> None:
    """Music of the queue starts at once and lets the source go."""
    speaker, player_id = await _speaker(scenario)
    await live_source.play(player_id)
    await live_source.pause(player_id)
    await scenario.advance(5)
    speaker.commands.clear()

    await scenario.play(player_id)

    live_source.provider.on_source_released.assert_awaited_once_with(SOURCE_ID, player_id)
    state = _state(scenario, player_id)
    assert state.playback_state == PlaybackState.PLAYING
    assert state.active_source == player_id
    # past the pause timeout, which no longer counts
    await scenario.advance(DEFAULT_PAUSE_TIMEOUT)
    assert _state(scenario, player_id).playback_state == PlaybackState.PLAYING
    await scenario.mass.players.cmd_stop(player_id)
    await scenario.idle()


async def test_an_announcement_on_a_paused_live_source_leaves_the_source(
    scenario: Scenario, live_source: LiveSource
) -> None:
    """The announcement plays without waiting for the paused player, which keeps its source."""
    speaker, player_id = await _speaker(scenario)
    await live_source.play(player_id)
    await live_source.pause(player_id)
    await scenario.advance(5)
    speaker.commands.clear()
    started = scenario.mass.loop.time()
    heard: list[float] = []
    apply = speaker._apply

    def _heard(command: str, *args: Any, **state: Any) -> None:
        if command == "play_media":
            heard.append(scenario.mass.loop.time() - started)
        apply(command, *args, **state)

    speaker._apply = _heard  # type: ignore[method-assign]
    await _announce(scenario, player_id, None, [speaker])

    assert heard
    assert heard[0] < 5
    state = _state(scenario, player_id)
    assert state.playback_state == PlaybackState.IDLE
    assert state.active_source == SOURCE_URI
    live_source.provider.on_source_released.assert_not_awaited()


async def test_a_source_that_pauses_because_of_a_stop_leaves_its_player_idle(
    scenario: Scenario, live_source: LiveSource
) -> None:
    """A source that pauses as its player was stopped does not turn the stop into a pause."""
    speaker, player_id = await _speaker(scenario)
    await live_source.play(player_id)
    speaker.commands.clear()
    await scenario.mass.players.cmd_stop(player_id)
    await scenario.advance(1)
    scenario.clear_events()

    await live_source.pause(player_id)
    await scenario.advance(1)

    assert _names(speaker) == ["stop"]
    assert PlaybackState.PAUSED not in _seen_states(scenario, player_id)
    assert _state(scenario, player_id).playback_state == PlaybackState.IDLE


async def test_a_source_that_pauses_while_its_player_still_stops_leaves_it_idle(
    scenario: Scenario, live_source: LiveSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A speaker that reports a stop late is not taken for one that was paused."""
    speaker, player_id = await _speaker(scenario)
    await live_source.play(player_id)
    # the speaker takes the stop, and reports it late
    monkeypatch.setattr(speaker, "stop", AsyncMock())
    await scenario.mass.players.cmd_stop(player_id)

    await live_source.pause(player_id)
    await scenario.report(speaker, playback_state=PlaybackState.IDLE)

    assert _state(scenario, player_id).playback_state == PlaybackState.IDLE


async def test_a_source_that_pauses_before_its_player_plays_leaves_it_idle(
    scenario: Scenario, live_source: LiveSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A player that has not played the source yet has nothing to be paused on."""
    speaker, player_id = await _speaker(scenario)
    monkeypatch.setattr(speaker, "play_media", AsyncMock())
    await live_source.play(player_id)

    await live_source.pause(player_id)
    await scenario.advance(1)

    assert _state(scenario, player_id).playback_state == PlaybackState.IDLE


async def test_a_resume_shortly_before_the_pause_timeout_plays_on(
    scenario: Scenario, live_source: LiveSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A speaker that is slow to start is not stopped by the pause timeout meanwhile."""
    (speaker,), (player_id,) = await _airplay_speakers(scenario, "Kitchen")
    await live_source.play(player_id)
    await live_source.pause(player_id)
    await scenario.advance(DEFAULT_PAUSE_TIMEOUT - 2)
    speaker.commands.clear()
    scenario.clear_events()

    async def _starts_late(media: PlayerMedia) -> None:
        speaker._apply("play_media", media, current_media=media)

    monkeypatch.setattr(speaker, "play_media", _starts_late)
    await live_source.play(player_id)
    await scenario.advance(3)
    assert _state(scenario, player_id).playback_state == PlaybackState.PAUSED
    await scenario.report(speaker, playback_state=PlaybackState.PLAYING)
    await scenario.advance(DEFAULT_PAUSE_TIMEOUT)

    assert _names(speaker) == ["play_media"]
    assert _seen_states(scenario, player_id)[-1] == PlaybackState.PLAYING
    assert PlaybackState.IDLE not in _seen_states(scenario, player_id)


async def test_a_resume_that_never_plays_leaves_its_player_idle(
    scenario: Scenario, live_source: LiveSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A speaker that takes the resume and stays silent is not shown as paused for good."""
    (speaker,), (player_id,) = await _airplay_speakers(scenario, "Kitchen")
    await live_source.play(player_id)
    await live_source.pause(player_id)
    await scenario.advance(5)
    speaker.commands.clear()
    monkeypatch.setattr(speaker, "play_media", AsyncMock())
    await live_source.play(player_id)
    assert _state(scenario, player_id).playback_state == PlaybackState.PAUSED

    await scenario.advance(DEFAULT_PAUSE_TIMEOUT)

    assert _state(scenario, player_id).playback_state == PlaybackState.IDLE
    # it can still get to play
    assert _names(speaker) == []


async def test_a_pause_during_a_resume_silences_the_speaker(
    scenario: Scenario, live_source: LiveSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source that pauses again before the speaker plays stays paused, and stops the start."""
    speaker, player_id = await _speaker(scenario)
    await live_source.play(player_id)
    await live_source.pause(player_id)
    await scenario.advance(5)
    speaker.commands.clear()

    async def _starts_late(media: PlayerMedia) -> None:
        speaker._apply("play_media", media)

    monkeypatch.setattr(speaker, "play_media", _starts_late)
    await live_source.play(player_id)
    await live_source.pause(player_id)
    await scenario.advance(1)

    assert _names(speaker) == ["play_media", "stop"]
    assert _state(scenario, player_id).playback_state == PlaybackState.PAUSED
    # the speaker got to play the start before it took the stop
    await scenario.report(speaker, playback_state=PlaybackState.PLAYING)
    await scenario.report(speaker, playback_state=PlaybackState.IDLE)
    assert _state(scenario, player_id).playback_state == PlaybackState.PAUSED


async def test_what_the_speaker_plays_itself_outlasts_the_pause_timeout(
    scenario: Scenario, live_source: LiveSource
) -> None:
    """The pause timeout does not stop a speaker that plays something of its own by then."""
    speaker, player_id = await _speaker(scenario)
    await live_source.play(player_id)
    await live_source.pause(player_id)
    await scenario.advance(5)
    speaker.commands.clear()
    await scenario.report(speaker, playback_state=PlaybackState.PLAYING)

    await scenario.advance(DEFAULT_PAUSE_TIMEOUT)

    assert _names(speaker) == []
    assert _state(scenario, player_id).playback_state == PlaybackState.PLAYING


async def test_a_source_that_plays_on_before_the_speaker_stopped_is_not_paused(
    scenario: Scenario, live_source: LiveSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resume that overtakes the pause leaves no pause behind for when playback ends."""
    speaker, player_id = await _speaker(scenario)
    await live_source.play(player_id)
    # the speaker takes the stop and the start, and reports neither before it stopped
    monkeypatch.setattr(speaker, "stop", AsyncMock())
    monkeypatch.setattr(speaker, "play_media", AsyncMock())
    await live_source.pause(player_id)
    assert _state(scenario, player_id).playback_state == PlaybackState.PLAYING
    await live_source.play(player_id)
    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    assert not player.stop_called

    await scenario.report(speaker, playback_state=PlaybackState.IDLE)

    assert _state(scenario, player_id).playback_state == PlaybackState.IDLE


async def test_a_speaker_that_refuses_the_pause_is_not_paused(
    scenario: Scenario, live_source: LiveSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A speaker that plays on is shown as it is, also when it ends later."""
    speaker, player_id = await _speaker(scenario)
    await live_source.play(player_id)
    monkeypatch.setattr(speaker, "stop", AsyncMock(side_effect=PlayerCommandFailed("busy")))

    with pytest.raises(PlayerCommandFailed):
        await live_source.pause(player_id)

    assert _state(scenario, player_id).playback_state == PlaybackState.PLAYING
    await scenario.report(speaker, playback_state=PlaybackState.IDLE)
    assert _state(scenario, player_id).playback_state == PlaybackState.IDLE


async def test_a_pause_of_another_source_leaves_the_player_alone(
    scenario: Scenario, live_source: LiveSource
) -> None:
    """Only the source that plays on the player can pause it."""
    speaker, player_id = await _speaker(scenario)
    await live_source.play(player_id)
    speaker.commands.clear()

    await live_source.pause(player_id, "another")
    await scenario.advance(1)

    assert _names(speaker) == []
    assert _state(scenario, player_id).playback_state == PlaybackState.PLAYING


async def test_a_speaker_that_can_be_held_keeps_its_connection(
    scenario: Scenario, live_source: LiveSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A speaker with its own way to wait for the resume is held instead of stopped."""
    speaker, player_id = await _speaker(scenario)
    await live_source.play(player_id)
    speaker.commands.clear()

    async def _hold() -> None:
        speaker._apply("hold", playback_state=PlaybackState.PAUSED)

    monkeypatch.setattr(speaker, "hold", _hold)
    await live_source.pause(player_id)
    await scenario.advance(5)

    assert _names(speaker) == []
    assert _state(scenario, player_id).playback_state == PlaybackState.PAUSED
    # the server stops it at the pause timeout, as it stops a paused queue
    await scenario.advance(DEFAULT_PAUSE_TIMEOUT)
    assert _names(speaker) == ["stop"]
    assert _state(scenario, player_id).playback_state == PlaybackState.IDLE
