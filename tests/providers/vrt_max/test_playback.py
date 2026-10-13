"""Tests for VRT MAX playback: stream resolution, resume position and progress write-back."""

from __future__ import annotations

import logging
from typing import Any, Self
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
    UnplayableMediaError,
)
from music_assistant_models.media_items import (
    ItemMapping,
    PodcastEpisode,
    ProviderMapping,
)

from music_assistant.providers.vrt_max.api_client import VrtMaxClient
from music_assistant.providers.vrt_max.models import (
    VrtApiError,
    VrtAuthError,
    VrtProgress,
    VrtResumeTarget,
    VrtStreamInfo,
)
from music_assistant.providers.vrt_max.provider import VrtMaxProvider

EPISODE_ID = "/vrtmax/podcasts/radio-1/h/pod/1/1--ep/"


def _episode(duration: int) -> PodcastEpisode:
    """Build a minimal PodcastEpisode, as MA hands one to on_played."""
    return PodcastEpisode(
        item_id=EPISODE_ID,
        provider="vrt_max--test",
        name="Ep",
        position=1,
        duration=duration,
        provider_mappings={
            ProviderMapping(
                item_id=EPISODE_ID,
                provider_domain="vrt_max",
                provider_instance="vrt_max--test",
            )
        },
        podcast=ItemMapping(
            media_type=MediaType.PODCAST,
            item_id="/vrtmax/podcasts/radio-1/h/pod/",
            provider="vrt_max--test",
            name="Pod",
        ),
    )


async def test_episode_stream_details_requires_auth(provider: VrtMaxProvider) -> None:
    """On-demand playback without VRT credentials raises UnplayableMediaError."""
    provider._auth.enabled = False  # type: ignore[misc]

    with pytest.raises(UnplayableMediaError):
        await provider.get_stream_details(EPISODE_ID, MediaType.PODCAST_EPISODE)


async def test_episode_stream_details_success(provider: VrtMaxProvider) -> None:
    """A resolved on-demand episode yields a seekable HLS StreamDetails."""
    provider._auth.enabled = True  # type: ignore[misc]
    provider._client.get_stream_info = AsyncMock(  # type: ignore[method-assign]
        return_value=VrtStreamInfo("streamid", 1800)
    )
    provider._auth.get_player_token = AsyncMock(  # type: ignore[method-assign]
        return_value="ptok"
    )
    provider._client.resolve_ondemand_hls = AsyncMock(  # type: ignore[method-assign]
        return_value="https://x/nodrm.m3u8"
    )

    details = await provider.get_stream_details(EPISODE_ID, MediaType.PODCAST_EPISODE)

    assert details.stream_type == StreamType.HLS
    assert details.path == "https://x/nodrm.m3u8"
    assert details.can_seek is True
    assert details.duration == 1800
    # Multi-instance: the stream is scoped to this instance, not the shared domain.
    assert details.provider == provider.instance_id


async def test_episode_stream_details_auth_error(provider: VrtMaxProvider) -> None:
    """A player-token auth failure surfaces as LoginFailed."""
    provider._auth.enabled = True  # type: ignore[misc]
    provider._client.get_stream_info = AsyncMock(  # type: ignore[method-assign]
        return_value=VrtStreamInfo("streamid", 1800)
    )
    provider._auth.get_player_token = AsyncMock(  # type: ignore[method-assign]
        side_effect=VrtAuthError("bad creds")
    )

    with pytest.raises(LoginFailed):
        await provider.get_stream_details(EPISODE_ID, MediaType.PODCAST_EPISODE)


async def test_episode_stream_details_api_error(provider: VrtMaxProvider) -> None:
    """A transient stream-info failure surfaces as ResourceTemporarilyUnavailable."""
    provider._auth.enabled = True  # type: ignore[misc]
    provider._client.get_stream_info = AsyncMock(  # type: ignore[method-assign]
        side_effect=VrtApiError("boom")
    )

    with pytest.raises(ResourceTemporarilyUnavailable):
        await provider.get_stream_details(EPISODE_ID, MediaType.PODCAST_EPISODE)


async def test_get_stream_details_unsupported_media_type(provider: VrtMaxProvider) -> None:
    """An unsupported media type raises UnplayableMediaError."""
    with pytest.raises(UnplayableMediaError):
        await provider.get_stream_details("x", MediaType.ARTIST)


async def test_get_resume_position_enabled(provider: VrtMaxProvider) -> None:
    """get_resume_position returns the user's VRT progress in milliseconds."""
    provider._auth.enabled = True  # type: ignore[misc]
    provider._auth.get_access_token = AsyncMock(  # type: ignore[method-assign]
        return_value="tok"
    )
    provider._client.get_progress = AsyncMock(  # type: ignore[method-assign]
        return_value=VrtProgress(completed=True, position=30)
    )

    result = await provider.get_resume_position(EPISODE_ID, MediaType.PODCAST_EPISODE)

    assert result == (True, 30000, None)


async def test_get_resume_position_disabled_raises_not_implemented(
    provider: VrtMaxProvider,
) -> None:
    """Without VRT credentials, resume position is not implemented."""
    provider._auth.enabled = False  # type: ignore[misc]

    with pytest.raises(NotImplementedError):
        await provider.get_resume_position(EPISODE_ID, MediaType.PODCAST_EPISODE)


async def test_get_resume_position_wrong_media_type_raises_not_implemented(
    provider: VrtMaxProvider,
) -> None:
    """Only podcast episodes support VRT resume tracking."""
    provider._auth.enabled = True  # type: ignore[misc]

    with pytest.raises(NotImplementedError):
        await provider.get_resume_position("radio1", MediaType.RADIO)


