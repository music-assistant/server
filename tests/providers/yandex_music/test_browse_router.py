"""Browse dispatch contracts independent of the provider's API requests."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import ProviderFeature

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.yandex_music.browse import _BrowsePath, _BrowseRoute, _BrowseRouter
from music_assistant.providers.yandex_music.constants import ROTOR_STATION_MY_WAVE
from music_assistant.providers.yandex_music.provider import YandexMusicProvider, _WaveState


@pytest.fixture
def browse_provider() -> Mock:
    """Stub the fetch handlers while keeping real per-station wave locks."""
    provider = Mock(spec=YandexMusicProvider)
    provider.instance_id = "yandex_music_instance"
    provider.supported_features = {ProviderFeature.BROWSE}
    provider._get_user_wave_presets.return_value = []
    provider._get_discovered_tag_slugs = AsyncMock(return_value=set())
    provider._wave_states = {}
    provider._get_wave_state.side_effect = lambda key: provider._wave_states.setdefault(
        key, _WaveState()
    )
    return provider


def test_browse_path_components_cannot_be_changed_through_an_alias() -> None:
    """Route components remain consistent even if a caller extends its own copy."""
    path = _BrowsePath.parse("yandex_music_instance://my_wave/next/")
    components = path.path_parts
    components += ("unexpected",)
    assert list(path.path_parts) == ["my_wave", "next", ""]
    assert path.subpath == "my_wave"
    assert path.sub_subpath == "next"


async def test_registry_dispatches_first_match(browse_provider: Mock) -> None:
    """Overlapping routes resolve in registration order."""
    first = AsyncMock(return_value=[])
    second = AsyncMock(return_value=[])
    router = _BrowseRouter(
        browse_provider,
        routes=[_BrowseRoute(lambda _path: True, first), _BrowseRoute(lambda _path: True, second)],
    )
    assert await router.dispatch("yandex_music_instance://custom") == []
    first.assert_awaited_once()
    second.assert_not_awaited()


@pytest.mark.parametrize(
    ("subpath", "handler"),
    [
        ("for_you", "_browse_for_you"),
        ("picks/mood/calm", "_browse_picks"),
        ("mixes/summer", "_browse_mixes"),
        ("waves/genre/rock", "_browse_waves"),
        ("radio/genre/rock", "_browse_waves"),
        ("my_waves_set/custom", "_browse_vibe_sets"),
        ("waves_landing/custom", "_browse_waves_landing"),
    ],
)
async def test_browse_delegates_prefixes(browse_provider: Mock, subpath: str, handler: str) -> None:
    """Existing prefixes retain the full path and path components."""
    fetch = AsyncMock(return_value=[])
    setattr(browse_provider, handler, fetch)
    path = f"yandex_music_instance://{subpath}"
    assert await YandexMusicProvider.browse(browse_provider, path) == []
    fetch.assert_awaited_once_with(path, subpath.split("/"))


@pytest.mark.parametrize(
    ("subpath", "handler"), [("pinned", "_browse_pins"), ("history", "_browse_history")]
)
async def test_browse_delegates_no_argument_handlers(
    browse_provider: Mock, subpath: str, handler: str
) -> None:
    """Pins and history retain their argument-free retrieval contract."""
    fetch = AsyncMock(return_value=[])
    setattr(browse_provider, handler, fetch)
    assert (
        await YandexMusicProvider.browse(browse_provider, f"yandex_music_instance://{subpath}")
        == []
    )
    fetch.assert_awaited_once_with()


@pytest.mark.parametrize("suffix", ["my_wave", "my_wave/next"])
async def test_my_wave_handler_holds_station_lock(browse_provider: Mock, suffix: str) -> None:
    """The dispatch shell retains the lock around wave browsing."""
    wave = browse_provider._get_wave_state(ROTOR_STATION_MY_WAVE)

    async def fetch(_path: str, _subpath: str | None) -> list[Any]:
        assert wave.lock.locked()
        return []

    browse_provider._browse_my_wave = AsyncMock(side_effect=fetch)
    assert (
        await YandexMusicProvider.browse(browse_provider, f"yandex_music_instance://{suffix}") == []
    )
    assert not wave.lock.locked()


@pytest.mark.parametrize("suffix", ["my_wave_modes/discover/next", "my_wave_modes_discover/next"])
async def test_wave_modes_accept_both_uri_forms(browse_provider: Mock, suffix: str) -> None:
    """Slash and underscore forms preserve the preset and pagination flag."""
    station = f"{ROTOR_STATION_MY_WAVE}#discover"
    wave = browse_provider._get_wave_state(station)

    async def fetch(_path: str, station_key: str, load_more: bool) -> list[Any]:
        assert wave.lock.locked()
        assert station_key == station
        assert load_more is True
        return []

    browse_provider._browse_my_wave_mode = AsyncMock(side_effect=fetch)
    assert (
        await YandexMusicProvider.browse(browse_provider, f"yandex_music_instance://{suffix}") == []
    )


@pytest.mark.parametrize("suffix", ["my_wave_presets/0/next", "my_wave_presets_0/next"])
async def test_user_presets_accept_both_uri_forms(browse_provider: Mock, suffix: str) -> None:
    """Saved settings are applied under the same station lock as the browse fetch."""
    browse_provider._get_user_wave_presets.return_value = [{"name": "Calm", "moodEnergy": "calm"}]
    station = f"{ROTOR_STATION_MY_WAVE}#preset_0"
    wave = browse_provider._get_wave_state(station)

    async def fetch(_path: str, station_key: str, load_more: bool) -> list[Any]:
        assert wave.lock.locked()
        assert wave.settings == {"moodEnergy": "calm"}
        assert station_key == station
        assert load_more is True
        return []

    browse_provider._browse_my_wave_mode = AsyncMock(side_effect=fetch)
    assert (
        await YandexMusicProvider.browse(browse_provider, f"yandex_music_instance://{suffix}") == []
    )


@pytest.mark.parametrize(
    "suffix",
    [
        "my_wave_modes/next",
        "my_wave_modes_unknown",
        "my_wave_presets/x",
        "my_wave_presets_-1",
        "my_wave_presets_4",
    ],
)
async def test_invalid_presets_return_empty(browse_provider: Mock, suffix: str) -> None:
    """Invalid presets must not fall through into tag discovery or the MA library."""
    browse_provider._browse_my_wave_mode = AsyncMock()
    assert (
        await YandexMusicProvider.browse(browse_provider, f"yandex_music_instance://{suffix}") == []
    )
    browse_provider._browse_my_wave_mode.assert_not_awaited()
    browse_provider._get_discovered_tag_slugs.assert_not_awaited()


async def test_collection_nested_library_path(browse_provider: Mock) -> None:
    """Nested collection URLs continue to use MA's library browsing."""
    with patch.object(MusicProvider, "browse", new_callable=AsyncMock, return_value=[]) as library:
        assert (
            await YandexMusicProvider.browse(
                browse_provider, "yandex_music_instance://collection/tracks"
            )
            == []
        )
        library.assert_awaited_once_with(browse_provider, "yandex_music_instance://tracks")


