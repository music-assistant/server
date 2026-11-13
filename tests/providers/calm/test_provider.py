"""Tests for the Calm provider's mapping of Calm content to Music Assistant items."""

from __future__ import annotations

import logging

import pytest
from calm_client import CalmAPIError, CalmAuthError, CalmRateLimitError
from calm_client import models as calm_models
from music_assistant_models.enums import AlbumType, ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Album, BrowseFolder, Track

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.calm import CalmProvider

from .conftest import GUIDE_ID, PROGRAM_ID, FakeCalmClient


async def test_get_album_maps_program(provider: CalmProvider) -> None:
    """A program maps to an album with narrator, description and cover."""
    album = await provider.get_album(PROGRAM_ID)
    assert album.name == "Rain Sounds"
    assert album.album_type == AlbumType.ALBUM
    assert [a.name for a in album.artists] == ["Curated by Calm"]
    assert album.metadata.description == "Gentle rain."
    assert album.metadata.images
    assert album.metadata.images[0].path.endswith("cell.jpeg")


async def test_album_tracks_ordered_by_position(provider: CalmProvider) -> None:
    """Guides come back in position order, preferring compressed audio over wav."""
    tracks = await provider.get_album_tracks(PROGRAM_ID)
    assert [t.item_id for t in tracks] == [GUIDE_ID, "guide2"]
    assert tracks[0].track_number == 1
    assert tracks[0].duration == 1423
    mapping = next(iter(tracks[0].provider_mappings))
    assert mapping.audio_format.content_type == ContentType.MP4


async def test_get_track_resolves_program(provider: CalmProvider, api: FakeCalmClient) -> None:
    """A guide is resolved through its summary to the program that holds its audio."""
    track = await provider.get_track(GUIDE_ID)
    assert track.name == "First"
    assert isinstance(track.album, Album)
    assert track.album.item_id == PROGRAM_ID
    assert f"programs/guides/{GUIDE_ID}" in api.calls


async def test_stream_details(provider: CalmProvider) -> None:
    """Stream details point at the preferred audio asset."""
    details = await provider.get_stream_details(GUIDE_ID, MediaType.TRACK)
    assert details.stream_type == StreamType.HTTP
    assert details.path == "https://assets.calm.com/a.m4a"
    assert details.duration == 1423
    assert details.audio_format.content_type == ContentType.MP4


async def test_stream_refused_when_locked(provider: CalmProvider, api: FakeCalmClient) -> None:
    """Content Calm reports as locked for the account is not streamed."""
    api.unlocked = False
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details(GUIDE_ID, MediaType.TRACK)


async def test_search_maps_programs_and_guides(provider: CalmProvider) -> None:
    """Search splits results into albums and tracks, and marks locked items unavailable."""
    result = await provider.search("rain", [MediaType.ALBUM, MediaType.TRACK], limit=5)
    assert [a.item_id for a in result.albums] == [PROGRAM_ID]
    assert [t.item_id for t in result.tracks] == [GUIDE_ID]
    album, track = result.albums[0], result.tracks[0]
    assert isinstance(album, Album)
    assert isinstance(track, Track)
    assert not next(iter(album.provider_mappings)).available
    assert next(iter(track.provider_mappings)).available
    tracks_only = await provider.search("rain", [MediaType.TRACK])
    assert not tracks_only.albums


async def test_browse_root_lists_feeds(provider: CalmProvider) -> None:
    """The root lists the Calm feeds as folders."""
    items = await provider.browse("calm--test://")
    assert all(isinstance(i, BrowseFolder) for i in items)
    assert isinstance(items[0], BrowseFolder)
    assert items[0].path == "calm--test://feed/sleep"


async def test_browse_feed_skips_promos_and_empty_packs(provider: CalmProvider) -> None:
    """A feed lists only packs with playable content, followed by its tags."""
    items = await provider.browse("calm--test://feed/sleep")
    folders = [i for i in items if isinstance(i, BrowseFolder)]
    assert len(folders) == len(items)
    assert [i.path for i in folders] == [
        "calm--test://pack/sleep-featured",
        "calm--test://feed/sleep-music",
        "calm--test://pack/tagged-x",
    ]


async def test_browse_pack_items(provider: CalmProvider) -> None:
    """A pack lists its guides as tracks, programs as albums and sub-packs as folders."""
    items = await provider.browse("calm--test://pack/sleep-featured")
    assert isinstance(items[0], Track)
    assert items[0].item_id == GUIDE_ID
    assert items[0].album is not None
    assert items[0].album.item_id == PROGRAM_ID
    assert isinstance(items[1], Album)
    assert isinstance(items[2], BrowseFolder)
    assert items[2].path == "calm--test://pack/sub"


