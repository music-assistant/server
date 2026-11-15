"""Tests for the rules of the menu actions a provider declares."""

from __future__ import annotations

from typing import Any

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.provider import ProviderMenuAction

from music_assistant.helpers.menu_actions import (
    MAX_MENU_ACTION_LABEL_LENGTH,
    MAX_MENU_ACTIONS,
    check_menu_actions,
)

DOMAIN = "frame"


def _action(**overrides: Any) -> ProviderMenuAction:
    """Return a valid menu action of the frame provider, with the given fields changed."""
    fields: dict[str, Any] = {
        "id": "send_to_frame",
        "label": "Show on the picture frame",
        "media_types": [MediaType.ALBUM, MediaType.PLAYLIST],
        "command": "frame/send_to_frame",
        "icon": "mdi-image-frame",
        **overrides,
    }
    return ProviderMenuAction(**fields)


def test_valid_actions_kept_untouched() -> None:
    """Valid actions are kept as they are, in their order, without problems."""
    actions = [
        _action(),
        _action(
            id="play_2",
            label="x" * MAX_MENU_ACTION_LABEL_LENGTH,
            media_types=[MediaType.TRACK, MediaType.PODCAST_EPISODE, MediaType.AUDIOBOOK],
            command="frame/play/on_frame",
            icon=None,
            plays=True,
        ),
    ]
    kept, problems = check_menu_actions(DOMAIN, list(actions))
    assert kept == actions
    assert problems == []


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"id": "Send"}, "needs an id"),
        ({"id": ""}, "needs an id"),
        ({"id": "send-to-frame"}, "needs an id"),
        ({"id": "x" * 33}, "needs an id"),
        ({"icon": "image-frame"}, "needs an icon"),
        ({"icon": "mdi-Image"}, "needs an icon"),
        ({"icon": "mdi-image frame"}, "needs an icon"),
        ({"command": "other/send_to_frame"}, "needs a command"),
        ({"command": "frames/send_to_frame"}, "needs a command"),
        ({"command": "frame/"}, "needs a command"),
        ({"command": "frame/../players/cmd"}, "needs a command"),
        ({"command": "send_to_frame"}, "needs a command"),
        ({"label": ""}, "needs a label"),
        ({"label": "   "}, "needs a label"),
        ({"label": "x" * (MAX_MENU_ACTION_LABEL_LENGTH + 1)}, "needs a label"),
        ({"media_types": []}, "needs media_types"),
        ({"media_types": [MediaType.ALBUM, MediaType.UNKNOWN]}, "needs media_types"),
        ({"media_types": [MediaType.FOLDER]}, "needs media_types"),
        ({"media_types": [MediaType.GENRE]}, "needs media_types"),
    ],
)
def test_action_breaking_a_rule_left_out(overrides: dict[str, Any], problem: str) -> None:
    """An action that breaks a rule is left out with one problem naming it; the others stay."""
    valid = _action(id="other")
    kept, problems = check_menu_actions(DOMAIN, [_action(**overrides), valid])
    assert kept == [valid]
    assert len(problems) == 1
    assert problems[0].startswith(f"menu action '{overrides.get('id', 'send_to_frame')}': ")
    assert problem in problems[0]


def test_second_action_with_same_id_left_out() -> None:
    """Of two actions with the same id the first is kept."""
    first = _action()
    kept, problems = check_menu_actions(DOMAIN, [first, _action(label="Again")])
    assert kept == [first]
    assert problems == [
        "menu action 'send_to_frame': another menu action of this provider has the same id"
    ]


def test_actions_beyond_the_limit_left_out() -> None:
    """Only the first valid actions up to the limit are kept, each one beyond it is reported."""
    actions = [_action(id=f"action_{index}") for index in range(MAX_MENU_ACTIONS + 2)]
    # an invalid action does not count towards the limit
    actions.insert(1, _action(id="broken", command="other/broken"))
    kept, problems = check_menu_actions(DOMAIN, actions)
    assert kept == [action for action in actions if action.id != "broken"][:MAX_MENU_ACTIONS]
    assert problems[1:] == [
        f"menu action 'action_{index}': a provider declares at most {MAX_MENU_ACTIONS} menu actions"
        for index in (MAX_MENU_ACTIONS, MAX_MENU_ACTIONS + 1)
    ]
