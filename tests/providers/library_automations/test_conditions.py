"""Tests for the generic condition evaluator."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Track

from music_assistant.providers.library_automations import conditions
from music_assistant.providers.library_automations.models import AutomationCondition


def _make_track(item_id: str = "1", name: str = "Song A", genres: set[str] | None = None) -> Track:
    track = Track(item_id=item_id, provider="library", name=name, provider_mappings=set())
    track.media_type = MediaType.TRACK
    if genres:
        track.metadata.genres = genres
    return track


def _make_provider(playlist_tracks: dict[str, list[Track]] | None = None) -> MagicMock:
    """Create a provider stand-in whose playlists.tracks() serves the given fixture data."""
    provider = MagicMock()
    tracks_by_playlist = playlist_tracks or {}

    async def fake_tracks(playlist_id: str, _domain: str) -> AsyncGenerator[Track]:
        for track in tracks_by_playlist.get(playlist_id, []):
            yield track

    provider.mass.music.playlists.tracks = fake_tracks
    return provider


async def test_no_conditions_always_matches() -> None:
    """An empty condition list matches any item."""
    assert await conditions.evaluate_conditions([], "AND", _make_track(), _make_provider()) is True


async def test_contains_operator_on_name_is_case_insensitive() -> None:
    """The 'contains' operator matches substrings regardless of case."""
    track = _make_track(name="Bohemian Rhapsody")
    provider = _make_provider()
    match = AutomationCondition(field="name", operator="contains", value="rhapsody")
    no_match = AutomationCondition(field="name", operator="contains", value="xyz")
    assert await conditions.evaluate_conditions([match], "AND", track, provider) is True
    assert await conditions.evaluate_conditions([no_match], "AND", track, provider) is False


async def test_contains_operator_on_genre_list() -> None:
    """The 'contains' operator on the genre field checks the genre set."""
    track = _make_track(genres={"Rock", "Pop"})
    provider = _make_provider()
    match = AutomationCondition(field="genre", operator="contains", value="rock")
    no_match = AutomationCondition(field="genre", operator="contains", value="jazz")
    assert await conditions.evaluate_conditions([match], "AND", track, provider) is True
    assert await conditions.evaluate_conditions([no_match], "AND", track, provider) is False


async def test_and_logic_requires_all_conditions() -> None:
    """AND logic only matches when every condition matches."""
    track = _make_track(name="Song A", genres={"Rock"})
    provider = _make_provider()
    matching = AutomationCondition(field="name", operator="contains", value="song")
    failing = AutomationCondition(field="genre", operator="contains", value="jazz")
    assert (
        await conditions.evaluate_conditions([matching, failing], "AND", track, provider) is False
    )
    assert await conditions.evaluate_conditions([matching], "AND", track, provider) is True


async def test_or_logic_requires_any_condition() -> None:
    """OR logic matches when at least one condition matches."""
    track = _make_track(name="Song A", genres={"Rock"})
    provider = _make_provider()
    matching = AutomationCondition(field="name", operator="contains", value="song")
    failing = AutomationCondition(field="genre", operator="contains", value="jazz")
    assert await conditions.evaluate_conditions([matching, failing], "OR", track, provider) is True
    assert await conditions.evaluate_conditions([failing], "OR", track, provider) is False


async def test_eq_operator_on_explicit_field() -> None:
    """The 'eq' operator compares the explicit metadata flag exactly."""
    track = _make_track()
    track.metadata.explicit = True
    provider = _make_provider()
    match = AutomationCondition(field="explicit", operator="eq", value=True)
    no_match = AutomationCondition(field="explicit", operator="eq", value=False)
    assert await conditions.evaluate_conditions([match], "AND", track, provider) is True
    assert await conditions.evaluate_conditions([no_match], "AND", track, provider) is False


async def test_in_playlist_matches_when_track_is_member() -> None:
    """The 'in_playlist' field matches when the item's uri appears in one of the playlists."""
    track = _make_track(item_id="5")
    other_track = _make_track(item_id="9")
    provider = _make_provider({"12": [other_track, track]})
    condition = AutomationCondition(field="in_playlist", operator="in", value=["12"])
    assert await conditions.evaluate_conditions([condition], "AND", track, provider) is True


async def test_in_playlist_does_not_match_when_track_is_absent() -> None:
    """The 'in_playlist' field does not match when the item isn't in any listed playlist."""
    track = _make_track(item_id="5")
    other_track = _make_track(item_id="9")
    provider = _make_provider({"12": [other_track]})
    condition = AutomationCondition(field="in_playlist", operator="in", value=["12"])
    assert await conditions.evaluate_conditions([condition], "AND", track, provider) is False


async def test_in_playlist_checks_any_of_multiple_selected_playlists() -> None:
    """A multi-select in_playlist condition matches if the item is in ANY of the playlists."""
    track = _make_track(item_id="5")
    provider = _make_provider({"12": [], "13": [track]})
    condition = AutomationCondition(field="in_playlist", operator="in", value=["12", "13"])
    assert await conditions.evaluate_conditions([condition], "AND", track, provider) is True


async def test_in_playlist_ignores_unknown_playlist_id() -> None:
    """An invalid/deleted playlist id in the selection doesn't break evaluation."""
    track = _make_track(item_id="5")

    async def raising_tracks(playlist_id: str, _domain: str) -> AsyncGenerator[Track]:
        if playlist_id == "missing":
            raise MediaNotFoundError("no such playlist")
        yield track

    provider = MagicMock()
    provider.mass.music.playlists.tracks = raising_tracks
    condition = AutomationCondition(field="in_playlist", operator="in", value=["missing", "12"])
    assert await conditions.evaluate_conditions([condition], "AND", track, provider) is True