async def test_library_from_favorites(provider: CalmProvider) -> None:
    """The library is made of the guides and programs the user favorited."""
    albums = [a async for a in provider.get_library_albums()]
    tracks = [t async for t in provider.get_library_tracks()]
    assert [a.item_id for a in albums] == [PROGRAM_ID]
    assert [t.item_id for t in tracks] == [GUIDE_ID]


async def test_recommendations(provider: CalmProvider) -> None:
    """Home feed packs with playable content become recommendation rows."""
    rows = await provider.get_recommendations()
    assert [r.item_id for r in rows] == ["pack/sleep-featured"]
    items = await provider.get_recommendation_items(rows[0].item_id)
    assert len(items) == 3


async def test_call_logs_in_again_when_session_expired(
    provider: CalmProvider, api: FakeCalmClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expired session triggers one fresh login and a retry of the call."""
    logins: list[bool] = []

    async def fake_login() -> None:
        logins.append(True)

    async def close() -> None:
        return None

    monkeypatch.setattr(provider, "_login", fake_login)
    monkeypatch.setattr(api, "close", close, raising=False)
    attempts: list[bool] = []

    async def flaky() -> str:
        attempts.append(True)
        if len(attempts) == 1:
            raise CalmAuthError("expired")
        return "ok"

    assert await provider._call(flaky) == "ok"
    assert len(attempts) == 2
    assert logins == [True]


async def test_rate_limited_call_is_retried(provider: CalmProvider) -> None:
    """A 429 from Calm is retried through the throttler instead of failing the call."""
    provider.throttler = ThrottlerManager(rate_limit=100, period=1, initial_backoff=0)
    attempts: list[bool] = []

    async def limited_once() -> str:
        attempts.append(True)
        if len(attempts) == 1:
            raise CalmRateLimitError("slow down", retry_after=0)
        return "ok"

    assert await provider._call(limited_once) == "ok"
    assert len(attempts) == 2


async def test_other_api_errors_are_not_retried(provider: CalmProvider) -> None:
    """Errors that are not rate limits reach the caller on the first attempt."""
    attempts: list[bool] = []

    async def failing() -> str:
        attempts.append(True)
        raise CalmAPIError("boom")

    with pytest.raises(CalmAPIError):
        await provider._call(failing)
    assert len(attempts) == 1


@pytest.mark.parametrize(
    ("setting", "expected_url", "expected_type"),
    [
        ("compact", "https://assets.calm.com/a.m4a", ContentType.MP4),
        ("balanced", "https://assets.calm.com/a.oga", ContentType.OGG),
        ("highest", "https://assets.calm.com/a.wav", ContentType.WAV),
        ("nonsense", "https://assets.calm.com/a.m4a", ContentType.MP4),
        (None, "https://assets.calm.com/a.m4a", ContentType.MP4),
    ],
)
async def test_audio_quality_setting_picks_the_format(
    provider: CalmProvider,
    monkeypatch: pytest.MonkeyPatch,
    setting: str | None,
    expected_url: str,
    expected_type: ContentType,
) -> None:
    """The configured quality decides which asset streams; unknown values mean compact."""
    monkeypatch.setattr(
        provider.config,
        "get_value",
        lambda key, default=None: {"audio_quality": setting}.get(key, default),
    )
    details = await provider.get_stream_details(GUIDE_ID, MediaType.TRACK)
    assert details.path == expected_url
    assert details.audio_format.content_type == expected_type
    tracks = await provider.get_album_tracks(PROGRAM_ID)
    mapping = next(iter(tracks[0].provider_mappings))
    assert mapping.audio_format.content_type == expected_type


async def test_audio_quality_config_entry(provider: CalmProvider) -> None:
    """The quality option offers every library tier and defaults to compact."""
    entries = {entry.key: entry for entry in await provider.get_config_entries()}
    entry = entries["audio_quality"]
    assert entry.default_value == "compact"
    assert entry.options is not None
    assert [option.value for option in entry.options] == ["compact", "balanced", "highest"]


async def test_unknown_audio_format_is_reported_in_the_log(
    provider: CalmProvider, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A format the library does not know is logged, and the item still maps."""
    monkeypatch.setattr(calm_models, "_warned_audio_types", set())
    caplog.set_level(logging.WARNING, logger="calm_client")
    item = calm_models.PackItem.model_validate(
        {
            "type": "guide",
            "guide_id": GUIDE_ID,
            "title": "First",
            "is_unlocked": True,
            "assets": [{"url": "https://assets.calm.com/a.flac", "content_type": "audio/flac"}],
        }
    )
    track = provider._parse_feed_guide(item)
    assert track.item_id == GUIDE_ID
    messages = [r.getMessage() for r in caplog.records if r.name == "calm_client"]
    assert len(messages) == 1
    assert "unknown_audio_format" in messages[0]
    assert "audio/flac" in messages[0]
