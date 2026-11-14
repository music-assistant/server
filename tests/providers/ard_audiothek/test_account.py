"""Test the ARD account features of the ARD Audiothek provider."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import PodcastEpisode

from music_assistant.constants import CONF_PASSWORD
from music_assistant.providers.ard_audiothek import (
    CONF_EXPIRY_TIME,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_BEARER,
    CONF_USERID,
    SUPPORTED_FEATURES,
    ARDAudiothek,
    setup,
)
from music_assistant.providers.ard_audiothek.auth import ArdTokens, PlayHistoryEntry

MODULE = "music_assistant.providers.ard_audiothek"
CORE_ID = "urn:ard:publication:ff44198a1a2d64b5"
ASSET_ID = "urn:ard:episode:8d38a19c5de2bdc8"
CONFIG = {"log_level": "INFO", "max_num_episodes": 0, "podcast_finished_time": 95}


def _provider(asset_id: str | None = ASSET_ID) -> ARDAudiothek:
    """Return a signed in provider whose API knows the given asset id for every episode."""
    config = Mock(instance_id="ard_audiothek--test", enabled=True)
    config.get_value.side_effect = lambda key, default=None: CONFIG.get(key, default)
    provider = ARDAudiothek(Mock(), Mock(domain="ard_audiothek"), config, SUPPORTED_FEATURES)
    provider.token = "id-token"
    provider.user_id = "uid"
    provider.remote_progress = {}
    provider._asset_ids = {}
    provider._progress_fetched_at = -60

    session = Mock(
        execute=AsyncMock(
            return_value={"itemByCoreId": {"assetId": asset_id} if asset_id else None}
        )
    )
    client = Mock(__aenter__=AsyncMock(return_value=session), __aexit__=AsyncMock())
    provider.get_client = AsyncMock(return_value=client)  # type: ignore[method-assign]
    return provider


def _episode(duration: int = 1000) -> Mock:
    """Return a podcast episode of the given duration in seconds."""
    return Mock(spec=PodcastEpisode, duration=duration)


def _setup_provider(setup_data: dict[str, Any]) -> ARDAudiothek:
    """Return a provider with the given setup data, for testing the client creation."""
    config = Mock(instance_id="ard_audiothek--test", enabled=True)
    config.get_value.side_effect = lambda key, default=None: CONFIG.get(key, default)
    provider = ARDAudiothek(Mock(), Mock(domain="ard_audiothek"), config, SUPPORTED_FEATURES)
    provider.get_setup_value = Mock(  # type: ignore[method-assign]
        side_effect=lambda key, default=None: setup_data.get(key, default)
    )
    provider._update_setup_data = Mock()  # type: ignore[method-assign]
    provider._client_initialized = False
    return provider


async def test_expired_token_is_refreshed() -> None:
    """An expired ID token is replaced using the refresh token, and the new one is stored."""
    provider = _setup_provider(
        {
            CONF_REFRESH_TOKEN: "refresh-token",
            CONF_TOKEN_BEARER: "old-token",
            CONF_USERID: "uid",
            CONF_EXPIRY_TIME: "0",
        }
    )
    tokens = ArdTokens("new-token", "new-refresh", "uid", "Jane", "jane@example.com", 5000.0)

    with (
        patch(f"{MODULE}.refresh_tokens", AsyncMock(return_value=tokens)) as refresh,
        patch(f"{MODULE}.Client"),
        patch(f"{MODULE}._create_aiohttptransport") as transport,
    ):
        await provider.get_client()

    refresh.assert_awaited_once_with(provider.mass.http_session, "refresh-token")
    assert provider.token == "new-token"
    update_setup_data = cast("Mock", provider._update_setup_data)
    update_setup_data.assert_any_call(CONF_REFRESH_TOKEN, "new-refresh")
    update_setup_data.assert_any_call(CONF_EXPIRY_TIME, "4940.0")
    transport.assert_called_once_with({"Authorization": "Bearer new-token"})


async def test_valid_token_is_not_refreshed() -> None:
    """A token that has not expired yet is used as is."""
    provider = _setup_provider(
        {
            CONF_REFRESH_TOKEN: "refresh-token",
            CONF_TOKEN_BEARER: "token",
            CONF_USERID: "uid",
            CONF_EXPIRY_TIME: str(time.time() + 600),
        }
    )

    with (
        patch(f"{MODULE}.refresh_tokens", AsyncMock()) as refresh,
        patch(f"{MODULE}.Client"),
        patch(f"{MODULE}._create_aiohttptransport"),
    ):
        await provider.get_client()

    refresh.assert_not_awaited()


async def test_password_login_requires_signing_in_again() -> None:
    """An account set up with the former password login has to be signed in again."""
    provider = _setup_provider({CONF_PASSWORD: "secret"})

    with pytest.raises(LoginFailed, match="sign in"):
        await provider.get_client()


async def test_library_podcasts_skip_unavailable_ones() -> None:
    """A subscribed podcast ARD cannot serve is reported and the others are still listed."""
    provider = _provider()
    podcast = Mock()
    provider.get_podcast = AsyncMock(  # type: ignore[method-assign]
        side_effect=[MediaNotFoundError("gone"), podcast]
    )
    provider.report_skipped_sync_item = Mock()  # type: ignore[method-assign]

    with patch(f"{MODULE}.get_subscribed_podcast_ids", AsyncMock(return_value=["a", "b"])):
        podcasts = [item async for item in provider.get_library_podcasts()]

    assert podcasts == [podcast]
    provider.report_skipped_sync_item.assert_called_once()
    assert provider.report_skipped_sync_item.call_args.args[:2] == (MediaType.PODCAST, "a")


async def test_progress_is_stored_under_the_asset_id() -> None:
    """The play history is keyed by the episode's asset id, not by its core id."""
    provider = _provider()

    with patch(f"{MODULE}.set_play_history_entry", AsyncMock()) as store:
        await provider.on_played(MediaType.PODCAST_EPISODE, CORE_ID, False, 85, _episode())

    store.assert_awaited_once_with(
        provider.mass.http_session, "id-token", "uid", ASSET_ID, 85, False
    )
    assert provider._get_progress(CORE_ID) == (False, 85000)


