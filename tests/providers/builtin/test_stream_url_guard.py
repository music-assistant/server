"""Tests that the builtin provider refuses local filesystem paths."""

from __future__ import annotations

import os
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    Track,
    UniqueList,
)

import music_assistant
from music_assistant.constants import MASS_LOGO, VARIOUS_ARTISTS_FANART
from music_assistant.providers.builtin import BuiltinProvider

LOCAL_PATHS = [
    "/etc/passwd",
    "/home/other/private/track.flac",
    "../../secret.mp3",
    "file:///etc/passwd",
    "relative/path.mp3",
    "",
]


def _make_provider() -> BuiltinProvider:
    """Return a BuiltinProvider instance with mocked collaborators."""
    provider = BuiltinProvider.__new__(BuiltinProvider)
    provider.mass = MagicMock()
    provider.mass.cache_path = "/data/cache"
    provider.logger = MagicMock()
    provider.manifest = MagicMock(domain="builtin")
    provider.config = MagicMock(instance_id="builtin_1")
    return provider


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/stream.mp3",
        "https://example.com/stream.mp3",
        "rtsp://example.com/stream",
        "rtmp://example.com/stream",
    ],
)
def test_ensure_stream_url_accepts_remote_schemes(url: str) -> None:
    """Remote stream URLs pass the guard unchanged."""
    BuiltinProvider._ensure_stream_url(url)


@pytest.mark.parametrize("item_id", LOCAL_PATHS)
def test_ensure_stream_url_rejects_local_paths(item_id: str) -> None:
    """Anything that is not a remote stream URL is refused."""
    with pytest.raises(MediaNotFoundError):
        BuiltinProvider._ensure_stream_url(item_id)


@pytest.mark.asyncio
async def test_get_media_info_rejects_local_path_before_probing() -> None:
    """The sink guard fires before any cache lookup or ffprobe read."""
    provider = _make_provider()
    provider._resolve_url = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(MediaNotFoundError):
        await provider._get_media_info("/etc/passwd")

    cast("Any", provider.mass).cache.get.assert_not_called()
    cast("Any", provider._resolve_url).assert_not_called()


@pytest.mark.asyncio
async def test_get_stream_details_rejects_local_path() -> None:
    """Playback of a local path is blocked at the stream layer."""
    provider = _make_provider()
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("/etc/passwd", MediaType.TRACK)


@pytest.mark.asyncio
async def test_parse_item_rejects_local_path() -> None:
    """Resolving a local path (get_track/get_radio/enqueue) is blocked."""
    provider = _make_provider()
    with pytest.raises(MediaNotFoundError):
        await provider.parse_item("/etc/passwd", requested_media_type=MediaType.TRACK)


@pytest.mark.asyncio
async def test_add_track_rejects_local_path_without_storing() -> None:
    """add_track refuses a local path before writing anything to config."""
    provider = _make_provider()
    with pytest.raises(MediaNotFoundError):
        await provider.add_track("/etc/passwd", "Passwords")
    cast("Any", provider.mass).config.set.assert_not_called()


@pytest.mark.asyncio
async def test_add_radio_rejects_local_path_without_storing() -> None:
    """add_radio refuses a local path before writing anything to config."""
    provider = _make_provider()
    with pytest.raises(MediaNotFoundError):
        await provider.add_radio("/etc/passwd", "Passwords")
    cast("Any", provider.mass).config.set.assert_not_called()


@pytest.mark.asyncio
async def test_add_track_stores_a_stream_url() -> None:
    """A remote stream URL is accepted and persisted."""
    provider = _make_provider()
    cast("Any", provider.mass).config.get.return_value = []
    provider.get_track = AsyncMock(return_value=MagicMock(spec=Track))  # type: ignore[method-assign]

    await provider.add_track("http://example.com/song.mp3", "Song")

    cast("Any", provider.mass).config.set.assert_called_once()


def _track_with_image(item_id: str, image_path: str) -> Track:
    """Build a builtin track carrying a single thumbnail image."""
    return Track(
        item_id=item_id,
        provider="builtin",
        name="x",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="builtin",
                provider_instance="builtin_1",
            )
        },
        metadata=MediaItemMetadata(
            images=UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_path,
                        provider="builtin",
                        remotely_accessible=image_path.startswith("http"),
                    )
                ]
            )
        ),
    )


@pytest.mark.parametrize(
    "image_url",
    [
        "http://x/a.jpg",
        "https://x/a.jpg",
        "rtsp://x/stream",
        "rtmp://x/stream",
        "data:image/png;base64,AAAA",
    ],
)
def test_ensure_remote_image_url_accepts_remote(image_url: str) -> None:
    """Remote URLs (including stream schemes for embedded art) and data URIs pass."""
    BuiltinProvider._ensure_remote_image_url(image_url)


