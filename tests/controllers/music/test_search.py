"""Tests for the global search on the music controller."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature, ProviderType
from music_assistant_models.errors import (
    MusicAssistantError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    Artist,
    Genre,
    ProviderMapping,
    Radio,
    SearchResults,
    Track,
)

from music_assistant.controllers.music import MusicController
from music_assistant.controllers.music.constants import (
    SEARCH_CACHE_EXPIRATION_LOCAL_PROVIDER,
    SEARCH_CACHE_EXPIRATION_STREAMING_PROVIDER,
)
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import Callable


def _make_track(item_id: str, provider: str, name: str) -> Track:
    """Return a minimal Track for the given provider."""
    return Track(
        item_id=item_id,
        provider=provider,
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider,
                provider_instance=provider,
            )
        },
    )


def _make_library_artist(name: str, mapped_provider: str) -> Artist:
    """Return a library Artist with a provider mapping to the given provider."""
    return Artist(
        item_id="lib1",
        provider="library",
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id="artist1",
                provider_domain=mapped_provider,
                provider_instance=mapped_provider,
            )
        },
    )


def _make_search_provider(instance_id: str, domain: str | None = None) -> Mock:
    """Return a mocked music provider that supports search."""
    prov = Mock(spec=MusicProvider)
    prov.instance_id = instance_id
    prov.domain = domain or instance_id
    prov.search_cache_key = ""
    prov.name = instance_id
    prov.available = True
    prov.supported_features = {ProviderFeature.SEARCH}
    prov.is_streaming_provider = True
    prov.search = AsyncMock(return_value=SearchResults())
    return prov


def _make_controller(providers: list[Mock]) -> MusicController:
    """Return a music controller wired to the given mocked providers."""
    mass = Mock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock(return_value=None)
    mass.get_providers_supporting_feature.return_value = []
    mass.get_provider = Mock(
        side_effect=lambda instance_id, **_kwargs: next(
            (p for p in providers if p.instance_id == instance_id), None
        )
    )
    # mimic the task_id deduplication behavior of the real create_task implementation
    tracked_tasks: dict[str, asyncio.Task[Any]] = {}

    def _create_task(
        target: Any, *_args: Any, task_id: str | None = None, **_kwargs: Any
    ) -> asyncio.Task[Any]:
        if task_id and (existing := tracked_tasks.get(task_id)) and not existing.done():
            target.close()
            return existing
        task = asyncio.get_running_loop().create_task(target)

        def _task_done(_task: asyncio.Task[Any]) -> None:
            if task_id:
                tracked_tasks.pop(task_id, None)
            if not _task.cancelled():
                _task.exception()

        task.add_done_callback(_task_done)
        if task_id:
            tracked_tasks[task_id] = task
        return task

    mass.create_task = Mock(side_effect=_create_task)
    controller = MusicController.__new__(MusicController)
    controller.mass = mass
    controller.domain = "music"
    controller.logger = logging.getLogger(__name__)
    controller.get_unique_providers = Mock(  # type: ignore[method-assign]
        return_value=[p.instance_id for p in providers]
    )
    controller.search_library = AsyncMock(return_value=SearchResults())  # type: ignore[method-assign]
    return controller


def _cache_writes(controller: MusicController, provider: str) -> list[Any]:
    """Return the cache.set calls made for the given provider."""
    cache_set = cast("AsyncMock", controller.mass.cache.set)
    return [call for call in cache_set.await_args_list if call.kwargs.get("provider") == provider]


async def _wait_for(condition: Callable[[], bool], timeout: float = 1.0) -> None:
    """Wait until the given condition is true."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def test_search_provider_uses_centralized_provider_search() -> None:
    """A single-provider lookup uses the cached and timeout-bounded search path."""
    provider = _make_search_provider("prov_a")
    controller = _make_controller([provider])
    expected = SearchResults(tracks=[_make_track("track1", "prov_a", "My Song")])
    controller._apply_user_provider_filter = Mock(return_value=[])  # type: ignore[method-assign]
    controller._search_provider = AsyncMock(return_value=expected)  # type: ignore[method-assign]

    result = await controller.search_provider(
        "My Song",
        provider.instance_id,
        [MediaType.TRACK],
        limit=5,
        allowed_provider_instances={"prov_a"},
    )

    assert result == expected
    controller._search_provider.assert_awaited_once_with(
        "My Song",
        provider.instance_id,
        [MediaType.TRACK],
        limit=5,
        strict_provider_instance=True,
    )


