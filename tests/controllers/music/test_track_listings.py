"""Tests for the track listings: searched, sorted and paged on the server."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import (
    ProviderFeature,
    ProviderSharing,
    ProviderType,
    SortDirection,
    SortField,
)
from music_assistant_models.errors import InvalidDataError, ProviderUnavailableError
from music_assistant_models.media_items import ProviderMapping, SearchResults

from music_assistant.controllers.music.constants import SIMILAR_ITEMS_LIMIT
from music_assistant.models.music_provider import MusicProvider
from tests.common import set_music_source_access

from .helpers import create_album, create_track

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from music_assistant.mass import MusicAssistant


@pytest.fixture(name="mass")
def mass_fixture(music_mass_with_cache: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance with a cache store: provider listings are cached."""
    return music_mass_with_cache


def _song_on(album_id: str, album_name: str) -> Track:
    """Return the test song as a search finds it on the given album."""
    track = create_track("qobuz_1", f"song_{album_id}", name="Song")
    track.album = create_album("qobuz_1", album_id, name=album_name)
    return track


def _names(items: list[Any]) -> list[str]:
    return [item.name for item in items]


async def test_track_albums_follow_the_listing_contract(mass: MusicAssistant) -> None:
    """The albums of a track are searched, sorted and paged, in search order by default."""
    await mass.music.albums.add_item_to_library(create_album("qobuz_1", "lib", name="Library"))
    search = AsyncMock(
        return_value=SearchResults(
            tracks=[
                _song_on("zebra", "Zebra"),
                _song_on("lib", "Library"),
                _song_on("apple", "Apple"),
            ]
        )
    )

    async def _albums(**kwargs: Any) -> list[str]:
        return _names(await mass.music.tracks.albums("song", "qobuz_1", **kwargs))

    with (
        patch.object(mass.music.tracks, "get", AsyncMock(return_value=_song_on("x", "X"))),
        patch.object(mass.music, "search", search),
    ):
        # the order the search finds them in
        assert await _albums() == ["Zebra", "Library", "Apple"]
        assert await _albums(sort_field=SortField.NAME) == ["Apple", "Library", "Zebra"]
        assert await _albums(search="zeb") == ["Zebra"]
        assert await _albums(limit=1, offset=2) == ["Apple"]
        assert await _albums(limit=0) == ["Zebra", "Library", "Apple"]
        assert await _albums(in_library_only=True) == ["Library"]


async def test_track_albums_reject_an_unsupported_sort_before_searching(
    mass: MusicAssistant,
) -> None:
    """A sort the albums of a track do not offer fails before anything is searched."""
    search = AsyncMock(return_value=SearchResults())
    with (
        patch.object(mass.music.tracks, "get", AsyncMock(return_value=_song_on("x", "X"))),
        patch.object(mass.music, "search", search),
        pytest.raises(InvalidDataError),
    ):
        await mass.music.tracks.albums("song", "qobuz_1", sort_field=SortField.DURATION)
    search.assert_not_awaited()


async def test_library_track_albums_are_the_whole_set(mass: MusicAssistant) -> None:
    """The library albums of a track are read without the listing page size."""
    query = AsyncMock(return_value=[])
    with patch.object(mass.music.albums, "get_library_items_by_query", query):
        await mass.music.tracks.get_library_track_albums("1")
    assert query.await_args is not None
    assert query.await_args.kwargs["limit"] == 0


def _metadata_tier(provider: Any) -> Any:
    """Return a providers-by-feature double that offers the provider in the metadata tier only."""
    return lambda _feature, priority=(): [provider] if ProviderType.METADATA in priority else []


def _similar_tracks_provider() -> MagicMock:
    """Return the music provider of the reference track, which lists similar tracks."""
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = "qobuz_1"
    provider.supported_features = {ProviderFeature.SIMILAR_TRACKS}
    return provider


async def test_similar_tracks_follow_the_listing_contract(mass: MusicAssistant) -> None:
    """The similar tracks are fetched once, in the provider's ranking, then sorted and paged."""
    similar = [
        create_track("qobuz_1", "t1", name="Zebra", duration=100),
        create_track("qobuz_1", "t2", name="Apple", duration=300),
        create_track("qobuz_1", "t3", name="Mango", duration=200),
    ]
    fetch = AsyncMock(return_value=(similar, None))

    async def _similar(provider: str = "qobuz_1", **kwargs: Any) -> list[str]:
        return _names(await mass.music.tracks.similar_tracks("ref", provider, **kwargs))

    with (
        patch.object(mass, "get_provider", return_value=_similar_tracks_provider()),
        patch.object(
            mass.music.tracks, "get", AsyncMock(return_value=create_track("qobuz_1", "ref"))
        ),
        patch.object(mass.music.tracks, "_get_similar_tracks_from_provider", fetch),
    ):
        assert await _similar() == ["Zebra", "Apple", "Mango"]
        # the domain and the instance it resolves to share one listing
        assert await _similar("qobuz") == ["Zebra", "Apple", "Mango"]
        assert await _similar(sort_field=SortField.DURATION, sort_direction=SortDirection.DESC) == [
            "Apple",
            "Mango",
            "Zebra",
        ]
        assert await _similar(search="ango") == ["Mango"]
        assert await _similar(limit=2) == ["Zebra", "Apple"]
        assert await _similar(limit=2, offset=2) == ["Mango"]
        with pytest.raises(InvalidDataError):
            await _similar(sort_field=SortField.YEAR)
    fetch.assert_awaited_once()
    assert fetch.await_args is not None
    # the provider is asked for the whole listing, whatever page is asked for
    assert fetch.await_args.args[2] == SIMILAR_ITEMS_LIMIT