@pytest.mark.parametrize(
    ("position", "fully_played", "completed"),
    [(950, False, True), (400, True, True), (400, False, False)],
    ids=["threshold", "fully-played", "in-progress"],
)
async def test_progress_marks_finished_episodes(
    position: int, fully_played: bool, completed: bool
) -> None:
    """An episode counts as finished once played to the end or past the configured share."""
    provider = _provider()

    with patch(f"{MODULE}.set_play_history_entry", AsyncMock()) as store:
        await provider.on_played(
            MediaType.PODCAST_EPISODE, CORE_ID, fully_played, position, _episode(1000)
        )

    assert store.call_args.args[5] is completed


@pytest.mark.parametrize("asset_id", [ASSET_ID, None], ids=["too-short", "no-asset-id"])
async def test_progress_is_not_always_stored(asset_id: str | None) -> None:
    """Short plays and episodes without an asset id are left out of the play history."""
    provider = _provider(asset_id)
    position = 20 if asset_id else 85

    with patch(f"{MODULE}.set_play_history_entry", AsyncMock()) as store:
        await provider.on_played(MediaType.PODCAST_EPISODE, CORE_ID, False, position, _episode())

    store.assert_not_awaited()


async def test_unplayed_episode_leaves_the_play_history() -> None:
    """An episode marked as unplayed is removed from the play history."""
    provider = _provider()
    provider.remote_progress = {ASSET_ID: PlayHistoryEntry(True, 1000.0, None)}

    with (
        patch(f"{MODULE}.remove_play_history_entry", AsyncMock()) as remove,
        patch(f"{MODULE}.set_play_history_entry", AsyncMock()) as store,
    ):
        await provider.on_played(MediaType.PODCAST_EPISODE, CORE_ID, False, 0, _episode())

    remove.assert_awaited_once_with(provider.mass.http_session, "id-token", "uid", ASSET_ID)
    store.assert_not_awaited()
    assert ASSET_ID not in provider.remote_progress


async def test_starting_playback_keeps_the_play_history() -> None:
    """Position 0 while the episode is playing is no request to mark it as unplayed."""
    provider = _provider()

    with patch(f"{MODULE}.remove_play_history_entry", AsyncMock()) as remove:
        await provider.on_played(
            MediaType.PODCAST_EPISODE, CORE_ID, False, 0, _episode(), is_playing=True
        )

    remove.assert_not_awaited()


async def test_resume_position_comes_from_the_play_history() -> None:
    """Right before playback the play history is read again, with its timestamp."""
    provider = _provider()
    provider._progress_fetched_at = time.monotonic()
    updated_on = datetime(2026, 11, 13, 21, 0, tzinfo=UTC)
    history = {ASSET_ID: PlayHistoryEntry(False, 330.0, updated_on)}

    with patch(f"{MODULE}.get_play_history", AsyncMock(return_value=history)) as read:
        result = await provider.get_resume_position(CORE_ID, MediaType.PODCAST_EPISODE)

    read.assert_awaited_once()
    assert result == (False, 330000, updated_on)


async def test_episode_listings_reuse_the_play_history() -> None:
    """Listing episodes reads the play history at most once a minute."""
    provider = _provider()

    with patch(f"{MODULE}.get_play_history", AsyncMock(return_value={})) as read:
        await provider._update_progress()
        await provider._update_progress()

    read.assert_awaited_once()


@pytest.mark.parametrize("refresh_token", ["refresh-token", None], ids=["signed-in", "anonymous"])
async def test_subscriptions_are_editable_when_signed_in(refresh_token: str | None) -> None:
    """Only a provider linked to an ARD account changes subscriptions in that account."""
    mass = Mock()
    mass.config.get_provider_setup_value.return_value = refresh_token
    config = Mock(instance_id="ard_audiothek--test", enabled=True)
    config.get_value.side_effect = lambda key, default=None: CONFIG.get(key, default)

    provider = await setup(mass, Mock(domain="ard_audiothek"), config)

    editable = ProviderFeature.LIBRARY_PODCASTS_EDIT in provider.supported_features
    assert editable is bool(refresh_token)


async def test_library_add_and_remove_change_the_subscriptions() -> None:
    """Adding a podcast to the library subscribes to it, removing it unsubscribes."""
    provider = _provider()
    podcast = Mock(media_type=MediaType.PODCAST, item_id="urn:ard:show:2af809299201a921")

    with (
        patch(f"{MODULE}.add_subscription", AsyncMock()) as add,
        patch(f"{MODULE}.remove_subscription", AsyncMock()) as remove,
    ):
        assert await provider.library_add(podcast) is True
        assert await provider.library_remove(podcast.item_id, MediaType.PODCAST) is True

    args = (provider.mass.http_session, "id-token", "uid", "urn:ard:show:2af809299201a921")
    add.assert_awaited_once_with(*args)
    remove.assert_awaited_once_with(*args)
