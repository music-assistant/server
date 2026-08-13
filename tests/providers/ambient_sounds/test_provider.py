"""Tests for the Ambient Sounds provider."""

import pathlib
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import AudioError, InvalidDataError, MediaNotFoundError

from music_assistant.providers import ambient_sounds
from music_assistant.providers.ambient_sounds import (
    CONF_KEY_CUSTOM_SOUNDS,
    PRESETS,
    AmbientSoundsProvider,
)


def _create_provider(cache_path: str) -> AmbientSoundsProvider:
    """
    Create an AmbientSoundsProvider with mocked dependencies.

    :param cache_path: The cache directory the provider renders loop files into.
    """
    with patch.object(AmbientSoundsProvider, "__init__", lambda *_a, **_kw: None):
        provider = AmbientSoundsProvider.__new__(AmbientSoundsProvider)

    provider.config = MagicMock()
    provider.config.instance_id = "ambient_sounds"
    provider.config.setup_data = {}
    # no declared config entries: get_setup_value's fallback resolves to the default
    provider.config.values = {}
    provider.config.get_value = lambda _key, default=None: default
    provider.manifest = MagicMock()
    provider.manifest.domain = "ambient_sounds"
    provider.mass = MagicMock()
    provider.mass.cache_path = cache_path
    provider.logger = MagicMock()
    # in-memory, path-aware stand-in for persistent config storage: the provider
    # stores its custom sounds in its own setup_data via the base class helpers
    config_store: dict[str, Any] = {"providers": {"ambient_sounds": {"setup_data": {}}}}

    def _config_get(key: str, default: Any = None) -> Any:
        parent: Any = config_store
        for part in key.split("/"):
            if not isinstance(parent, dict) or part not in parent:
                return default
            parent = parent[part]
        return parent

    def _config_set(key: str, value: Any, immediate: bool = False) -> None:  # noqa: ARG001
        parts = key.split("/")
        parent = config_store
        for part in parts[:-1]:
            parent = parent.setdefault(part, {})
        parent[parts[-1]] = value

    provider.mass.config.get = _config_get
    provider.mass.config.set = _config_set
    provider.mass.config.encrypt_string = lambda value: value
    provider.mass.config.decrypt_string = lambda value: value
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
    stored = provider._stored_sounds()
    assert len(stored) == 1
    assert stored[0]["name"] == "Rain 2"


async def test_add_custom_sound_invalid_url(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An url that fails to probe is rejected and not stored."""
    # async_parse_tags raises InvalidDataError (not AudioError) on ffprobe failures;
    # the frontend relies on that exact error code to show its friendly message
    monkeypatch.setattr(
        ambient_sounds, "async_parse_tags", AsyncMock(side_effect=InvalidDataError("not audio"))
    )
    provider = _create_provider(str(tmp_path))
    with pytest.raises(InvalidDataError):
        await provider.add_sound("https://example.com/not_audio", "Broken")
    assert not provider.get_setup_value(CONF_KEY_CUSTOM_SOUNDS)


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
