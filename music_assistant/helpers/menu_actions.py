"""Rules for the menu actions a provider declares in its manifest."""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType
from music_assistant_models.provider import ProviderMenuAction

# the number of menu actions a provider may declare
MAX_MENU_ACTIONS: Final = 5
MAX_MENU_ACTION_LABEL_LENGTH: Final = 40
MAX_MENU_ACTION_ID_LENGTH: Final = 32
MAX_MENU_ACTION_ICON_LENGTH: Final = 64
MENU_ACTION_ID_PATTERN: Final = re.compile(rf"[a-z0-9_]{{1,{MAX_MENU_ACTION_ID_LENGTH}}}")
MENU_ACTION_ICON_PATTERN: Final = re.compile(r"mdi-[a-z0-9]+(-[a-z0-9]+)*")
# the media types that have a menu in the apps
MENU_ACTION_MEDIA_TYPES: Final = frozenset(
    {
        MediaType.TRACK,
        MediaType.ALBUM,
        MediaType.ARTIST,
        MediaType.PLAYLIST,
        MediaType.RADIO,
        MediaType.PODCAST,
        MediaType.PODCAST_EPISODE,
        MediaType.AUDIOBOOK,
    }
)
# what breaks a label out of its single line or reorders the text around it: control
# characters, line and paragraph separators, and the bidirectional embeddings, overrides
# and isolates
_LABEL_FORBIDDEN_CATEGORIES: Final = frozenset({"Cc", "Zl", "Zp"})
_LABEL_FORBIDDEN_CHARACTERS: Final = frozenset(
    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)


def read_menu_actions(raw: object) -> tuple[list[ProviderMenuAction], list[str]]:
    """
    Return the menu actions of a provider that follow the rules, and why each other one is left out.

    Takes ``menu_actions`` as it is in the manifest file. An action is left out when a field has
    the wrong type, its id or icon is malformed, its label is not one line of 1 to
    ``MAX_MENU_ACTION_LABEL_LENGTH`` printable characters, it names no media type or one without
    a menu, its scope is unknown, or its id is already taken. Only the first ``MAX_MENU_ACTIONS``
    valid actions are kept. Never raises.

    :param raw: The value of ``menu_actions`` in the manifest of the provider.
    """
    if not isinstance(raw, list):
        return [], ["'menu_actions' must be a list"]
    kept: list[ProviderMenuAction] = []
    problems: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not _has_field_types(item):
            problems.append(
                f"menu action {index}: needs string id, label and scope, a list of string "
                "media_types, and optionally a string icon and a boolean requires_player"
            )
            continue
        action = ProviderMenuAction.from_dict(item)
        if problem := _find_problem(action, kept):
            problems.append(f"menu action '{action.id}': {problem}")
        else:
            kept.append(action)
    return kept, problems


def _has_field_types(item: dict[str, object]) -> bool:
    """Return True when a declared menu action has the types its fields need."""
    media_types = item.get("media_types")
    return (
        all(isinstance(item.get(key), str) for key in ("id", "label", "scope"))
        and isinstance(media_types, list)
        and all(isinstance(media_type, str) for media_type in media_types)
        and isinstance(item.get("icon") or "", str)
        and isinstance(item.get("requires_player", False), bool)
    )


def _find_problem(action: ProviderMenuAction, kept: list[ProviderMenuAction]) -> str | None:
    """
    Return what is wrong with a single menu action, None when it may be kept.

    :param action: The menu action to check.
    :param kept: The actions of the provider that were kept so far.
    """
    if not MENU_ACTION_ID_PATTERN.fullmatch(action.id):
        return (
            "needs an id of lower case letters, digits and underscores "
            f"(at most {MAX_MENU_ACTION_ID_LENGTH})"
        )
    if any(other.id == action.id for other in kept):
        return "another menu action of this provider has the same id"
    if not _is_label(action.label):
        return (
            f"needs a label of 1 to {MAX_MENU_ACTION_LABEL_LENGTH} characters on one line, "
            "without control characters"
        )
    if not action.media_types or not set(action.media_types) <= MENU_ACTION_MEDIA_TYPES:
        allowed = ", ".join(sorted(MENU_ACTION_MEDIA_TYPES))
        return f"needs media_types from: {allowed}"
    if action.scope == Scope.UNKNOWN:
        return "needs a scope that Music Assistant knows, such as 'library.read'"
    if action.icon is not None and not (
        len(action.icon) <= MAX_MENU_ACTION_ICON_LENGTH
        and MENU_ACTION_ICON_PATTERN.fullmatch(action.icon)
    ):
        return (
            "needs an icon in the form of a material design icon name ('mdi-...') "
            f"of at most {MAX_MENU_ACTION_ICON_LENGTH} characters"
        )
    if len(kept) >= MAX_MENU_ACTIONS:
        return f"a provider declares at most {MAX_MENU_ACTIONS} menu actions"
    return None


def _is_label(label: str) -> bool:
    """Return True when a label is one line of printable text of an allowed length."""
    return (
        bool(label.strip())
        and len(label) <= MAX_MENU_ACTION_LABEL_LENGTH
        and not any(
            unicodedata.category(char) in _LABEL_FORBIDDEN_CATEGORIES
            or char in _LABEL_FORBIDDEN_CHARACTERS
            for char in label
        )
    )
