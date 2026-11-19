"""Tests for the rules of the menu actions a provider declares."""

from __future__ import annotations

from typing import Any

import pytest
from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType
from music_assistant_models.provider import (
    MAX_MENU_ACTION_LABEL_LENGTH,
    ProviderMenuAction,
    ProviderPage,
)

from music_assistant.helpers.menu_actions import (
    MAX_MENU_ACTION_ICON_LENGTH,
    MAX_MENU_ACTIONS,
    read_menu_actions,
    read_page,
)

TYPES_PROBLEM = (
    "menu action 0: needs string id, label and scope, a list of string media_types, and "
    "optionally a string icon and a boolean requires_player"
)


def _action(**overrides: Any) -> dict[str, Any]:
    """Return a valid declared menu action, as in a manifest file, with the given fields changed."""
    return {
        "id": "send_to_frame",
        "label": "Show on the picture frame",
        "media_types": ["album", "playlist"],
        "scope": "library.read",
        "icon": "mdi-image-frame",
        **overrides,
    }


def test_valid_actions_kept_untouched() -> None:
    """Valid actions are kept as declared, in their order, without problems."""
    without_icon = _action(
        id="play_2",
        label="x" * MAX_MENU_ACTION_LABEL_LENGTH,
        media_types=["track", "podcast_episode", "audiobook"],
        scope="queues.control",
        requires_player=True,
    )
    del without_icon["icon"]
    kept, problems = read_menu_actions(
        [_action(), without_icon, _action(id="null_icon", icon=None)]
    )
    assert kept == [
        ProviderMenuAction(
            id="send_to_frame",
            label="Show on the picture frame",
            media_types=[MediaType.ALBUM, MediaType.PLAYLIST],
            scope=Scope.LIBRARY_READ,
            icon="mdi-image-frame",
        ),
        ProviderMenuAction(
            id="play_2",
            label="x" * MAX_MENU_ACTION_LABEL_LENGTH,
            media_types=[MediaType.TRACK, MediaType.PODCAST_EPISODE, MediaType.AUDIOBOOK],
            scope=Scope.QUEUES_CONTROL,
            requires_player=True,
        ),
        ProviderMenuAction(
            id="null_icon",
            label="Show on the picture frame",
            media_types=[MediaType.ALBUM, MediaType.PLAYLIST],
            scope=Scope.LIBRARY_READ,
        ),
    ]
    assert problems == []


@pytest.mark.parametrize(
    "label",
    [
        "Op de fotolijst tonen",
        "Показать на рамке",
        "عرض على الإطار",
        "フォトフレームに表示",
        "Frame 🖼️",
        "Family \U0001f468\u200d\U0001f469\u200d\U0001f467",
        "Persian می\u200cخواهم",  # noqa: RUF001 - Persian letters, not Latin look-alikes
    ],
)
def test_label_in_any_script_accepted(label: str) -> None:
    """A label in any script, emoji included, is kept."""
    kept, problems = read_menu_actions([_action(label=label)])
    assert [action.label for action in kept] == [label]
    assert problems == []


def test_not_a_list() -> None:
    """A menu_actions value that is not a list keeps no action."""
    assert read_menu_actions({"id": "send_to_frame"}) == ([], ["'menu_actions' must be a list"])


@pytest.mark.parametrize(
    "item",
    [
        "send_to_frame",
        None,
        {key: value for key, value in _action().items() if key != "scope"},
        {key: value for key, value in _action().items() if key != "label"},
        _action(id=5),
        _action(label=None),
        _action(label=["Show"]),
        _action(scope=None),
        _action(media_types="album"),
        _action(media_types=[1]),
        _action(icon=["mdi-image-frame"]),
        _action(icon=0),
        _action(icon=False),
        _action(icon=[]),
        _action(icon={}),
        _action(requires_player="false"),
        _action(requires_player=1),
    ],
)
def test_action_of_wrong_type_left_out(item: object) -> None:
    """An action with a field of the wrong type is left out with its problem; the others stay."""
    kept, problems = read_menu_actions([item, _action(id="other")])
    assert [action.id for action in kept] == ["other"]
    assert problems == [TYPES_PROBLEM]


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
        ({"icon": "mdi-" + "a" * (MAX_MENU_ACTION_ICON_LENGTH - 3)}, "needs an icon"),
        ({"label": ""}, "needs a label"),
        ({"label": "   "}, "needs a label"),
        ({"label": "x" * (MAX_MENU_ACTION_LABEL_LENGTH + 1)}, "needs a label"),
        ({"label": "Show\non the frame"}, "needs a label"),
        ({"label": "Show\u2028on the frame"}, "needs a label"),
        ({"label": "Show\u202eemarf eht no"}, "needs a label"),
        ({"label": "Show\u2067on the frame"}, "needs a label"),
        ({"label": "Show\x00"}, "needs a label"),
        ({"label": "\u200b"}, "needs a label"),
        ({"label": "\ufeff"}, "needs a label"),
        ({"label": "\u00ad"}, "needs a label"),
        ({"label": "\u200b\u200d "}, "needs a label"),
        ({"media_types": []}, "needs media_types"),
        ({"media_types": ["album", "unknown"]}, "needs media_types"),
        ({"media_types": ["album", "something_new"]}, "needs media_types"),
        ({"media_types": ["folder"]}, "needs media_types"),
        ({"media_types": ["genre"]}, "needs media_types"),
        ({"scope": "library.everything"}, "needs a scope"),
        ({"scope": ""}, "needs a scope"),
    ],
)
def test_action_breaking_a_rule_left_out(overrides: dict[str, Any], problem: str) -> None:
    """An action that breaks a rule is left out with one problem naming it; the others stay."""
    kept, problems = read_menu_actions([_action(**overrides), _action(id="other")])
    assert [action.id for action in kept] == ["other"]
    assert len(problems) == 1
    assert problems[0].startswith(f"menu action '{overrides.get('id', 'send_to_frame')}': ")
    assert problem in problems[0]


