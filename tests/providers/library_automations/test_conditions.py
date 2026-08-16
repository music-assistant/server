"""Tests for the generic condition evaluator."""

from __future__ import annotations

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Track

from music_assistant.providers.library_automations import conditions
from music_assistant.providers.library_automations.models import AutomationCondition


def _make_track(name: str = "Song A", genres: set[str] | None = None) -> Track:
    track = Track(item_id="1", provider="library", name=name, provider_mappings=set())
    track.media_type = MediaType.TRACK
    if genres:
        track.metadata.genres = genres
    return track


def test_no_conditions_always_matches() -> None:
    """An empty condition list matches any item."""
    assert conditions.evaluate_conditions([], "AND", _make_track()) is True


def test_contains_operator_on_name_is_case_insensitive() -> None:
    """The 'contains' operator matches substrings regardless of case."""
    track = _make_track(name="Bohemian Rhapsody")
    match = AutomationCondition(field="name", operator="contains", value="rhapsody")
    no_match = AutomationCondition(field="name", operator="contains", value="xyz")
    assert conditions.evaluate_conditions([match], "AND", track) is True
    assert conditions.evaluate_conditions([no_match], "AND", track) is False


def test_contains_operator_on_genre_list() -> None:
    """The 'contains' operator on the genre field checks the genre set."""
    track = _make_track(genres={"Rock", "Pop"})
    match = AutomationCondition(field="genre", operator="contains", value="rock")
    no_match = AutomationCondition(field="genre", operator="contains", value="jazz")
    assert conditions.evaluate_conditions([match], "AND", track) is True
    assert conditions.evaluate_conditions([no_match], "AND", track) is False


def test_and_logic_requires_all_conditions() -> None:
    """AND logic only matches when every condition matches."""
    track = _make_track(name="Song A", genres={"Rock"})
    matching = AutomationCondition(field="name", operator="contains", value="song")
    failing = AutomationCondition(field="genre", operator="contains", value="jazz")
    assert conditions.evaluate_conditions([matching, failing], "AND", track) is False
    assert conditions.evaluate_conditions([matching], "AND", track) is True


def test_or_logic_requires_any_condition() -> None:
    """OR logic matches when at least one condition matches."""
    track = _make_track(name="Song A", genres={"Rock"})
    matching = AutomationCondition(field="name", operator="contains", value="song")
    failing = AutomationCondition(field="genre", operator="contains", value="jazz")
    assert conditions.evaluate_conditions([matching, failing], "OR", track) is True
    assert conditions.evaluate_conditions([failing], "OR", track) is False


def test_eq_operator_on_explicit_field() -> None:
    """The 'eq' operator compares the explicit metadata flag exactly."""
    track = _make_track()
    track.metadata.explicit = True
    match = AutomationCondition(field="explicit", operator="eq", value=True)
    no_match = AutomationCondition(field="explicit", operator="eq", value=False)
    assert conditions.evaluate_conditions([match], "AND", track) is True
    assert conditions.evaluate_conditions([no_match], "AND", track) is False
