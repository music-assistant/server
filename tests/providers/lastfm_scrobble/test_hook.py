"""Tests for the Last.fm scrobbler's playback report hook."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport

from music_assistant.providers.lastfm_scrobble import (
    SUPPORTED_FEATURES,
    LastFMScrobbleProvider,
)


def _provider() -> LastFMScrobbleProvider:
    """Build a Last.fm provider instance without any stored setup data."""
    mass = Mock()
    mass.config.get.return_value = {}
    config = Mock()
    config.values = {}
    config.get_value.side_effect = lambda _key, default=None: default
    return LastFMScrobbleProvider(mass, Mock(domain="lastfm_scrobble"), config, SUPPORTED_FEATURES)


def _report() -> MediaItemPlaybackProgressReport:
    """Build a playback progress report for a fully played track."""
    return MediaItemPlaybackProgressReport(
        uri="library://track/1",
        media_type=MediaType.TRACK,
        name="track",
        duration=180,
        seconds_played=180,
        fully_played=True,
        is_playing=False,
    )


async def test_reports_are_ignored_without_an_authenticated_account() -> None:
    """Without a session key there is nothing to scrobble to."""
    provider = _provider()

    await provider.handle_async_init()
    await provider.loaded_in_mass()
    await provider.on_media_item_played(_report())

    assert provider._handler is None
    assert ProviderFeature.SCROBBLE in provider.supported_features


async def test_the_hook_forwards_the_report_to_the_handler() -> None:
    """An authenticated provider hands the report to its scrobble handler."""
    provider = _provider()
    provider._handler = Mock(on_media_item_played=AsyncMock())
    report = _report()

    await provider.on_media_item_played(report)

    provider._handler.on_media_item_played.assert_awaited_once_with(report)
