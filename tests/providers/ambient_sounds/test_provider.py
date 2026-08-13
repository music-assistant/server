"""Tests for the Ambient Sounds provider."""

import pathlib
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import AudioError, MediaNotFoundError

from music_assistant.providers import ambient_sounds
from music_assistant.providers.ambient_sounds import PRESETS, AmbientSoundsProvider


def _create_provider(cache_path: str) -> AmbientSoundsProvider:
    """
    Create an AmbientSoundsProvider with mocked dependencies.

    :param cache_path: The cache directory the provider renders loop files into.
    """
    with patch.object(AmbientSoundsProvider, "__init__", lambda *_a, **_kw: None):
        provider = AmbientSoundsProvider.__new__(AmbientSoundsProvider)

    provider.config = MagicMock()
    provider.config.instance_id = "ambient_sounds"
    provider.manifest = MagicMock()
    provider.manifest.domain = "ambient_sounds"
    provider.mass = MagicMock()
    provider.mass.cache_path = cache_path
    provider.logger = MagicMock()
    # in-memory stand-in for persistent config storage of custom sounds
    stored: dict[str, Any] = {}
    provider.mass.config.get = lambda key, default=None: stored.get(key, default)
    provider.mass.config.set = lambda key, value: stored.__setitem__(key, value)
    provider.mass.cache.get = AsyncMock(return_value=None)
    provider.mass.cache.set = AsyncMock()
    provider.mass.cache.delete = AsyncMock()
    provider._unregister_handles = []

    return provider


def _mock_media_info(duration: float | None = 3600.0, icyname: str | None = None) -> MagicMock:
    """Create a mocked AudioTags result for a probed custom sound url."""
    media_info = MagicMock()
    media_info.duration = duration
    media_info.format = "mp3"
    media_info.sample_rate = 44100
    media_info.bits_per_sample = 16
    media_info.channels = 2
    media_info.raw = {}
    media_info.get = lambda key, default=None: {"icyname": icyname}.get(key, default)
    return media_info


async def test_sound_effects_enumeration(tmp_path: pathlib.Path) -> None:
    """All presets are enumerated as sound effect items with correct metadata."""
    provider = _create_provider(str(tmp_path))
    items = [item async for item in provider.get_sound_effects()]
    assert len(items) == len(PRESETS)
    assert {item.item_id for item in items} == set(PRESETS)
    for item in items:
        assert item.media_type == MediaType.SOUND_EFFECT
        assert item.name
        assert item.translation_key == item.item_id
        assert item.metadata.description
        assert item.duration == ambient_sounds.LOOP_DURATION


async def test_get_sound_effect(tmp_path: pathlib.Path) -> None:
    """A single sound effect resolves by id; unknown ids raise MediaNotFoundError."""
    provider = _create_provider(str(tmp_path))
    item = await provider.get_sound_effect("white_noise")
    assert item.name == "White noise"
    assert item.provider_mappings
    with pytest.raises(MediaNotFoundError):
        await provider.get_sound_effect("unknown_preset")


async def test_supported_features() -> None:
    """The provider advertises sound effects and browse, no library features."""
    assert ProviderFeature.SOUND_EFFECTS in ambient_sounds.SUPPORTED_FEATURES
    assert ProviderFeature.BROWSE in ambient_sounds.SUPPORTED_FEATURES
    assert not any(x for x in ambient_sounds.SUPPORTED_FEATURES if x.value.startswith("library_"))


