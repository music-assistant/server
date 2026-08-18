"""Tests for the `reachable_via` provider-mapping filter on library_items()."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.media_items import Artist, ProviderMapping, Track, UniqueList

from music_assistant.mass import MusicAssistant

if TYPE_CHECKING:
    import pytest


def _mapping(
    provider_instance: str,
    provider_domain: str,
    item_id: str,
    *,
    in_library: bool = True,
    available: bool = True,
) -> ProviderMapping:
    return ProviderMapping(
        item_id=item_id,
        provider_domain=provider_domain,
        provider_instance=provider_instance,
        in_library=in_library,
        available=available,
    )


async def _add_track(mass: MusicAssistant, name: str, mappings: set[ProviderMapping]) -> Track:
    """Add a library track (with a dummy library artist) with the given provider mappings."""
    artist = Artist(
        item_id="0",
        provider="library",
        name=f"{name} Artist",
        provider_mappings={_mapping("local_1", "local", f"{name}-artist")},
    )
    db_artist = await mass.music.artists.add_item_to_library(artist)
    track = Track(
        item_id="0",
        provider="library",
        name=name,
        provider_mappings=mappings,
        artists=UniqueList([db_artist]),
        disc_number=1,
        track_number=1,
    )
    return await mass.music.tracks.add_item_to_library(track)


async def test_reachable_via_includes_item_not_in_library_on_requested_provider(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A library item playable via Spotify (but not favorited there) must be included.

    The item is a library item because of its Local mapping (in_library=True); its
    Spotify mapping is not in Spotify's own library (in_library=False). Reachability
    must not require the *matched* mapping to be in-library, unlike the existing
    `provider` filter.
    """
    monkeypatch.setattr(
        mass.music, "get_active_provider_instances", lambda: ["local_1", "spotify_1"]
    )
    await _add_track(
        mass,
        "Track A",
        {
            _mapping("local_1", "local", "track-a-local"),
            _mapping("spotify_1", "spotify", "track-a-spotify", in_library=False),
        },
    )

    result = await mass.music.tracks.library_items(reachable_via=["spotify_1"])

    assert {t.name for t in result} == {"Track A"}


async def test_reachable_via_excludes_item_reachable_only_via_unloaded_provider(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale mapping to a provider that is no longer loaded cannot satisfy the filter."""
    monkeypatch.setattr(mass.music, "get_active_provider_instances", lambda: ["local_1"])
    await _add_track(
        mass,
        "Track B",
        {
            _mapping("local_1", "local", "track-b-local"),
            _mapping("spotify_1", "spotify", "track-b-spotify", in_library=False),
        },
    )

    result = await mass.music.tracks.library_items(reachable_via=["spotify_1"])

    assert result == []


async def test_reachable_via_excludes_item_with_unavailable_mapping(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mapping that is no longer available on the requested provider cannot match."""
    monkeypatch.setattr(
        mass.music, "get_active_provider_instances", lambda: ["local_1", "spotify_1"]
    )
    await _add_track(
        mass,
        "Track H",
        {
            _mapping("local_1", "local", "track-h-local"),
            _mapping("spotify_1", "spotify", "track-h-spotify", available=False),
        },
    )

    result = await mass.music.tracks.library_items(reachable_via=["spotify_1"])

    assert result == []


async def test_reachable_via_excludes_item_for_provider_outside_user_access(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A provider outside the current user's access (per get_active_provider_instances) is ignored.

    `get_active_provider_instances` already applies the current user's provider_filter, so a
    requested instance that it omits (whether unloaded or simply not allowed for this
    user) must not make the item reachable, even though a matching mapping exists.
    """
    monkeypatch.setattr(mass.music, "get_active_provider_instances", lambda: ["local_1"])
    await _add_track(
        mass,
        "Track C",
        {
            _mapping("local_1", "local", "track-c-local"),
            _mapping("spotify_1", "spotify", "track-c-spotify"),
        },
    )

    result = await mass.music.tracks.library_items(reachable_via=["local_1", "spotify_1"])

    assert {t.name for t in result} == {"Track C"}
    result = await mass.music.tracks.library_items(reachable_via=["spotify_1"])
    assert result == []


async def test_reachable_via_none_applies_no_filter(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting the filter (None) keeps existing unfiltered behavior."""
    monkeypatch.setattr(mass.music, "get_active_provider_instances", list)
    await _add_track(mass, "Track D", {_mapping("local_1", "local", "track-d-local")})

    result = await mass.music.tracks.library_items(reachable_via=None)

    assert {t.name for t in result} == {"Track D"}


async def test_reachable_via_explicit_empty_list_returns_no_items(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit empty list returns no items, distinct from omitting the filter."""
    monkeypatch.setattr(mass.music, "get_active_provider_instances", lambda: ["local_1"])
    await _add_track(mass, "Track E", {_mapping("local_1", "local", "track-e-local")})

    result = await mass.music.tracks.library_items(reachable_via=[])

    assert result == []


async def test_reachable_via_applies_to_random_order_by_subquery(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reachability filter is also applied when using the fast random subquery path."""
    monkeypatch.setattr(
        mass.music, "get_active_provider_instances", lambda: ["local_1", "spotify_1"]
    )
    await _add_track(
        mass,
        "Track F",
        {
            _mapping("local_1", "local", "track-f-local"),
            _mapping("spotify_1", "spotify", "track-f-spotify", in_library=False),
        },
    )
    await _add_track(mass, "Track G", {_mapping("local_1", "local", "track-g-local")})

    result = await mass.music.tracks.library_items(
        order_by="random_play_count", reachable_via=["spotify_1"]
    )

    assert {t.name for t in result} == {"Track F"}
