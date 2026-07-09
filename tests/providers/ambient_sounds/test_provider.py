"""Tests for the Ambient Sounds provider."""

import pathlib
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature, StreamType
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

    return provider


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
