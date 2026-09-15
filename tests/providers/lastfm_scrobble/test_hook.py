"""Tests for the Last.fm scrobbler's playback report hook."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, Mock, patch

import pylast
import pytest
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import LoginFailed
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


async def _authenticated_provider(network: Mock) -> LastFMScrobbleProvider:
    """Build a loaded Last.fm provider that submits to the given pylast network."""
    provider = _provider()
    provider._network = network
    await provider.loaded_in_mass()
    return provider


def _network() -> Mock:
    """Build a mock pylast network of the Last.fm service."""
    network = Mock()
    network.name = "Last.fm"
    return network


def _report(is_playing: bool = False) -> MediaItemPlaybackProgressReport:
    """Build a playback progress report for a fully played track."""
    return MediaItemPlaybackProgressReport(
        uri="library://track/1",
        media_type=MediaType.TRACK,
        name="track",
        duration=180,
        seconds_played=180,
        fully_played=True,
        is_playing=is_playing,
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


@pytest.mark.parametrize(
    ("failing_call", "is_playing"), [("update_now_playing", True), ("scrobble", False)]
)
@pytest.mark.parametrize("status", [str(pylast.STATUS_AUTH_FAILED), str(pylast.STATUS_INVALID_SK)])
async def test_a_rejected_session_stops_scrobbling_and_asks_for_reauth(
    status: str, failing_call: str, is_playing: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """A rejected session logs one warning, stops all submissions and unloads for re-auth."""
    network = _network()
    getattr(network, failing_call).side_effect = pylast.WSError(
        network, status, "Invalid session key - Please re-authenticate"
    )
    provider = await _authenticated_provider(network)

    with patch.object(provider, "unload_with_error") as unload_with_error:
        await provider.on_media_item_played(_report(is_playing=is_playing))
        await provider.on_media_item_played(_report(is_playing=is_playing))

    # the rejected call is not retried and nothing else is submitted after it
    getattr(network, failing_call).assert_called_once()
    assert network.update_now_playing.call_count + network.scrobble.call_count == 1
    assert provider._handler is None
    unload_with_error.assert_called_once()
    err = unload_with_error.call_args.args[0]
    assert isinstance(err, LoginFailed)
    assert err.translation_key == "session_invalid"
    assert err.translation_owner == "provider.lastfm_scrobble"
    assert err.translation_args == ["Last.fm"]
    assert [r.levelno for r in caplog.records if r.levelno >= logging.WARNING] == [logging.WARNING]


@pytest.mark.parametrize("status", ["16", 503])
async def test_a_transient_error_keeps_scrobbling(status: str | int) -> None:
    """A transient Last.fm error leaves the provider loaded, so the next report retries it."""
    network = _network()
    network.update_now_playing.side_effect = pylast.WSError(network, status, "Service Unavailable")
    provider = await _authenticated_provider(network)

    with patch.object(provider, "unload_with_error") as unload_with_error:
        await provider.on_media_item_played(_report(is_playing=True))
        await provider.on_media_item_played(_report(is_playing=True))

    assert network.update_now_playing.call_count == 2
    assert provider._handler is not None
    unload_with_error.assert_not_called()
