"""Tests for filesystem provider sound effects support."""

import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.media_items import SoundEffect

from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import CONF_ENTRY_CONTENT_TYPE


def _create_provider(base_path: str = "/media") -> LocalFileSystemProvider:
    """
    Create a LocalFileSystemProvider with mocked dependencies.

    :param base_path: The base path the provider operates on.
    """
    mock_config = MagicMock()
    mock_config.get_value = MagicMock(
        side_effect=lambda key: "sound_effects" if key == CONF_ENTRY_CONTENT_TYPE.key else None
    )
    mock_config.instance_id = "filesystem_local--test"

    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)

    provider.config = mock_config
    provider.manifest = MagicMock()
    provider.manifest.domain = "filesystem_local"
    provider.media_content_type = "sound_effects"
    provider.write_access = False
    provider.base_path = base_path
    provider.sync_running = False
    provider.mass = MagicMock()
    provider.logger = MagicMock()
    provider.cache = MagicMock()
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()

    return provider


def _mock_tags(title: str) -> MagicMock:
    """
    Create mocked AudioTags for a sound effect file.

    :param title: The title to report for the file.
    """
    tags = MagicMock()
    tags.title = title
    tags.title_sort = None
    tags.duration = 30.0
    tags.sample_rate = 44100
    tags.bits_per_sample = 16
    tags.channels = 2
    tags.bit_rate = 320
    tags.format = "mp3"
    tags.has_cover_image = False
    tags.get = MagicMock(return_value=None)
    return tags


def test_sound_effects_content_type_features() -> None:
    """Sound effects content type advertises the sound effects feature, no library features."""
    provider = _create_provider()
    features = provider.supported_features
    assert ProviderFeature.SOUND_EFFECTS in features
    assert not any(x for x in features if x.value.startswith("library_"))


@pytest.mark.asyncio
async def test_sync_library_returns_early() -> None:
    """Sound effects provider never syncs anything into the library."""
    provider = _create_provider()
    await provider.sync_library(MediaType.TRACK)
    provider.mass.music.database.get_rows_from_query.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_get_sound_effects_enumerates_files(tmp_path: pathlib.Path) -> None:
    """get_sound_effects yields a parsed SoundEffect for every audio file found."""
    (tmp_path / "rain.mp3").write_bytes(b"fake")
    (tmp_path / "thunder.flac").write_bytes(b"fake")
    (tmp_path / "readme.txt").write_text("not audio")
    provider = _create_provider(base_path=str(tmp_path))

    with patch(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        new_callable=AsyncMock,
        side_effect=lambda path, _size: _mock_tags(pathlib.Path(path).stem),
    ):
        result = [x async for x in provider.get_sound_effects()]

    assert [x.name for x in result] == ["rain", "thunder"]
    assert all(isinstance(x, SoundEffect) for x in result)
    assert result[0].item_id == "rain.mp3"
    assert result[0].provider == "filesystem_local--test"
    # parsed items are stored in the cache for the next enumeration
    assert provider.cache.set.await_count == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_get_sound_effects_uses_cache(tmp_path: pathlib.Path) -> None:
    """get_sound_effects returns cached items without re-parsing the file."""
    (tmp_path / "rain.mp3").write_bytes(b"fake")
    provider = _create_provider(base_path=str(tmp_path))
    cached_item = SoundEffect(
        item_id="rain.mp3",
        provider="filesystem_local--test",
        name="rain",
        provider_mappings=set(),
    )
    provider.cache.get = AsyncMock(return_value=cached_item)  # type: ignore[method-assign]

    with patch(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        new_callable=AsyncMock,
    ) as mock_parse_tags:
        result = [x async for x in provider.get_sound_effects()]

    assert result == [cached_item]
    mock_parse_tags.assert_not_awaited()


@pytest.mark.asyncio
async def test_browse_maps_files_to_sound_effects() -> None:
    """Browse lists audio files as sound effect items and hides playlist files."""
    provider = _create_provider()
    audio_file = MagicMock()
    audio_file.is_dir = False
    audio_file.filename = "rain.mp3"
    audio_file.ext = "mp3"
    audio_file.relative_path = "rain.mp3"
    audio_file.absolute_path = "/media/rain.mp3"
    playlist_file = MagicMock()
    playlist_file.is_dir = False
    playlist_file.filename = "mix.m3u"
    playlist_file.ext = "m3u"
    playlist_file.relative_path = "mix.m3u"
    playlist_file.absolute_path = "/media/mix.m3u"
    provider._scandir = AsyncMock(return_value=[audio_file, playlist_file])  # type: ignore[method-assign]

    result = await provider.browse("filesystem_local--test://")

    assert len(result) == 1
    assert result[0].media_type == MediaType.SOUND_EFFECT
    assert result[0].item_id == "rain.mp3"