async def test_search_provider_resolves_domain_within_explicit_scope() -> None:
    """A domain lookup selects an allowed instance rather than a filtered global instance."""
    filtered_provider = _make_search_provider("qobuz_1", domain="qobuz")
    allowed_provider = _make_search_provider("qobuz_2", domain="qobuz")
    controller = _make_controller([filtered_provider, allowed_provider])
    expected = SearchResults(tracks=[_make_track("track1", "qobuz_2", "My Song")])
    controller._search_provider = AsyncMock(return_value=expected)  # type: ignore[method-assign]

    result = await controller.search_provider(
        "My Song",
        "qobuz",
        [MediaType.TRACK],
        limit=5,
        allowed_provider_instances={"qobuz_2"},
    )

    assert result == expected
    controller._search_provider.assert_awaited_once_with(
        "My Song",
        "qobuz_2",
        [MediaType.TRACK],
        limit=5,
        strict_provider_instance=True,
    )


async def test_search_provider_does_not_substitute_unavailable_scoped_instance() -> None:
    """An unavailable scoped account can not fall back to another account."""
    unavailable_provider = _make_search_provider("qobuz_1", domain="qobuz")
    unavailable_provider.available = False
    outside_scope_provider = _make_search_provider("qobuz_2", domain="qobuz")
    controller = _make_controller([unavailable_provider, outside_scope_provider])
    controller.mass.get_provider = Mock(  # type: ignore[method-assign]
        side_effect=lambda _instance_id, return_unavailable=False: (
            unavailable_provider if return_unavailable else outside_scope_provider
        )
    )
    controller._search_provider = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(ResourceTemporarilyUnavailable, match="qobuz_1"):
        await controller.search_provider(
            "My Song",
            "qobuz_1",
            [MediaType.TRACK],
            allowed_provider_instances={"qobuz_1"},
        )

    controller._search_provider.assert_not_awaited()
    controller.mass.get_provider.assert_called_once_with(
        "qobuz_1",
        return_unavailable=True,
    )


async def test_search_provider_scoped_domain_all_unavailable_raises() -> None:
    """A domain lookup within scope raises rather than looking like an empty result."""
    unavailable_a = _make_search_provider("qobuz_1", domain="qobuz")
    unavailable_a.available = False
    unavailable_b = _make_search_provider("qobuz_2", domain="qobuz")
    unavailable_b.available = False
    controller = _make_controller([unavailable_a, unavailable_b])
    controller.mass.get_provider = Mock(  # type: ignore[method-assign]
        side_effect=lambda instance_id, **_kwargs: next(
            (p for p in (unavailable_a, unavailable_b) if p.instance_id == instance_id), None
        )
    )
    controller._search_provider = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(ResourceTemporarilyUnavailable, match="qobuz"):
        await controller.search_provider(
            "My Song",
            "qobuz",
            [MediaType.TRACK],
            allowed_provider_instances={"qobuz_1", "qobuz_2"},
        )

    controller._search_provider.assert_not_awaited()


async def test_search_provider_unscoped_domain_skips_unavailable_sibling() -> None:
    """An unscoped domain lookup does not let an unavailable instance mask a sibling."""
    unavailable_first = _make_search_provider("qobuz_1", domain="qobuz")
    unavailable_first.available = False
    available_second = _make_search_provider("qobuz_2", domain="qobuz")
    for provider in (unavailable_first, available_second):
        provider.type = ProviderType.MUSIC
    controller = _make_controller([unavailable_first, available_second])
    controller._apply_user_provider_filter = Mock(  # type: ignore[method-assign]
        return_value=[unavailable_first, available_second]
    )
    expected = SearchResults(tracks=[_make_track("track1", "qobuz_2", "My Song")])
    controller._search_provider = AsyncMock(return_value=expected)  # type: ignore[method-assign]

    result = await controller.search_provider("My Song", "qobuz", [MediaType.TRACK])

    assert result == expected
    controller._search_provider.assert_awaited_once_with(
        "My Song",
        "qobuz_2",
        [MediaType.TRACK],
        limit=10,
        strict_provider_instance=True,
    )