async def test_stream_details_renders_loop(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Streamdetails render the loop file once and reuse it on subsequent calls."""
    # shrink the loop so the test renders quickly
    monkeypatch.setattr(ambient_sounds, "LOOP_DURATION", 2)
    monkeypatch.setattr(ambient_sounds, "CROSSFADE_DURATION", 1)
    provider = _create_provider(str(tmp_path))
    await provider.handle_async_init()

    stream_details = await provider.get_stream_details("ocean_waves")
    assert stream_details.stream_type == StreamType.LOCAL_FILE
    assert stream_details.media_type == MediaType.SOUND_EFFECT
    assert stream_details.duration == 2
    assert isinstance(stream_details.path, str)
    loop_file = pathlib.Path(stream_details.path)
    assert loop_file.is_file()
    assert loop_file.read_bytes()[:4] == b"fLaC"
    # no leftover temp file from the render
    assert not loop_file.with_name(f"{loop_file.name}.tmp").exists()

    # a second call must reuse the rendered file instead of rendering again
    mtime = loop_file.stat().st_mtime
    stream_details_2 = await provider.get_stream_details("ocean_waves")
    assert stream_details_2.path == stream_details.path
    assert loop_file.stat().st_mtime == mtime


async def test_stream_details_unknown_preset(tmp_path: pathlib.Path) -> None:
    """Streamdetails for an unknown preset raise MediaNotFoundError."""
    provider = _create_provider(str(tmp_path))
    await provider.handle_async_init()
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("unknown_preset")


async def test_add_custom_sound(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A custom sound is probed, stored and enumerated alongside the presets."""
    monkeypatch.setattr(
        ambient_sounds, "async_parse_tags", AsyncMock(return_value=_mock_media_info())
    )
    provider = _create_provider(str(tmp_path))
    url = "https://example.com/sounds/rain.mp3"

    item = await provider.add_sound(url, "Rain")
    assert item.item_id == url
    assert item.name == "Rain"
    assert item.duration == 3600
    assert item.media_type == MediaType.SOUND_EFFECT

    items = [x async for x in provider.get_sound_effects()]
    assert len(items) == len(PRESETS) + 1
    assert (await provider.get_sound_effect(url)).name == "Rain"

    # adding the same url again replaces the stored entry instead of duplicating it
    await provider.add_sound(url, "Rain 2")
    stored = provider.mass.config.get(provider._custom_sounds_conf_key)
    assert len(stored) == 1
    assert stored[0]["name"] == "Rain 2"


async def test_add_custom_sound_invalid_url(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An url that fails to probe is rejected and not stored."""
    monkeypatch.setattr(
        ambient_sounds, "async_parse_tags", AsyncMock(side_effect=AudioError("not audio"))
    )
    provider = _create_provider(str(tmp_path))
    with pytest.raises(AudioError):
        await provider.add_sound("https://example.com/not_audio", "Broken")
    assert not provider.mass.config.get(provider._custom_sounds_conf_key)


async def test_remove_custom_sound(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A removed custom sound is no longer enumerated or resolvable."""
    monkeypatch.setattr(
        ambient_sounds, "async_parse_tags", AsyncMock(return_value=_mock_media_info())
    )
    provider = _create_provider(str(tmp_path))
    url = "https://example.com/sounds/rain.mp3"
    await provider.add_sound(url, "Rain")

    await provider.remove_sound(url)
    items = [x async for x in provider.get_sound_effects()]
    assert len(items) == len(PRESETS)
    with pytest.raises(MediaNotFoundError):
        await provider.get_sound_effect(url)


async def test_custom_sound_stream_details(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Streamdetails for a custom sound point at its url with probed format info."""
    monkeypatch.setattr(
        ambient_sounds, "async_parse_tags", AsyncMock(return_value=_mock_media_info())
    )
    provider = _create_provider(str(tmp_path))
    url = "https://example.com/sounds/rain.mp3"
    await provider.add_sound(url, "Rain")

    stream_details = await provider.get_stream_details(url)
    assert stream_details.stream_type == StreamType.HTTP
    assert stream_details.media_type == MediaType.SOUND_EFFECT
    assert stream_details.path == url
    assert stream_details.duration == 3600
    assert stream_details.audio_format.content_type == ContentType.MP3
    assert stream_details.can_seek
    assert stream_details.allow_seek


async def test_custom_sound_live_stream_not_seekable(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An endless (radio-style) stream without duration is marked as not seekable."""
    monkeypatch.setattr(
        ambient_sounds,
        "async_parse_tags",
        AsyncMock(return_value=_mock_media_info(duration=None, icyname="Some Radio")),
    )
    provider = _create_provider(str(tmp_path))
    url = "https://example.com/streams/radio"
    await provider.add_sound(url, "Radio")

    stream_details = await provider.get_stream_details(url)
    assert stream_details.duration is None
    assert not stream_details.can_seek
    assert not stream_details.allow_seek


async def test_remove_custom_sound_clears_cached_media_info(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing a custom sound also drops its cached media info."""
    monkeypatch.setattr(
        ambient_sounds, "async_parse_tags", AsyncMock(return_value=_mock_media_info())
    )
    provider = _create_provider(str(tmp_path))
    url = "https://example.com/sounds/rain.mp3"
    await provider.add_sound(url, "Rain")

    await provider.remove_sound(url)
    cache_delete = cast("AsyncMock", provider.mass.cache.delete)
    cache_delete.assert_awaited_once_with(
        url, provider=provider.instance_id, category=ambient_sounds.CACHE_CATEGORY_MEDIA_INFO
    )


async def test_unload_unregisters_api_commands(tmp_path: pathlib.Path) -> None:
    """Unloading the provider unregisters its API commands so a reload can re-register."""
    provider = _create_provider(str(tmp_path))
    handles = [MagicMock(), MagicMock()]
    provider._unregister_handles.extend(handles)

    await provider.unload()
    for handle in handles:
        handle.assert_called_once()
    assert not provider._unregister_handles


async def test_failed_render_leaves_no_temp_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed render raises AudioError and cleans up its partial temp file."""

    async def fake_check_output(*args: str) -> tuple[int, bytes]:
        # simulate ffmpeg dying halfway: partial output written, non-zero exit
        pathlib.Path(args[-1]).write_bytes(b"partial")
        return 1, b"boom"

    monkeypatch.setattr(ambient_sounds, "check_output", fake_check_output)
    provider = _create_provider(str(tmp_path))
    await provider.handle_async_init()
    with pytest.raises(AudioError):
        await provider.get_stream_details("white_noise")
    assert not list(tmp_path.rglob("*.tmp"))
