"""Tests the event handling for LastFM Plugin Provider."""

import logging

from music_assistant_models.enums import EventType, MediaType
from music_assistant_models.event import MassEvent
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport

from music_assistant.helpers.scrobbler import ScrobblerConfig, ScrobblerHelper


class DummyHandler(ScrobblerHelper):
    """Spy version of a ScrobblerHelper to allow easy testing."""

    _tracked = 0
    _now_playing = 0

    def __init__(self, logger: logging.Logger, config: ScrobblerConfig | None = None) -> None:
        """Initialize."""
        super().__init__(logger, config)

    def _is_configured(self) -> bool:
        return True

    async def _update_now_playing(self, report: MediaItemPlaybackProgressReport) -> None:
        self._now_playing += 1

    async def _scrobble(self, report: MediaItemPlaybackProgressReport) -> None:
        self._tracked += 1


async def test_it_does_not_scrobble_the_same_track_twice() -> None:
    """While songs are playing we get updates every 30 seconds.

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
    """When a song starts playing we update the 'now playing' endpoint.

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


def create_report(
    duration: int = 148,
    seconds_played: int = 59,
    is_playing: bool = True,
    uri: str = "filesystem://track/1",
    version: str | None = None,
) -> MassEvent:
    """Create the MediaItemPlaybackProgressReport and wrap it in a MassEvent."""
    return wrap_event(
        MediaItemPlaybackProgressReport(
            uri=uri,
            media_type=MediaType.TRACK,
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
        )
    )


def wrap_event(data: MediaItemPlaybackProgressReport) -> MassEvent:
    """Create a MEDIA_ITEM_PLAYED event."""
    return MassEvent(EventType.MEDIA_ITEM_PLAYED, data.uri, data)
