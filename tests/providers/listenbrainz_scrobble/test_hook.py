"""Tests for the ListenBrainz scrobbler's playback report hook."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport

from music_assistant.providers.listenbrainz_scrobble import (
    CONF_USER_TOKEN,
    SUPPORTED_FEATURES,
    ListenBrainzEventHandler,
    ListenBrainzScrobbleProvider,
    setup,
)


def _mass(setup_data: dict[str, str] | None = None) -> Mock:
    """Mock the server, holding the given setup data of the provider."""
    mass = Mock()
    mass.config.get.return_value = setup_data or {}
    mass.config.decrypt_string.side_effect = lambda value: value
    return mass


def _config() -> Mock:
    """Mock a provider config without values, so every option falls back to its default."""
    config = Mock()
    config.values = {}
    config.get_value.side_effect = lambda _key, default=None: default
    return config


def _provider(setup_data: dict[str, str] | None = None) -> ListenBrainzScrobbleProvider:
    """Build a ListenBrainz provider instance with the given stored setup data."""
    return ListenBrainzScrobbleProvider(
        _mass(setup_data), Mock(domain="listenbrainz_scrobble"), _config(), SUPPORTED_FEATURES
    )


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


async def test_setup_declares_the_scrobble_feature() -> None:
    """The provider tells the server it records plays, or it would never be handed any."""
    provider = await setup(_mass(), Mock(domain="listenbrainz_scrobble"), _config())

    assert isinstance(provider, ListenBrainzScrobbleProvider)
    assert ProviderFeature.SCROBBLE in provider.supported_features


async def test_the_handler_is_built_from_the_stored_token() -> None:
    """A stored user token gives the provider a handler that reports to ListenBrainz."""
    provider = _provider({CONF_USER_TOKEN: "token"})

    # the client validates the token against the service when it is set
    with patch("music_assistant.providers.listenbrainz_scrobble.ListenBrainz") as client_cls:
        await provider.handle_async_init()
    await provider.loaded_in_mass()

    client_cls.return_value.set_auth_token.assert_called_once_with("token")
    assert isinstance(provider._handler, ListenBrainzEventHandler)


async def test_the_hook_forwards_the_report_to_the_handler() -> None:
    """A report handed to the provider reaches its scrobble handler."""
    provider = _provider()
    provider._handler = Mock(on_media_item_played=AsyncMock())
    report = _report()

    await provider.on_media_item_played(report)

    provider._handler.on_media_item_played.assert_awaited_once_with(report)
