"""Tests for the music controller helper functions."""

from __future__ import annotations

from typing import Any

from music_assistant_models.enums import MediaType
from music_assistant_models.helpers import set_global_cache_values
from music_assistant_models.media_items import Artist, ItemMapping, ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.music.helpers import preferred_thumb, sort_search_result


def _mapping(item_id: str, name: str, provider: str = "spotify") -> ItemMapping:
    """Build a minimal track ItemMapping for sort tests."""
    return ItemMapping(media_type=MediaType.TRACK, item_id=item_id, provider=provider, name=name)


def _track(item_id: str, name: str, provider: str = "spotify", artist: str | None = None) -> Track:
    """Build a minimal Track (optionally with a single artist) for sort tests."""
    artists: UniqueList[Artist | ItemMapping] = UniqueList()
    if artist is not None:
        artists.append(
            ItemMapping(
                media_type=MediaType.ARTIST, item_id=f"ar_{item_id}", provider=provider, name=artist
            )
        )
    return Track(
        item_id=item_id,
        provider=provider,
        name=name,
        artists=artists,
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain=provider, provider_instance=provider)
        },
    )


def test_empty_input_returns_empty_uniquelist() -> None:
    """An empty input yields an empty UniqueList."""
    items: list[ItemMapping] = []
    result = sort_search_result("anything", items)
    assert isinstance(result, UniqueList)
    assert result == []


def test_no_name_match_keeps_original_order() -> None:
    """Items whose name does not match the query are returned in their original order."""
    items = [_mapping("1", "Foo"), _mapping("2", "Bar")]
    assert sort_search_result("totally different", items) == items


def test_exact_name_match_ranked_first() -> None:
    """An item whose name matches the query is placed before non-matching items."""
    other = _mapping("1", "Something else")
    match = _mapping("2", "Hello")
    result = sort_search_result("hello", [other, match])
    assert result[0] is match
    assert other in result


def test_match_is_case_insensitive() -> None:
    """Name matching ignores case (and surrounding noise)."""
    match = _mapping("1", "HELLO")
    assert sort_search_result("hello", [match])[0] is match


def test_library_item_prioritized_over_streaming() -> None:
    """Between two exact name matches, the library item ranks first."""
    streaming = _mapping("1", "Hello", provider="spotify")
    library = _mapping("2", "Hello", provider="library")
    result = sort_search_result("hello", [streaming, library])
    assert result[0] is library


def test_artist_query_gives_bonus_to_matching_artist() -> None:
    """An 'artist - title' query gives a ranking bonus to the matching artist."""
    by_other = _track("1", "Hello", artist="Lionel Richie")
    by_adele = _track("2", "Hello", artist="Adele")
    result = sort_search_result("Adele - Hello", [by_other, by_adele])
    assert result[0] is by_adele
    assert by_other in result


def test_duplicate_items_are_deduplicated() -> None:
    """Equal items appearing more than once collapse to a single entry."""
    item = _mapping("1", "Hello")
    duplicate = _mapping("1", "Hello")
    result = sort_search_result("hello", [item, duplicate])
    assert len(result) == 1


def test_all_items_preserved_with_matches_first() -> None:
    """Every unique item is preserved; scored matches come before the rest."""
    library_match = _mapping("1", "Hello", provider="library")
    streaming_match = _mapping("2", "Hello", provider="spotify")
    non_match = _mapping("3", "Goodbye")
    result = sort_search_result("hello", [non_match, streaming_match, library_match])
    assert result[0] is library_match
    assert result[1] is streaming_match
    assert non_match in result
    assert len(result) == 3


def _image(provider: str, image_type: str = "thumb", remote: bool = False) -> dict[str, Any]:
    """Build a stored (raw) image of the given provider."""
    return {
        "type": image_type,
        "path": f"{provider}-cover",
        "provider": provider,
        "remotely_accessible": remote,
    }


async def test_preferred_thumb_skips_the_artwork_of_a_hidden_source() -> None:
    """A user is shown the artwork of their own source, not of one hidden from them."""
    await set_global_cache_values({"available_providers": {"sonic_a", "sonic_b"}})
    images = [_image("sonic_b"), _image("sonic_a")]
    assert preferred_thumb(images, {"sonic_b"}) == _image("sonic_a")
    assert preferred_thumb(images, {"sonic_a"}) == _image("sonic_b")


async def test_preferred_thumb_keeps_a_remotely_accessible_image_of_a_hidden_source() -> None:
    """A remotely accessible image needs no provider to resolve it, so it can be shown."""
    await set_global_cache_values({"available_providers": {"sonic_a", "sonic_b"}})
    remote = _image("sonic_b", remote=True)
    assert preferred_thumb([remote, _image("sonic_a")], {"sonic_b"}) == remote


async def test_preferred_thumb_skips_the_artwork_of_an_unloaded_provider() -> None:
    """Artwork of a provider that is not loaded can not be resolved, so it is passed over."""
    await set_global_cache_values({"available_providers": {"sonic_a"}})
    images = [_image("sonic_b"), _image("sonic_a")]
    assert preferred_thumb(images, set()) == _image("sonic_a")


def test_preferred_thumb_treats_every_provider_as_loaded_without_a_cache() -> None:
    """Without the providers cache every provider counts as loaded, like MediaItem.available."""
    images = [_image("sonic_b"), _image("sonic_a")]
    assert preferred_thumb(images, set()) == _image("sonic_b")
    assert preferred_thumb(images, {"sonic_b"}) == _image("sonic_a")


def test_preferred_thumb_falls_back_to_the_first_thumb() -> None:
    """With no thumb the viewer can be shown, the first thumb is returned."""
    images = [_image("sonic_b", image_type="fanart"), _image("sonic_b"), _image("sonic_c")]
    assert preferred_thumb(images, {"sonic_b", "sonic_c"}) == _image("sonic_b")


def test_preferred_thumb_without_thumbs() -> None:
    """Images without any thumb (or no images at all) yield no thumb."""
    assert preferred_thumb([_image("sonic_a", image_type="fanart")], set()) is None
    assert preferred_thumb(None, set()) is None
