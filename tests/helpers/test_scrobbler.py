"""Tests for the scrobbler helpers."""

import logging
from unittest import mock

import pytest
from music_assistant_models.enums import EventType, MediaType, PlayerType
from music_assistant_models.event import MassEvent
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport

from music_assistant.helpers.scrobbler import (
    ScrobblerConfig,
    ScrobblerHelper,
    create_scrobble_players_config_entry,
)


class DummyHandler(ScrobblerHelper):
    """Spy version of a ScrobblerHelper to allow easy testing."""

    _tracked = 0
    _now_playing = 0

    def __init__(
        self,
        logger: logging.Logger,
        config: ScrobblerConfig | None = None,
        supported_media_types: frozenset[MediaType] | None = None,
    ) -> None:
        """Initialize."""
        super().__init__(logger, config, supported_media_types)

    def _is_configured(self) -> bool:
        return True

    async def _update_now_playing(self, report: MediaItemPlaybackProgressReport) -> None:
        self._now_playing += 1

    async def _scrobble(self, report: MediaItemPlaybackProgressReport) -> None:
        self._tracked += 1


async def test_it_does_not_scrobble_the_same_track_twice() -> None:
    """
    While songs are playing we get updates every 30 seconds.

    Here we test that songs only get scrobbled once during each play.
    """
    handler = DummyHandler(logging.getLogger())

    # not fully played yet
    await handler._on_mass_media_item_played(create_report(duration=180, seconds_played=30))
    assert handler._tracked == 0

    # fully played near the end
    await handler._on_mass_media_item_played(create_report(duration=180, seconds_played=176))
    assert handler._tracked == 1

    # fully played on track change should not scrobble again
    await handler._on_mass_media_item_played(create_report(duration=180, seconds_played=180))
    assert handler._tracked == 1

    # single song is on repeat and started playing again
    await handler._on_mass_media_item_played(create_report(duration=180, seconds_played=30))
    assert handler._tracked == 1

    # fully played for the second time
    await handler._on_mass_media_item_played(create_report(duration=180, seconds_played=179))
    assert handler._tracked == 2


async def test_it_resets_now_playing_when_songs_are_on_loop() -> None:
    """
    When a song starts playing we update the 'now playing' endpoint.

    This ends automatically, so if a single song is on repeat, we need to send the request again
    """
    handler = DummyHandler(logging.getLogger())

    # started playing, should update now_playing
    await handler._on_mass_media_item_played(create_report(duration=180, seconds_played=30))
    assert handler._now_playing == 1

    # fully played on track change should not update again
    await handler._on_mass_media_item_played(create_report(duration=180, seconds_played=180))
    assert handler._now_playing == 1

    # restarted same song, should scrobble again
    await handler._on_mass_media_item_played(create_report(duration=180, seconds_played=30))
    assert handler._now_playing == 2


async def test_it_does_not_update_now_playing_on_pause() -> None:
    """Don't update now_playing when pausing the player early in the song."""
    handler = DummyHandler(logging.getLogger())

    await handler._on_mass_media_item_played(
        create_report(duration=180, seconds_played=20, is_playing=False)
    )
    assert handler._now_playing == 0


async def test_it_filters_scrobbles_by_player() -> None:
    """Only scrobble tracks from configured players."""
    handler = DummyHandler(
        logging.getLogger(),
        ScrobblerConfig(suffix_version=False, mass_playerids=["living_room"]),
    )

    await handler._on_mass_media_item_played(
        create_report(duration=180, seconds_played=176, player_id="kitchen")
    )
    assert handler._now_playing == 0
    assert handler._tracked == 0

    await handler._on_mass_media_item_played(
        create_report(duration=180, seconds_played=176, player_id="living_room")
    )
    assert handler._now_playing == 1
    assert handler._tracked == 1


async def test_it_filters_scrobbles_without_player_context() -> None:
    """Skip scrobbling if a player filter is configured and the event has no player context."""
    handler = DummyHandler(
        logging.getLogger(),
        ScrobblerConfig(suffix_version=False, mass_playerids=["living_room"]),
    )

    await handler._on_mass_media_item_played(
        create_report(duration=180, seconds_played=176, player_id=None)
    )
    assert handler._now_playing == 0
    assert handler._tracked == 0


async def test_it_filters_unsupported_media_types() -> None:
    """Only provider supported media types should be scrobbled."""
    handler = DummyHandler(logging.getLogger(), supported_media_types=frozenset({MediaType.TRACK}))

    await handler._on_mass_media_item_played(
        create_report(
            duration=180,
            seconds_played=176,
            uri="filesystem://audiobook/1",
            media_type=MediaType.AUDIOBOOK,
        )
    )
    assert handler._now_playing == 0
    assert handler._tracked == 0


