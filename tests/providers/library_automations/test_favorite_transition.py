"""
Tests for the favorite/unfavorite transition detection in LibraryAutomationsProvider.

Music Assistant has no dedicated favorite/unfavorite event: toggling favorite status fires a
plain EventType.MEDIA_ITEM_UPDATED, indistinguishable at the event level from any other
metadata update to the same item. These tests are the regression coverage for that: a rule
must fire on a real True<->False transition, but NOT on an unrelated update to an item that
was already unfavorited (which would be a false positive - the actual bug this provider is
built to avoid).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Album, Track

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

from music_assistant.providers.library_automations import LibraryAutomationsProvider
from music_assistant.providers.library_automations.models import (
    AutomationAction,
    AutomationRule,
    AutomationTrigger,
)


def _make_plugin() -> LibraryAutomationsProvider:
    """Create a LibraryAutomationsProvider with mocked mass and empty in-memory stores."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "library_automations"
    config = MagicMock()
    config.values = {}
    config.get_value.side_effect = lambda _key, default=None: default
    plugin = LibraryAutomationsProvider(mass, manifest, config, set())
    plugin._rules = {}
    plugin._favorite_cache = {}
    return plugin


def _make_track(item_id: str, favorite: bool) -> Track:
    track = Track(
        item_id=item_id,
        provider="library",
        name=f"Track {item_id}",
        provider_mappings=set(),
        favorite=favorite,
    )
    track.media_type = MediaType.TRACK
    return track


async def _dispatch(plugin: LibraryAutomationsProvider, track: Track) -> None:
    await plugin._on_media_item_updated(cast("MassEvent", SimpleNamespace(data=track)))


@pytest.fixture
def matching_rule_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Patch actions.execute_action to record (rule_id, item_id) calls instead of running them."""
    calls: list[tuple[str, str]] = []

    async def fake_execute_action(_provider: object, rule: object, item: object) -> None:
        calls.append((rule.id, item.item_id))  # type: ignore[attr-defined]

    monkeypatch.setattr(
        "music_assistant.providers.library_automations.actions.execute_action",
        fake_execute_action,
    )
    return calls


def _add_unfavorite_rule(plugin: LibraryAutomationsProvider) -> str:
    rule = AutomationRule(
        id="rule-1",
        name="unfav to playlist",
        trigger=AutomationTrigger(type="media_item_unfavorited", media_types=["track"]),
        action=AutomationAction(type="add_to_playlist", params={"playlist_name": "Sorted Out"}),
    )
    plugin._rules[rule.id] = rule
    return rule.id


async def test_first_update_after_boot_does_not_trigger(
    matching_rule_calls: list[tuple[str, str]],
) -> None:
    """The first MEDIA_ITEM_UPDATED seen for an item only warms the cache, no trigger fires."""
    plugin = _make_plugin()
    _add_unfavorite_rule(plugin)
    await _dispatch(plugin, _make_track("1", favorite=True))
    assert matching_rule_calls == []


async def test_true_to_false_transition_fires_rule(
    matching_rule_calls: list[tuple[str, str]],
) -> None:
    """An actual favorite->unfavorite transition fires the matching rule exactly once."""
    plugin = _make_plugin()
    rule_id = _add_unfavorite_rule(plugin)
    await _dispatch(plugin, _make_track("1", favorite=True))  # warms cache
    await _dispatch(plugin, _make_track("1", favorite=False))  # real transition
    assert matching_rule_calls == [(rule_id, "1")]


async def test_repeated_update_without_favorite_change_does_not_retrigger(
    matching_rule_calls: list[tuple[str, str]],
) -> None:
    """A second update while already unfavorited must NOT re-fire the rule (the core bug)."""
    plugin = _make_plugin()
    _add_unfavorite_rule(plugin)
    await _dispatch(plugin, _make_track("1", favorite=True))  # warms cache
    await _dispatch(plugin, _make_track("1", favorite=False))  # real transition -> fires once
    matching_rule_calls.clear()
    # unrelated metadata update to the same, still-unfavorited item
    await _dispatch(plugin, _make_track("1", favorite=False))
    assert matching_rule_calls == []


async def test_false_to_true_transition_does_not_match_unfavorite_rule(
    matching_rule_calls: list[tuple[str, str]],
) -> None:
    """Re-favoriting fires the favorited trigger type, which this rule does not listen for."""
    plugin = _make_plugin()
    _add_unfavorite_rule(plugin)
    await _dispatch(plugin, _make_track("1", favorite=False))  # warms cache
    await _dispatch(plugin, _make_track("1", favorite=True))  # favorited transition
    assert matching_rule_calls == []


async def test_disabled_rule_is_never_dispatched(
    matching_rule_calls: list[tuple[str, str]],
) -> None:
    """A disabled rule does not fire even on a matching transition."""
    plugin = _make_plugin()
    rule_id = _add_unfavorite_rule(plugin)
    plugin._rules[rule_id].enabled = False
    await _dispatch(plugin, _make_track("1", favorite=True))
    await _dispatch(plugin, _make_track("1", favorite=False))
    assert matching_rule_calls == []


async def test_album_and_artist_transitions_are_also_tracked(
    matching_rule_calls: list[tuple[str, str]],
) -> None:
    """The same cache-based transition detection applies to albums and artists, not just tracks."""
    plugin = _make_plugin()
    rule = AutomationRule(
        id="rule-album",
        name="album unfav",
        trigger=AutomationTrigger(type="media_item_unfavorited", media_types=["album"]),
        action=AutomationAction(type="add_to_playlist", params={"playlist_name": "Sorted Out"}),
    )
    plugin._rules[rule.id] = rule

    def make_album(favorite: bool) -> Album:
        album = Album(
            item_id="9",
            provider="library",
            name="Some Album",
            provider_mappings=set(),
            favorite=favorite,
        )
        album.media_type = MediaType.ALBUM
        return album

    await plugin._on_media_item_updated(cast("MassEvent", SimpleNamespace(data=make_album(True))))
    await plugin._on_media_item_updated(cast("MassEvent", SimpleNamespace(data=make_album(False))))
    assert matching_rule_calls == [("rule-album", "9")]