def test_icon_at_the_length_limit_accepted() -> None:
    """An icon name of exactly the limit is kept."""
    icon = "mdi-" + "a" * (MAX_MENU_ACTION_ICON_LENGTH - 4)
    kept, _ = read_menu_actions([_action(icon=icon)])
    assert [action.icon for action in kept] == [icon]


def test_second_action_with_same_id_left_out() -> None:
    """Of two actions with the same id the first is kept."""
    kept, problems = read_menu_actions([_action(), _action(label="Again")])
    assert [action.label for action in kept] == ["Show on the picture frame"]
    assert problems == [
        "menu action 'send_to_frame': another menu action of this provider has the same id"
    ]


def test_actions_beyond_the_limit_left_out() -> None:
    """Only the first valid actions up to the limit are kept, each one beyond it is reported."""
    actions = [_action(id=f"action_{index}") for index in range(MAX_MENU_ACTIONS + 2)]
    # an invalid action does not count towards the limit
    actions.insert(1, _action(id="broken", scope="nothing"))
    kept, problems = read_menu_actions(actions)
    assert [action.id for action in kept] == [
        f"action_{index}" for index in range(MAX_MENU_ACTIONS)
    ]
    assert problems[1:] == [
        f"menu action 'action_{index}': a provider declares at most {MAX_MENU_ACTIONS} menu actions"
        for index in (MAX_MENU_ACTIONS, MAX_MENU_ACTIONS + 1)
    ]


def _page(**overrides: Any) -> dict[str, Any]:
    """Return a valid declared page, as in a manifest file, with the given fields changed."""
    return {
        "label": "Picture frame",
        "scope": "library.write",
        "icon": "mdi-image-frame",
        **overrides,
    }


def test_valid_page_kept() -> None:
    """A page that follows the rules is kept as declared, with or without an icon."""
    without_icon = _page()
    del without_icon["icon"]
    assert read_page(_page()) == (
        ProviderPage(label="Picture frame", scope=Scope.LIBRARY_WRITE, icon="mdi-image-frame"),
        [],
    )
    assert read_page(without_icon) == (
        ProviderPage(label="Picture frame", scope=Scope.LIBRARY_WRITE),
        [],
    )
    assert read_page(None) == (None, [])


@pytest.mark.parametrize(
    "raw",
    [
        "Picture frame",
        [_page()],
        {"label": "Picture frame"},
        _page(label=None),
        _page(scope=3),
        _page(icon=True),
    ],
)
def test_page_of_wrong_type_left_out(raw: object) -> None:
    """A page with a field of the wrong type is left out with its problem."""
    assert read_page(raw) == (
        None,
        ["page: needs string label and scope, and optionally a string icon"],
    )


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"label": ""}, "needs a label"),
        ({"label": "x" * (MAX_MENU_ACTION_LABEL_LENGTH + 1)}, "needs a label"),
        ({"label": "Picture\nframe"}, "needs a label"),
        ({"label": "Picture\u202eframe"}, "needs a label"),
        ({"scope": "library.everything"}, "needs a scope"),
        ({"icon": "image-frame"}, "needs an icon"),
        ({"icon": "mdi-" + "a" * (MAX_MENU_ACTION_ICON_LENGTH - 3)}, "needs an icon"),
    ],
)
def test_page_breaking_a_rule_left_out(overrides: dict[str, Any], problem: str) -> None:
    """A page that breaks a rule is left out with one problem naming it."""
    page, problems = read_page(_page(**overrides))
    assert page is None
    assert len(problems) == 1
    assert problems[0].startswith(f"page: {problem}")