@pytest.mark.parametrize(
    "variant", [{"allow_lookup": True}, {"preferred_provider_instances": ["qobuz_1"]}]
)
async def test_similar_tracks_of_a_variant_are_assembled_every_time(
    mass: MusicAssistant, variant: dict[str, Any]
) -> None:
    """A lookup or a provider preference changes what is assembled, so that listing is not kept."""
    fetch = AsyncMock(return_value=([create_track("qobuz_1", "t1", name="One")], None))
    with (
        patch.object(mass, "get_provider", return_value=_similar_tracks_provider()),
        patch.object(
            mass.music.tracks, "get", AsyncMock(return_value=create_track("qobuz_1", "ref"))
        ),
        patch.object(mass.music.tracks, "_get_similar_tracks_from_provider", fetch),
    ):
        for _ in range(2):
            tracks = await mass.music.tracks.similar_tracks("ref", "qobuz_1", **variant)
            assert _names(tracks) == ["One"]
    assert fetch.await_count == 2


async def test_similar_tracks_after_a_failing_provider_are_assembled_again(
    mass: MusicAssistant,
) -> None:
    """Similar tracks a failing provider might have answered first are served, not kept."""
    music_provider = _similar_tracks_provider()
    metadata_provider = SimpleNamespace(name="metadata")

    async def _fetch(provider: Any, *_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        if provider is music_provider:
            return None, ProviderUnavailableError("rate limited")
        return [create_track("qobuz_1", "t1", name="One")], None

    fetch = AsyncMock(side_effect=_fetch)
    with (
        patch.object(mass, "get_provider", return_value=music_provider),
        patch.object(
            mass, "get_providers_supporting_feature", side_effect=_metadata_tier(metadata_provider)
        ),
        patch.object(
            mass.music.tracks, "get", AsyncMock(return_value=create_track("qobuz_1", "ref"))
        ),
        patch.object(mass.music.tracks, "_get_similar_tracks_from_provider", fetch),
    ):
        for _ in range(2):
            assert _names(await mass.music.tracks.similar_tracks("ref", "qobuz_1")) == ["One"]
    # the failing provider and the fallback, on both calls
    assert fetch.await_count == 4


async def test_similar_tracks_missing_a_source_that_is_not_loaded_are_assembled_again(
    mass: MusicAssistant,
) -> None:
    """Similar tracks a mapped source that is not loaded might have answered first are not kept."""
    library_track = create_track("library", "1")
    library_track.provider_mappings = {
        ProviderMapping(item_id="ref", provider_domain="qobuz", provider_instance="qobuz_1")
    }
    metadata_provider = SimpleNamespace(name="metadata")
    fetch = AsyncMock(return_value=([create_track("qobuz_1", "t1", name="One")], None))
    with (
        # the track's own source is not loaded, so the fallback answers
        patch.object(mass, "get_provider", return_value=None),
        patch.object(
            mass, "get_providers_supporting_feature", side_effect=_metadata_tier(metadata_provider)
        ),
        patch.object(mass.music.tracks, "get", AsyncMock(return_value=library_track)),
        patch.object(mass.music.tracks, "_get_similar_tracks_from_provider", fetch),
    ):
        for _ in range(2):
            assert _names(await mass.music.tracks.similar_tracks("1", "library")) == ["One"]
    assert fetch.await_count == 2


async def test_similar_tracks_of_a_plugin_leave_out_hidden_sources(mass: MusicAssistant) -> None:
    """Similar tracks on a source the user may not see are left out of the shared listing."""
    set_music_source_access(
        mass,
        {
            "open_1": None,
            "hidden_1": ProviderAccess(owner="user-b", sharing=ProviderSharing.PRIVATE),
        },
    )
    sources = {
        source: SimpleNamespace(instance_id=source, type=ProviderType.MUSIC)
        for source in ("open_1", "hidden_1")
    }
    plugin = SimpleNamespace(name="plugin")
    # a track on both sources stays, as one source it sits on is visible
    both = create_track("hidden_1", "t3", name="Both")
    both.provider_mappings.add(
        ProviderMapping(item_id="t3", provider_domain="open", provider_instance="open_1")
    )
    similar = [
        create_track("open_1", "t1", name="Open"),
        create_track("hidden_1", "t2", name="Hidden"),
        both,
    ]
    fetch = AsyncMock(return_value=(similar, None))
    with (
        patch(
            "music_assistant.controllers.music.media.tracks.get_current_user",
        ) as mock_get_user,
        patch.object(
            mass,
            "get_provider",
            side_effect=lambda instance_id, **_kwargs: sources.get(instance_id),
        ),
        patch.object(
            mass,
            "get_providers_supporting_feature",
            side_effect=lambda _feature, priority=(): (
                [plugin] if ProviderType.PLUGIN in priority else []
            ),
        ),
        patch.object(
            mass.music.tracks, "get", AsyncMock(return_value=create_track("open_1", "ref"))
        ),
        patch.object(mass.music.tracks, "_get_similar_tracks_from_provider", fetch),
    ):
        # the owner of the hidden source sees both and fills the listing all users share
        mock_get_user.return_value = User(user_id="user-b", username="user-b", role=UserRole.USER)
        assert _names(await mass.music.tracks.similar_tracks("ref", "open_1")) == [
            "Open",
            "Hidden",
            "Both",
        ]
        mock_get_user.return_value = User(user_id="user-a", username="user-a", role=UserRole.USER)
        assert _names(await mass.music.tracks.similar_tracks("ref", "open_1")) == ["Open", "Both"]
    fetch.assert_awaited_once()
