"""Tests the event handling for LastFM Plugin Provider."""

import logging

from music_assistant_models.enums import EventType, MediaType
from music_assistant_models.event import MassEvent
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport

from music_assistant.helpers.scrobbler import ScrobblerHelper


class DummyHandler(ScrobblerHelper):
    """Spy version of a ScrobblerHelper to allow easy testing."""

    _tracked = 0
    _now_playing = 0

    def __init__(self, logger: logging.Logger) -> None:
        """Initialize."""
        super().__init__(logger)

    def _is_configured(self) -> bool:
        return True

    def _update_now_playing(self, report: MediaItemPlaybackProgressReport) -> None:
        self._now_playing += 1

    def _scrobble(self, report: MediaItemPlaybackProgressReport) -> None:
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


def create_report(
    duration: int, seconds_played: int, is_playing: bool = True, uri: str = "filesystem://track/1"
) -> MassEvent:
    """Create the MediaItemPlaybackProgressReport and wrap it in a MassEvent."""
    return wrap_event(
        MediaItemPlaybackProgressReport(
            uri=uri,
            media_type=MediaType.TRACK,
            name="track",
            artist=None,
            album=None,
            image_url=None,
            duration=duration,
            mbid="",
            seconds_played=seconds_played,
            fully_played=duration - seconds_played < 5,
            is_playing=is_playing,
        )
    )


def wrap_event(data: MediaItemPlaybackProgressReport) -> MassEvent:
    """Create a MEDIA_ITEM_PLAYED event."""
    return MassEvent(EventType.MEDIA_ITEM_PLAYED, data.uri, data)