async def test_internal_search_rejects_strict_instance_fallback() -> None:
    """The cached provider search keeps an exact captured account."""
    unavailable_provider = _make_search_provider("qobuz_1", domain="qobuz")
    unavailable_provider.available = False
    outside_scope_provider = _make_search_provider("qobuz_2", domain="qobuz")
    controller = _make_controller([unavailable_provider, outside_scope_provider])
    controller.mass.get_provider = Mock(  # type: ignore[method-assign]
        side_effect=lambda _instance_id, return_unavailable=False, **_kwargs: (
            unavailable_provider if return_unavailable else outside_scope_provider
        )
    )

    result = await controller._search_provider(
        "My Song",
        "qobuz_1",
        [MediaType.TRACK],
        strict_provider_instance=True,
    )

    assert result is None
    outside_scope_provider.search.assert_not_awaited()


async def test_search_provider_reports_timeout_as_failure() -> None:
    """A provider timeout remains distinguishable from a genuine empty result."""
    provider = _make_search_provider("prov_a")
    controller = _make_controller([provider])
    controller._search_provider = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with pytest.raises(ResourceTemporarilyUnavailable, match="prov_a"):
        await controller.search_provider(
            "My Song",
            provider.instance_id,
            [MediaType.TRACK],
            allowed_provider_instances={provider.instance_id},
        )


async def test_search_provider_returns_none_on_provider_error() -> None:
    """A provider error during search yields None instead of raising."""
    prov = _make_search_provider("prov_a")
    controller = _make_controller([prov])
    for error in (MusicAssistantError("rate limited"), ValueError("unexpected")):
        prov.search.side_effect = error
        result = await controller._search_provider("query", "prov_a", [MediaType.TRACK])
        assert result is None


async def test_global_search_returns_partial_results_when_provider_fails() -> None:
    """One failing provider must not break the entire global search."""
    prov_ok = _make_search_provider("prov_ok")
    prov_ok.search.return_value = SearchResults(
        tracks=[_make_track("track1", "prov_ok", "My Song")]
    )
    prov_bad = _make_search_provider("prov_bad")
    prov_bad.search.side_effect = MusicAssistantError("provider down")
    controller = _make_controller([prov_ok, prov_bad])

    result = await controller.search("My Song", media_types=[MediaType.TRACK], limit=5)

    assert [track.item_id for track in result.tracks] == ["track1"]
    prov_bad.search.assert_awaited_once()
    # an incomplete result may not be cached so the failed provider is retried
    assert not _cache_writes(controller, "music")


