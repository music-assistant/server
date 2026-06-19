"""Tests for the music controller helper functions."""

from __future__ import annotations

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Artist, ItemMapping, ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.music.helpers import sort_search_result


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