async def test_get_resume_position_error_maps_to_not_implemented(
    provider: VrtMaxProvider,
) -> None:
    """An auth/API failure while reading progress is reported as not implemented."""
    provider._auth.enabled = True  # type: ignore[misc]
    provider._auth.get_access_token = AsyncMock(  # type: ignore[method-assign]
        side_effect=VrtAuthError("bad")
    )

    with pytest.raises(NotImplementedError):
        await provider.get_resume_position(EPISODE_ID, MediaType.PODCAST_EPISODE)


async def test_on_played_fully_played_writes_total_duration(provider: VrtMaxProvider) -> None:
    """A fully played episode writes the total duration as the resume position."""
    provider._auth.enabled = True  # type: ignore[misc]
    provider._auth.get_access_token = AsyncMock(  # type: ignore[method-assign]
        return_value="tok"
    )
    target = VrtResumeTarget("mid", "name", 1800)
    provider._client.get_resume_target = AsyncMock(  # type: ignore[method-assign]
        return_value=target
    )
    provider._client.post_resume_point = AsyncMock()  # type: ignore[method-assign]
    media_item = _episode(duration=1800)

    await provider.on_played(
        MediaType.PODCAST_EPISODE,
        EPISODE_ID,
        fully_played=True,
        position=10,
        media_item=media_item,
    )

    provider._client.post_resume_point.assert_awaited_once_with(target, 1800, "tok", total=1800)


async def test_on_played_partial_writes_actual_position(provider: VrtMaxProvider) -> None:
    """A partially played episode writes the actual playback position."""
    provider._auth.enabled = True  # type: ignore[misc]
    provider._auth.get_access_token = AsyncMock(  # type: ignore[method-assign]
        return_value="tok"
    )
    target = VrtResumeTarget("mid", "name", 1800)
    provider._client.get_resume_target = AsyncMock(  # type: ignore[method-assign]
        return_value=target
    )
    provider._client.post_resume_point = AsyncMock()  # type: ignore[method-assign]
    media_item = _episode(duration=1800)

    await provider.on_played(
        MediaType.PODCAST_EPISODE,
        EPISODE_ID,
        fully_played=False,
        position=10,
        media_item=media_item,
    )

    provider._client.post_resume_point.assert_awaited_once_with(target, 10, "tok", total=1800)


async def test_on_played_ignores_non_episode_media_type(provider: VrtMaxProvider) -> None:
    """Progress is only written back for podcast episodes."""
    provider._auth.enabled = True  # type: ignore[misc]
    media_item = _episode(duration=1800)

    await provider.on_played(
        MediaType.RADIO, "radio1", fully_played=True, position=10, media_item=media_item
    )

    provider._client.get_resume_target.assert_not_called()  # type: ignore[attr-defined]
    provider._client.post_resume_point.assert_not_called()  # type: ignore[attr-defined]


async def test_on_played_disabled_does_nothing(provider: VrtMaxProvider) -> None:
    """Without VRT credentials, progress is not written back."""
    provider._auth.enabled = False  # type: ignore[misc]
    media_item = _episode(duration=1800)

    await provider.on_played(
        MediaType.PODCAST_EPISODE,
        EPISODE_ID,
        fully_played=True,
        position=10,
        media_item=media_item,
    )

    provider._client.get_resume_target.assert_not_called()  # type: ignore[attr-defined]


async def test_on_played_swallows_errors(provider: VrtMaxProvider) -> None:
    """A failure while writing progress back is swallowed, not raised."""
    provider._auth.enabled = True  # type: ignore[misc]
    provider._auth.get_access_token = AsyncMock(  # type: ignore[method-assign]
        side_effect=VrtAuthError("bad")
    )
    media_item = _episode(duration=1800)

    await provider.on_played(
        MediaType.PODCAST_EPISODE,
        EPISODE_ID,
        fully_played=True,
        position=10,
        media_item=media_item,
    )


class _FakeResponse:
    """Minimal stand-in for an aiohttp response used as an async context manager."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.status = 200
        self._payload = payload

    async def json(self) -> dict[str, Any]:
        return self._payload

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _client_returning(payload: dict[str, Any]) -> VrtMaxClient:
    """Return a client whose aggregator call yields the given payload."""
    session = Mock()
    session.get = Mock(return_value=_FakeResponse(payload))
    return VrtMaxClient(session, logging.getLogger("test"))


async def test_resolve_ondemand_hls_returns_the_drm_free_variant() -> None:
    """The DRM-free rendition is picked out of the aggregator's target list."""
    client = _client_returning(
        {
            "targetUrls": [
                {"type": "hls", "url": "https://x/aud-1_drm_1.m3u8"},
                {"type": "hls", "url": "https://x/aud-1_nodrm_1.m3u8"},
            ]
        }
    )

    assert await client.resolve_ondemand_hls("pub$aud", "ptok") == "https://x/aud-1_nodrm_1.m3u8"


async def test_resolve_ondemand_hls_rejects_drm_only_response() -> None:
    """With no DRM-free rendition on offer, resolution fails instead of returning one."""
    client = _client_returning({"targetUrls": [{"type": "hls", "url": "https://x/drm.m3u8"}]})

    # Returning the DRM target would need a decryption key we neither hold nor are
    # entitled to, so playback would fail anyway, with a far less obvious reason.
    with pytest.raises(MediaNotFoundError):
        await client.resolve_ondemand_hls("pub$aud", "ptok")


async def test_resolve_ondemand_hls_rejects_empty_target_list() -> None:
    """An aggregator response with no HLS targets at all fails too."""
    client = _client_returning({"targetUrls": []})

    with pytest.raises(MediaNotFoundError):
        await client.resolve_ondemand_hls("pub$aud", "ptok")