@pytest.mark.parametrize("image_url", ["/etc/passwd", "/x/cover.jpg", "file:///x.jpg", "cover.jpg"])
def test_ensure_remote_image_url_rejects_local(image_url: str) -> None:
    """A local image reference is refused."""
    with pytest.raises(MediaNotFoundError):
        BuiltinProvider._ensure_remote_image_url(image_url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "http://x/a.jpg",
        "https://x/a.jpg",
        "rtsp://x/stream",
        "rtmp://x/stream",
        "data:image/png;base64,AAAA",
    ],
)
async def test_resolve_image_returns_remote_reference(path: str) -> None:
    """resolve_image passes through a remote image reference (stream schemes included)."""
    provider = _make_provider()
    assert await provider.resolve_image(path) == path


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/etc/passwd", "/srv/other/cover.jpg", "file:///etc/passwd"])
async def test_resolve_image_rejects_local_path(path: str) -> None:
    """resolve_image refuses a local filesystem path."""
    provider = _make_provider()
    with pytest.raises(FileNotFoundError):
        await provider.resolve_image(path)


@pytest.mark.asyncio
async def test_resolve_image_keeps_bundled_images() -> None:
    """The bundled logo, fanart and genre icons still resolve."""
    provider = _make_provider()
    assert await provider.resolve_image("logo.png") == MASS_LOGO
    assert await provider.resolve_image("fanart.jpg") == VARIOUS_ARTISTS_FANART
    genre_icon = await provider.resolve_image("genres/rock.png")
    assert isinstance(genre_icon, str)
    assert genre_icon.endswith("rock.png")


@pytest.mark.asyncio
async def test_resolve_image_allows_server_generated_local_files() -> None:
    """Generated collages and bundled provider assets under our own dirs resolve."""
    provider = _make_provider()
    collage = os.path.join(provider.mass.cache_path, "collage_images", "playlist.jpg")
    assert await provider.resolve_image(collage) == collage
    bundled = os.path.join(
        os.path.dirname(music_assistant.__file__), "providers", "ai_radio", "air.png"
    )
    assert await provider.resolve_image(bundled) == bundled


@pytest.mark.asyncio
async def test_add_track_rejects_local_image_without_storing() -> None:
    """add_track refuses a local image URL even with a valid stream URL."""
    provider = _make_provider()
    with pytest.raises(MediaNotFoundError):
        await provider.add_track("http://ok/song.mp3", "Song", image_url="/etc/passwd")
    cast("Any", provider.mass).config.set.assert_not_called()


@pytest.mark.asyncio
async def test_add_radio_rejects_local_image_without_storing() -> None:
    """add_radio refuses a local image URL even with a valid stream URL."""
    provider = _make_provider()
    with pytest.raises(MediaNotFoundError):
        await provider.add_radio("http://ok/stream", "Radio", image_url="/etc/passwd")
    cast("Any", provider.mass).config.set.assert_not_called()


@pytest.mark.asyncio
async def test_library_add_rejects_local_path_track_without_storing() -> None:
    """library_add (the add_item object form) refuses a local-path track."""
    provider = _make_provider()
    track = _track_with_image("/etc/passwd", "http://ok/cover.jpg")
    with pytest.raises(MediaNotFoundError):
        await provider.library_add(track)
    cast("Any", provider.mass).config.set.assert_not_called()


@pytest.mark.asyncio
async def test_library_add_rejects_local_image_without_storing() -> None:
    """library_add refuses a track whose image is a local path."""
    provider = _make_provider()
    track = _track_with_image("http://ok/song.mp3", "/etc/passwd")
    with pytest.raises(MediaNotFoundError):
        await provider.library_add(track)
    cast("Any", provider.mass).config.set.assert_not_called()


@pytest.mark.asyncio
async def test_library_add_keeps_embedded_art_stream_scheme() -> None:
    """
    A stream track whose embedded-art image is its own source URL stays addable.

    The image guard on the direct add commands must not leak into library_add, where
    a probed rtsp/rtmp item carries its own URL as the cover-art path.
    """
    provider = _make_provider()
    cast("Any", provider.mass).config.get.return_value = []
    track = _track_with_image("rtsp://host/stream", "rtsp://host/stream")
    assert await provider.library_add(track) is True
    cast("Any", provider.mass).config.set.assert_called_once()


@pytest.mark.asyncio
async def test_get_media_info_rejects_resolved_local_path() -> None:
    """A .pls that resolves to a file:// entry is refused before ffprobe."""
    provider = _make_provider()
    cast("Any", provider.mass).cache.get = AsyncMock(return_value=None)
    provider._resolve_url = AsyncMock(return_value="file://host/evil.mp3")  # type: ignore[method-assign]
    with (
        patch("music_assistant.providers.builtin.async_parse_tags") as parse_mock,
        pytest.raises(MediaNotFoundError),
    ):
        await provider._get_media_info("http://ok/list.pls")
    parse_mock.assert_not_called()
