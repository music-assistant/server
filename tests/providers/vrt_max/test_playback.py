"""Tests for VRT MAX playback: stream resolution, resume position and progress write-back."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError, UnplayableMediaError

from music_assistant.providers.vrt_max import VrtMaxProvider
from music_assistant.providers.vrt_max.helpers import (
    VrtApiError,
    VrtAuthError,
    VrtProgress,
    VrtResumeTarget,
    VrtStreamInfo,
)

EPISODE_ID = "/vrtmax/podcasts/radio-1/h/pod/1/1--ep/"


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
    """A player-token auth failure maps to UnplayableMediaError."""
    provider._auth.enabled = True  # type: ignore[misc]
    provider._client.get_stream_info = AsyncMock(  # type: ignore[method-assign]
        return_value=VrtStreamInfo("streamid", 1800)
    )
    provider._auth.get_player_token = AsyncMock(  # type: ignore[method-assign]
        side_effect=VrtAuthError("bad creds")
    )

    with pytest.raises(UnplayableMediaError):
        await provider.get_stream_details(EPISODE_ID, MediaType.PODCAST_EPISODE)


async def test_episode_stream_details_api_error(provider: VrtMaxProvider) -> None:
    """A stream-info API failure maps to MediaNotFoundError."""
    provider._auth.enabled = True  # type: ignore[misc]
    provider._client.get_stream_info = AsyncMock(  # type: ignore[method-assign]
        side_effect=VrtApiError("boom")
    )

    with pytest.raises(MediaNotFoundError):
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
    media_item = Mock(duration=1800)

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
    media_item = Mock(duration=1800)

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
    media_item = Mock(duration=1800)

    await provider.on_played(
        MediaType.RADIO, "radio1", fully_played=True, position=10, media_item=media_item
    )

    provider._client.get_resume_target.assert_not_called()  # type: ignore[attr-defined]
    provider._client.post_resume_point.assert_not_called()  # type: ignore[attr-defined]


async def test_on_played_disabled_does_nothing(provider: VrtMaxProvider) -> None:
    """Without VRT credentials, progress is not written back."""
    provider._auth.enabled = False  # type: ignore[misc]
    media_item = Mock(duration=1800)

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
    media_item = Mock(duration=1800)

    await provider.on_played(
        MediaType.PODCAST_EPISODE,
        EPISODE_ID,
        fully_played=True,
        position=10,
        media_item=media_item,
    )