async def test_unknown_path_uses_existing_library_fallback(browse_provider: Mock) -> None:
    """Unknown non-root paths retain MA's KeyError rather than becoming root listings."""
    with (
        patch.object(
            MusicProvider, "browse", new_callable=AsyncMock, side_effect=KeyError("unknown")
        ),
        pytest.raises(KeyError),
    ):
        await YandexMusicProvider.browse(browse_provider, "yandex_music_instance://unknown")


async def test_standard_library_folder_skips_tag_discovery(browse_provider: Mock) -> None:
    """Known MA library paths never make a tag-discovery API request."""
    with patch.object(MusicProvider, "browse", new_callable=AsyncMock, return_value=[]):
        assert (
            await YandexMusicProvider.browse(browse_provider, "yandex_music_instance://artists")
            == []
        )
    browse_provider._get_discovered_tag_slugs.assert_not_awaited()


async def test_direct_tag_and_station_urls(browse_provider: Mock) -> None:
    """Play-time reconstruction of a tag or station keeps resolving directly."""
    browse_provider._get_discovered_tag_slugs.return_value = {"calm"}
    browse_provider._get_tag_playlists_as_browse = AsyncMock(return_value=[])
    browse_provider._browse_wave_station = AsyncMock(return_value=[])
    router = _BrowseRouter(browse_provider)
    assert await router.dispatch("yandex_music_instance://calm") == []
    browse_provider._get_tag_playlists_as_browse.assert_awaited_once_with("calm")
    assert await router.dispatch("yandex_music_instance://activity:workout") == []
    browse_provider._browse_wave_station.assert_awaited_once_with("activity:workout")


async def test_handler_invokable_in_isolation(browse_provider: Mock) -> None:
    """A wave handler can be exercised without the provider's browse dispatch."""
    browse_provider._browse_my_wave = AsyncMock(return_value=[])
    router = _BrowseRouter(browse_provider)
    path = _BrowsePath.parse("yandex_music_instance://my_wave/next")
    assert await router._my_wave(path) == []
    browse_provider._browse_my_wave.assert_awaited_once_with(path.path, "next")


async def test_waiting_preset_does_not_overwrite_locked_settings(browse_provider: Mock) -> None:
    """A concurrent preset browse waits before changing another operation's settings."""
    wave = _WaveState()
    wave.settings = {"moodEnergy": "active"}
    requesting = asyncio.Event()

    def get_wave(_key: str) -> _WaveState:
        requesting.set()
        return wave

    browse_provider._get_wave_state.side_effect = get_wave
    browse_provider._get_user_wave_presets.return_value = [{"name": "Calm", "moodEnergy": "calm"}]
    browse_provider._browse_my_wave_mode = AsyncMock(return_value=[])
    async with asyncio.TaskGroup() as tasks, wave.lock:
        tasks.create_task(
            YandexMusicProvider.browse(browse_provider, "yandex_music_instance://my_wave_presets_0")
        )
        await asyncio.wait_for(requesting.wait(), timeout=1)
        assert wave.settings == {"moodEnergy": "active"}
    assert wave.settings == {"moodEnergy": "calm"}