async def test_it_allows_provider_supported_media_types() -> None:
    """Providers can opt in to scrobbling additional media types."""
    handler = DummyHandler(
        logging.getLogger(),
        supported_media_types=frozenset({MediaType.TRACK, MediaType.AUDIOBOOK}),
    )

    await handler._on_mass_media_item_played(
        create_report(
            duration=180,
            seconds_played=176,
            uri="filesystem://audiobook/1",
            media_type=MediaType.AUDIOBOOK,
        )
    )
    assert handler._now_playing == 1
    assert handler._tracked == 1


async def test_it_suffixes_the_version_if_enabled_and_available() -> None:
    """Test that the track version is suffixed to the track name when enabled."""
    report_with_version = create_report(version="Deluxe Edition").data
    report_without_version = create_report(version=None).data

    handler = DummyHandler(logging.getLogger(), ScrobblerConfig(suffix_version=True))
    assert handler.get_name(report_with_version) == "track (Deluxe Edition)"
    assert handler.get_name(report_without_version) == "track"

    handler = DummyHandler(logging.getLogger(), ScrobblerConfig(suffix_version=False))
    assert handler.get_name(report_with_version) == "track"
    assert handler.get_name(report_without_version) == "track"


class _ServiceError(Exception):
    """Stand-in for a scrobble client's expected service/network error."""


class FailingHandler(DummyHandler):
    """Handler whose submissions always raise, to test exception handling."""

    scrobble_exceptions = (_ServiceError,)

    def __init__(self, logger: logging.Logger, error: Exception) -> None:
        """Initialize with the error to raise on every submission."""
        super().__init__(logger)
        self._error = error

    async def _update_now_playing(self, report: MediaItemPlaybackProgressReport) -> None:
        raise self._error

    async def _scrobble(self, report: MediaItemPlaybackProgressReport) -> None:
        raise self._error


async def test_it_swallows_expected_scrobble_exceptions() -> None:
    """Errors listed in scrobble_exceptions are logged and swallowed, leaving state untouched."""
    handler = FailingHandler(logging.getLogger(), _ServiceError("service unavailable"))

    # a fully played, playing report drives both the now_playing and scrobble paths
    await handler._on_mass_media_item_played(create_report(duration=180, seconds_played=176))

    # neither marker advances because both submissions failed before assignment
    assert handler.currently_playing is None
    assert handler.last_scrobbled is None


async def test_it_propagates_unexpected_scrobble_exceptions() -> None:
    """Errors outside scrobble_exceptions surface instead of being silently swallowed."""
    handler = FailingHandler(logging.getLogger(), ValueError("unexpected bug"))

    with pytest.raises(ValueError, match="unexpected bug"):
        await handler._on_mass_media_item_played(create_report(duration=180, seconds_played=176))


def test_it_only_offers_playback_capable_scrobble_players() -> None:
    """The scrobble-players picker only lists players that can render audio."""
    mass = mock.Mock()
    mass.players.all_players.return_value = [
        _player("wall-panel", "Wall Panel", PlayerType.DISPLAY),
        _player("living-room", "Living Room"),
        _player("turntable", "Turntable", PlayerType.SOURCE),
        _player("kitchen", "Kitchen"),
    ]

    entry = create_scrobble_players_config_entry(mass)

    assert entry.options is not None
    assert [option.value for option in entry.options] == ["kitchen", "living-room"]


def _player(
    player_id: str, display_name: str, player_type: PlayerType = PlayerType.PLAYER
) -> mock.Mock:
    """Return a minimal player for config-entry option generation."""
    player = mock.Mock()
    player.player_id = player_id
    player.display_name = display_name
    player.type = player_type
    return player


def create_report(
    duration: int = 148,
    seconds_played: int = 59,
    is_playing: bool = True,
    uri: str = "filesystem://track/1",
    version: str | None = None,
    player_id: str | None = "test_player",
    media_type: MediaType = MediaType.TRACK,
) -> MassEvent:
    """Create the MediaItemPlaybackProgressReport and wrap it in a MassEvent."""
    return wrap_event(
        MediaItemPlaybackProgressReport(
            uri=uri,
            media_type=media_type,
            name="track",
            artist=None,
            artist_mbids=None,
            album=None,
            album_mbid=None,
            image_url=None,
            duration=duration,
            mbid="",
            seconds_played=seconds_played,
            fully_played=duration - seconds_played < 5,
            is_playing=is_playing,
            version=version,
            player_id=player_id,
        )
    )


def wrap_event(data: MediaItemPlaybackProgressReport) -> MassEvent:
    """Create a MEDIA_ITEM_PLAYED event."""
    return MassEvent(EventType.MEDIA_ITEM_PLAYED, data.uri, data)