async def test_global_search_soft_timeout_returns_partial_and_caches_late(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow provider is not awaited beyond the soft timeout but still caches its result."""
    monkeypatch.setattr(
        "music_assistant.controllers.music.controller.SEARCH_PROVIDER_SOFT_TIMEOUT", 0.1
    )
    prov_fast = _make_search_provider("prov_fast")
    prov_fast.search.return_value = SearchResults(
        tracks=[_make_track("track1", "prov_fast", "My Song")]
    )
    prov_slow = _make_search_provider("prov_slow")
    slow_results = SearchResults(tracks=[_make_track("track2", "prov_slow", "My Song")])

    async def _slow_search(*_args: Any, **_kwargs: Any) -> SearchResults:
        await asyncio.sleep(0.5)
        return slow_results

    prov_slow.search.side_effect = _slow_search
    controller = _make_controller([prov_fast, prov_slow])

    start = time.monotonic()
    result = await controller.search("My Song", media_types=[MediaType.TRACK], limit=5)
    duration = time.monotonic() - start

    # the search returned within the soft timeout with the results of the fast provider
    assert duration < 0.4
    assert [track.item_id for track in result.tracks] == ["track1"]
    assert not _cache_writes(controller, "music")
    # the slow provider search completes in the background and caches its result
    await _wait_for(lambda: bool(_cache_writes(controller, "prov_slow")))
    late_writes = _cache_writes(controller, "prov_slow")
    assert len(late_writes) == 1
    assert late_writes[0].kwargs["data"] == slow_results.to_dict()


async def test_search_provider_waits_for_slow_provider_beyond_soft_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single-provider lookup waits for a slow (throttled) provider instead of failing."""
    monkeypatch.setattr(
        "music_assistant.controllers.music.controller.SEARCH_PROVIDER_SOFT_TIMEOUT", 0.05
    )
    provider = _make_search_provider("prov_slow")
    expected = SearchResults(tracks=[_make_track("track1", "prov_slow", "My Song")])

    async def _slow_search(*_args: Any, **_kwargs: Any) -> SearchResults:
        await asyncio.sleep(0.2)
        return expected

    provider.search.side_effect = _slow_search
    controller = _make_controller([provider])

    result = await controller.search_provider(
        "My Song",
        provider.instance_id,
        [MediaType.TRACK],
        allowed_provider_instances={provider.instance_id},
    )

    assert [track.item_id for track in result.tracks] == ["track1"]
    provider.search.assert_awaited_once()


async def test_global_search_hard_timeout_aborts_provider_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider search that exceeds the hard timeout is aborted and never cached."""
    monkeypatch.setattr(
        "music_assistant.controllers.music.controller.SEARCH_PROVIDER_SOFT_TIMEOUT", 0.05
    )
    monkeypatch.setattr(
        "music_assistant.controllers.music.controller.SEARCH_PROVIDER_HARD_TIMEOUT", 0.1
    )
    prov_hung = _make_search_provider("prov_hung")
    search_aborted = asyncio.Event()

    async def _hung_search(*_args: Any, **_kwargs: Any) -> SearchResults:
        try:
            await asyncio.sleep(5)
        finally:
            search_aborted.set()
        return SearchResults()

    prov_hung.search.side_effect = _hung_search
    controller = _make_controller([prov_hung])

    result = await controller.search("My Song", media_types=[MediaType.TRACK], limit=5)

    assert result.tracks == []
    # the hard timeout cancels the provider search and nothing is cached
    await _wait_for(search_aborted.is_set)
    cache_set = cast("AsyncMock", controller.mass.cache.set)
    assert not cache_set.await_args_list


async def test_search_provider_cache_hit_avoids_provider_call() -> None:
    """A cached provider result is served without hitting the provider again."""
    prov = _make_search_provider("prov_a")
    controller = _make_controller([prov])
    cached_results = SearchResults(tracks=[_make_track("track1", "prov_a", "My Song")])

    async def _fake_cache_get(*, provider: str = "default", **_kwargs: Any) -> Any:
        return cached_results if provider == "prov_a" else None

    controller.mass.cache.get = AsyncMock(side_effect=_fake_cache_get)  # type: ignore[method-assign]

    result = await controller.search("My Song", media_types=[MediaType.TRACK], limit=5)

    assert [track.item_id for track in result.tracks] == ["track1"]
    prov.search.assert_not_awaited()


async def test_empty_search_query_skips_all_providers() -> None:
    """An empty or whitespace-only query returns empty results without querying providers."""
    prov = _make_search_provider("prov_a")
    controller = _make_controller([prov])

    for search_query in ("", "   ", "\n\t"):
        result = await controller.search(search_query, media_types=[MediaType.TRACK], limit=5)

        assert result == SearchResults()
        prov.search.assert_not_awaited()
        cast("AsyncMock", controller.search_library).assert_not_awaited()
        cast("AsyncMock", controller.mass.cache.get).assert_not_awaited()


async def test_search_provider_cache_expiration_by_provider_type() -> None:
    """Streaming provider results are cached longer than local provider results."""
    prov_stream = _make_search_provider("prov_stream")
    prov_local = _make_search_provider("prov_local")
    prov_local.is_streaming_provider = False
    controller = _make_controller([prov_stream, prov_local])

    await controller.search("My Song", media_types=[MediaType.TRACK], limit=5)

    stream_writes = _cache_writes(controller, "prov_stream")
    local_writes = _cache_writes(controller, "prov_local")
    assert stream_writes[0].kwargs["expiration"] == SEARCH_CACHE_EXPIRATION_STREAMING_PROVIDER
    assert local_writes[0].kwargs["expiration"] == SEARCH_CACHE_EXPIRATION_LOCAL_PROVIDER


async def test_search_provider_without_streaming_attribute_gets_local_expiration() -> None:
    """Providers lacking is_streaming_provider (e.g. plugins) get the short cache expiration."""
    prov = _make_search_provider("prov_plugin")
    del prov.is_streaming_provider
    prov.search.return_value = SearchResults(
        tracks=[_make_track("track1", "prov_plugin", "My Song")]
    )
    controller = _make_controller([prov])

    result = await controller.search("My Song", media_types=[MediaType.TRACK], limit=5)

    assert [track.item_id for track in result.tracks] == ["track1"]
    writes = _cache_writes(controller, "prov_plugin")
    assert writes[0].kwargs["expiration"] == SEARCH_CACHE_EXPIRATION_LOCAL_PROVIDER


async def test_search_cache_write_failure_does_not_break_search() -> None:
    """A failing cache write still returns the provider results."""
    prov = _make_search_provider("prov_a")
    prov.search.return_value = SearchResults(tracks=[_make_track("track1", "prov_a", "My Song")])
    controller = _make_controller([prov])
    controller.mass.cache.set = AsyncMock(side_effect=RuntimeError("cache boom"))  # type: ignore[method-assign]

    result = await controller.search("My Song", media_types=[MediaType.TRACK], limit=5)

    assert [track.item_id for track in result.tracks] == ["track1"]


async def test_concurrent_identical_searches_share_one_provider_call() -> None:
    """Identical concurrent searches are coalesced into a single provider call."""
    prov = _make_search_provider("prov_a")

    async def _slowish_search(*_args: Any, **_kwargs: Any) -> SearchResults:
        await asyncio.sleep(0.05)
        return SearchResults(tracks=[_make_track("track1", "prov_a", "My Song")])

    prov.search.side_effect = _slowish_search
    controller = _make_controller([prov])

    result1, result2 = await asyncio.gather(
        controller.search("My Song", media_types=[MediaType.TRACK], limit=5),
        controller.search("My Song", media_types=[MediaType.TRACK], limit=5),
    )

    assert [track.item_id for track in result1.tracks] == ["track1"]
    assert [track.item_id for track in result2.tracks] == ["track1"]
    prov.search.assert_awaited_once()


async def test_search_dedup_filters_provider_items_already_in_library() -> None:
    """Provider items that map to a library item are filtered from the results."""
    library_track = Track(
        item_id="lib1",
        provider="library",
        name="Library Song",
        provider_mappings={
            ProviderMapping(
                item_id="track1",
                provider_domain="prov_a",
                provider_instance="prov_a",
            )
        },
    )
    prov = _make_search_provider("prov_a")
    prov.search.return_value = SearchResults(
        tracks=[
            _make_track("track1", "prov_a", "My Song"),
            _make_track("track2", "prov_a", "My Song 2"),
        ]
    )
    controller = _make_controller([prov])
    controller.search_library = AsyncMock(  # type: ignore[method-assign]
        return_value=SearchResults(tracks=[library_track])
    )

    result = await controller.search("My Song", media_types=[MediaType.TRACK], limit=5)

    assert [track.item_id for track in result.tracks] == ["lib1", "track2"]


async def test_search_dedup_filters_provider_radio_already_in_library() -> None:
    """Provider radio stations that map to a library radio station are filtered."""
    library_radio = Radio(
        item_id="lib1",
        provider="library",
        name="My Station",
        provider_mappings={
            ProviderMapping(
                item_id="radio1",
                provider_domain="prov_a",
                provider_instance="prov_a",
            )
        },
    )
    prov = _make_search_provider("prov_a")
    prov.search.return_value = SearchResults(
        radio=[
            Radio(
                item_id=item_id,
                provider="prov_a",
                name=name,
                provider_mappings={
                    ProviderMapping(
                        item_id=item_id, provider_domain="prov_a", provider_instance="prov_a"
                    )
                },
            )
            for item_id, name in (("radio1", "My Station"), ("radio2", "My Station 2"))
        ]
    )
    controller = _make_controller([prov])
    controller.search_library = AsyncMock(  # type: ignore[method-assign]
        return_value=SearchResults(radio=[library_radio])
    )

    result = await controller.search(
        "My Station", media_types=[MediaType.RADIO], limit=5, providers=["library", "prov_a"]
    )

    assert [radio.item_id for radio in result.radio] == ["lib1", "radio2"]


async def test_search_keeps_provider_items_in_library_when_library_excluded() -> None:
    """Without the library in the selection, provider items that are in the library are kept."""
    prov = _make_search_provider("prov_a")
    prov.search.return_value = SearchResults(tracks=[_make_track("track1", "prov_a", "My Song")])
    controller = _make_controller([prov])
    controller.search_library = AsyncMock(  # type: ignore[method-assign]
        return_value=SearchResults(
            tracks=[
                Track(
                    item_id="lib1",
                    provider="library",
                    name="My Song",
                    provider_mappings={
                        ProviderMapping(
                            item_id="track1",
                            provider_domain="prov_a",
                            provider_instance="prov_a",
                        )
                    },
                )
            ]
        )
    )

    result = await controller.search(
        "My Song", media_types=[MediaType.TRACK], limit=5, providers=["prov_a"]
    )

    assert [track.item_id for track in result.tracks] == ["track1"]
    controller.search_library.assert_not_awaited()


async def test_search_includes_genre_results_from_library() -> None:
    """Genre results from the library search end up in the combined search result."""
    library_genre = Genre(
        item_id="genre1", provider="library", name="Rock", provider_mappings=set()
    )
    prov = _make_search_provider("prov_a")
    controller = _make_controller([prov])
    controller.search_library = AsyncMock(  # type: ignore[method-assign]
        return_value=SearchResults(genres=[library_genre])
    )

    result = await controller.search("Rock", media_types=[MediaType.GENRE], limit=5)

    assert [genre.item_id for genre in result.genres] == ["genre1"]


async def test_search_providers_param_restricts_search() -> None:
    """The providers param restricts the search to the given providers."""
    prov_a = _make_search_provider("prov_a")
    prov_b = _make_search_provider("spotify--abc", domain="spotify")
    prov_b.search.return_value = SearchResults(
        tracks=[_make_track("track1", "spotify--abc", "My Song")]
    )
    controller = _make_controller([prov_a, prov_b])
    controller.search_library = AsyncMock(  # type: ignore[method-assign]
        return_value=SearchResults(tracks=[_make_track("lib1", "library", "My Song")])
    )

    # domain match: only the matching provider is searched, library results excluded
    result = await controller.search(
        "My Song", media_types=[MediaType.TRACK], limit=5, providers=["spotify"]
    )
    assert [track.item_id for track in result.tracks] == ["track1"]
    prov_a.search.assert_not_awaited()
    prov_b.search.assert_awaited_once()

    # the special value "library" selects the library only
    prov_b.search.reset_mock()
    result = await controller.search(
        "My Song", media_types=[MediaType.TRACK], limit=5, providers=["library"]
    )
    assert [track.item_id for track in result.tracks] == ["lib1"]
    prov_a.search.assert_not_awaited()
    prov_b.search.assert_not_awaited()


async def test_search_library_only_maps_to_library_provider() -> None:
    """The deprecated library_only flag behaves as providers=["library"]."""
    prov = _make_search_provider("prov_a")
    controller = _make_controller([prov])
    controller.search_library = AsyncMock(  # type: ignore[method-assign]
        return_value=SearchResults(tracks=[_make_track("lib1", "library", "My Song")])
    )

    result = await controller.search(
        "My Song", media_types=[MediaType.TRACK], limit=5, library_only=True
    )

    assert [track.item_id for track in result.tracks] == ["lib1"]
    prov.search.assert_not_awaited()


async def test_search_exact_library_match_skips_provider_media_types() -> None:
    """A (near) exact library match skips searching mapped providers for that media type."""
    prov = _make_search_provider("prov_a")
    controller = _make_controller([prov])
    controller.search_library = AsyncMock(  # type: ignore[method-assign]
        return_value=SearchResults(artists=[_make_library_artist("Nirvana", "prov_a")])
    )

    # the only requested media type is covered by the library: skip the provider
    result = await controller.search("Nirvana", media_types=[MediaType.ARTIST], limit=5)
    assert [artist.item_id for artist in result.artists] == ["lib1"]
    prov.search.assert_not_awaited()

    # other media types are still searched on the provider
    await controller.search("Nirvana", media_types=[MediaType.ARTIST, MediaType.TRACK], limit=5)
    prov.search.assert_awaited_once()
    assert prov.search.await_args.args[1] == [MediaType.TRACK]


async def test_search_exact_match_shortcut_skipped_for_explicit_providers() -> None:
    """An explicit providers selection always searches those providers."""
    prov = _make_search_provider("prov_a")
    controller = _make_controller([prov])
    controller.search_library = AsyncMock(  # type: ignore[method-assign]
        return_value=SearchResults(artists=[_make_library_artist("Nirvana", "prov_a")])
    )

    await controller.search(
        "Nirvana", media_types=[MediaType.ARTIST], limit=5, providers=["library", "prov_a"]
    )

    prov.search.assert_awaited_once()


@pytest.mark.parametrize("combined", [False, True])
async def test_search_preference_change_does_not_reuse_inflight_results(combined: bool) -> None:
    """A preference change separates caches and tasks from a suspended old search."""
    provider = _make_search_provider("ytmusic")
    provider.search_cache_key = "include_videos=False"
    controller = _make_controller([provider])
    started = asyncio.Event()
    release = asyncio.Event()

    async def backend(*_args: Any) -> SearchResults:
        preference = provider.search_cache_key
        if preference == "include_videos=False":
            started.set()
            await release.wait()
        return SearchResults(tracks=[_make_track(preference, "ytmusic", "Song")])

    provider.search.side_effect = backend
    entries: dict[tuple[str, str], SearchResults] = {}

    async def get_cache(*, key: str, provider: str, **_kwargs: Any) -> SearchResults | None:
        return entries.get((provider, key))

    async def set_cache(*, key: str, provider: str, data: Any, **_kwargs: Any) -> None:
        entries[provider, key] = SearchResults.from_dict(data)

    cast("AsyncMock", controller.mass.cache.get).side_effect = get_cache
    cast("AsyncMock", controller.mass.cache.set).side_effect = set_cache

    async def search() -> SearchResults | None:
        if combined:
            return await controller.search("Song", media_types=[MediaType.TRACK])
        return await controller._search_provider("Song", "ytmusic", [MediaType.TRACK])

    old_task = asyncio.create_task(search())
    await started.wait()
    provider.search_cache_key = "include_videos=True"
    current = await search()
    assert current is not None
    assert current.tracks[0].item_id == "include_videos=True"
    release.set()
    await old_task
    repeated = await search()
    assert repeated is not None
    assert repeated.tracks[0].item_id == "include_videos=True"
    assert provider.search.await_count == 2


@pytest.mark.parametrize("combined", [False, True])
async def test_search_reloaded_provider_uses_saved_preference(combined: bool) -> None:
    """Editing a disabled provider cannot reuse its previous search caches."""
    old_provider = _make_search_provider("ytmusic")
    old_provider.search_cache_key = "include_videos=False"
    providers = [old_provider]
    controller = _make_controller(providers)
    if combined:
        await controller.search("Song", media_types=[MediaType.TRACK])
    else:
        await controller._search_provider("Song", "ytmusic", [MediaType.TRACK])
    old_keys = {
        call.kwargs["key"] for call in cast("AsyncMock", controller.mass.cache.get).await_args_list
    }
    cast("AsyncMock", controller.mass.cache.get).side_effect = lambda **kwargs: (
        SearchResults() if kwargs["key"] in old_keys else None
    )
    reloaded = _make_search_provider("ytmusic")
    reloaded.search_cache_key = "include_videos=True"
    providers[:] = [reloaded]
    cast("AsyncMock", controller.mass.cache.get).reset_mock()
    if combined:
        await controller.search("Song", media_types=[MediaType.TRACK])
    else:
        await controller._search_provider("Song", "ytmusic", [MediaType.TRACK])
    assert all(
        call.kwargs["key"] not in old_keys
        for call in cast("AsyncMock", controller.mass.cache.get).await_args_list
    )
    reloaded.search.assert_awaited_once()


@pytest.mark.parametrize("pause_at", ["combined_cache", "library", "provider_cache", "dispatch"])
async def test_search_preference_change_before_dispatch_does_not_poison_cache(
    pause_at: str,
) -> None:
    """A changed preference before dispatch cannot populate the previous mode's cache."""
    provider = _make_search_provider("ytmusic")
    provider.search_cache_key = "include_videos=False"
    controller = _make_controller([provider])
    started = asyncio.Event()
    release = asyncio.Event()
    entries: dict[tuple[str, str], SearchResults] = {}
    paused = False

    async def pause() -> None:
        nonlocal paused
        if not paused:
            paused = True
            started.set()
            await release.wait()

    async def get_cache(*, key: str, provider: str, **_kwargs: Any) -> SearchResults | None:
        if (pause_at == "combined_cache" and provider == "music") or (
            pause_at == "provider_cache" and provider == "ytmusic"
        ):
            await pause()
        return entries.get((provider, key))

    async def set_cache(*, key: str, provider: str, data: Any, **_kwargs: Any) -> None:
        entries[provider, key] = SearchResults.from_dict(data)

    async def library(*_args: Any, **_kwargs: Any) -> SearchResults:
        if pause_at == "library":
            await pause()
        return SearchResults()

    async def backend(*_args: Any) -> SearchResults:
        return SearchResults(tracks=[_make_track(provider.search_cache_key, "ytmusic", "Song")])

    provider.search.side_effect = backend
    controller.search_library = AsyncMock(side_effect=library)  # type: ignore[method-assign]
    cast("AsyncMock", controller.mass.cache.get).side_effect = get_cache
    cast("AsyncMock", controller.mass.cache.set).side_effect = set_cache
    if pause_at == "dispatch":
        create_task = cast("Mock", controller.mass.create_task).side_effect

        async def delayed(target: Any) -> Any:
            await pause()
            return await target

        cast("Mock", controller.mass.create_task).side_effect = lambda target, **kwargs: (
            create_task(delayed(target), **kwargs)
        )

    async def search() -> SearchResults | None:
        if pause_at in ("combined_cache", "library"):
            return await controller.search("Song", media_types=[MediaType.TRACK])
        return await controller._search_provider("Song", "ytmusic", [MediaType.TRACK])

    pending = asyncio.create_task(search())
    await asyncio.wait_for(started.wait(), timeout=2)
    provider.search_cache_key = "include_videos=True"
    release.set()
    await pending
    provider.search.assert_not_awaited()
    assert not entries
    enabled = await search()
    assert enabled is not None
    assert enabled.tracks[0].item_id == "include_videos=True"
    provider.search_cache_key = "include_videos=False"
    disabled = await search()
    assert disabled is not None
    assert disabled.tracks[0].item_id == "include_videos=False"
    assert provider.search.await_count == 2
