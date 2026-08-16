"""Tests for the Library Automations rule dataclasses."""

from __future__ import annotations

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.library_automations.models import (
    ACTION_ADD_TO_PLAYLIST,
    CONDITION_LOGIC_AND,
    TRIGGER_MEDIA_ITEM_UNFAVORITED,
    AutomationAction,
    AutomationCondition,
    AutomationRule,
    AutomationTrigger,
    validate_rule_media_types,
)


class TestAutomationRuleRoundTrip:
    """Tests for AutomationRule.to_dict / from_dict."""

    def test_round_trip_preserves_all_fields(self) -> None:
        """to_dict / from_dict round-trip preserves the full rule."""
        original = AutomationRule(
            id="r1",
            name="Unfavorited to playlist",
            trigger=AutomationTrigger(
                type=TRIGGER_MEDIA_ITEM_UNFAVORITED,
                media_types=["track", "album"],
                params={"foo": "bar"},
            ),
            action=AutomationAction(
                type=ACTION_ADD_TO_PLAYLIST, params={"playlist_name": "Sorted Out"}
            ),
            enabled=False,
            conditions=[AutomationCondition(field="genre", operator="contains", value="rock")],
            condition_logic="OR",
        )
        recovered = AutomationRule.from_dict(original.to_dict())
        assert recovered == original

    def test_from_dict_generates_id_when_missing(self) -> None:
        """from_dict generates a fresh id when none is supplied."""
        rule = AutomationRule.from_dict(
            {
                "name": "test",
                "trigger": {"type": TRIGGER_MEDIA_ITEM_UNFAVORITED},
                "action": {"type": ACTION_ADD_TO_PLAYLIST, "params": {}},
            }
        )
        assert rule.id

    def test_from_dict_defaults_trigger_media_types_to_track(self) -> None:
        """A trigger dict without media_types defaults to ['track']."""
        trigger = AutomationTrigger.from_dict({"type": TRIGGER_MEDIA_ITEM_UNFAVORITED})
        assert trigger.media_types == ["track"]

    def test_from_dict_defaults_condition_logic(self) -> None:
        """condition_logic defaults to AND when omitted."""
        rule = AutomationRule.from_dict(
            {
                "id": "r2",
                "name": "test",
                "trigger": {"type": TRIGGER_MEDIA_ITEM_UNFAVORITED},
                "action": {"type": ACTION_ADD_TO_PLAYLIST, "params": {}},
            }
        )
        assert rule.condition_logic == CONDITION_LOGIC_AND

    def test_invalid_condition_logic_raises(self) -> None:
        """An unknown condition_logic value raises InvalidDataError."""
        with pytest.raises(InvalidDataError):
            AutomationRule.from_dict(
                {
                    "id": "r3",
                    "name": "test",
                    "trigger": {"type": TRIGGER_MEDIA_ITEM_UNFAVORITED},
                    "action": {"type": ACTION_ADD_TO_PLAYLIST, "params": {}},
                    "condition_logic": "XOR",
                }
            )

    def test_invalid_condition_operator_raises(self) -> None:
        """An unknown condition operator raises InvalidDataError."""
        with pytest.raises(InvalidDataError):
            AutomationCondition.from_dict({"field": "genre", "operator": "bogus", "value": "x"})

    def test_missing_name_raises(self) -> None:
        """A rule without a name raises InvalidDataError."""
        with pytest.raises(InvalidDataError):
            AutomationRule.from_dict(
                {
                    "id": "r4",
                    "trigger": {"type": TRIGGER_MEDIA_ITEM_UNFAVORITED},
                    "action": {"type": ACTION_ADD_TO_PLAYLIST, "params": {}},
                }
            )


class TestValidateRuleMediaTypes:
    """Tests for validate_rule_media_types."""

    def test_valid_media_types_pass(self) -> None:
        """track/album/artist are all accepted."""
        rule = AutomationRule(
            id="r1",
            name="test",
            trigger=AutomationTrigger(
                type=TRIGGER_MEDIA_ITEM_UNFAVORITED, media_types=["track", "album", "artist"]
            ),
            action=AutomationAction(type=ACTION_ADD_TO_PLAYLIST),
        )
        validate_rule_media_types(rule)  # should not raise

    def test_empty_media_types_raises(self) -> None:
        """An empty media_types list raises InvalidDataError."""
        rule = AutomationRule(
            id="r1",
            name="test",
            trigger=AutomationTrigger(type=TRIGGER_MEDIA_ITEM_UNFAVORITED, media_types=[]),
            action=AutomationAction(type=ACTION_ADD_TO_PLAYLIST),
        )
        with pytest.raises(InvalidDataError):
            validate_rule_media_types(rule)

    def test_unknown_media_type_raises(self) -> None:
        """A media_type outside track/album/artist raises InvalidDataError."""
        rule = AutomationRule(
            id="r1",
            name="test",
            trigger=AutomationTrigger(type=TRIGGER_MEDIA_ITEM_UNFAVORITED, media_types=["radio"]),
            action=AutomationAction(type=ACTION_ADD_TO_PLAYLIST),
        )
        with pytest.raises(InvalidDataError):
            validate_rule_media_types(rule)
