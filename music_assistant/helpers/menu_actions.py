"""Rules for the menu actions a provider declares in its manifest."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from music_assistant_models.enums import MediaType

if TYPE_CHECKING:
    from music_assistant_models.provider import ProviderMenuAction

# the number of menu actions a provider may declare
MAX_MENU_ACTIONS: Final = 5
MAX_MENU_ACTION_LABEL_LENGTH: Final = 40
MAX_MENU_ACTION_ID_LENGTH: Final = 32
MENU_ACTION_ID_PATTERN: Final = re.compile(rf"[a-z0-9_]{{1,{MAX_MENU_ACTION_ID_LENGTH}}}")
MENU_ACTION_ICON_PATTERN: Final = re.compile(r"mdi-[a-z0-9]+(-[a-z0-9]+){0,7}")
# what follows "<domain>/" in the command of a menu action
MENU_ACTION_COMMAND_PATTERN: Final = re.compile(r"[a-z0-9_]{1,64}(/[a-z0-9_]{1,64}){0,3}")
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


def check_menu_actions(
    domain: str, actions: list[ProviderMenuAction]
) -> tuple[list[ProviderMenuAction], list[str]]:
    """
    Return the menu actions of a provider that follow the rules, and why each other one is left out.

    An action is left out when its id or icon is malformed, its command is not in the provider's
    own ``<domain>/`` namespace, its label is empty or too long, it names no media type or one
    without a menu, or its id is already taken. Only the first ``MAX_MENU_ACTIONS`` valid actions
    are kept.

    :param domain: Domain of the provider that declares the actions.
    :param actions: The menu actions from the manifest of the provider.
    """
    kept: list[ProviderMenuAction] = []
    problems: list[str] = []
    for action in actions:
        if problem := _find_problem(domain, action, kept):
            problems.append(f"menu action '{action.id}': {problem}")
        else:
            kept.append(action)
    return kept, problems


def _find_problem(
    domain: str, action: ProviderMenuAction, kept: list[ProviderMenuAction]
) -> str | None:
    """
    Return what is wrong with a single menu action, None when it may be kept.

    :param domain: Domain of the provider that declares the action.
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
    if not action.label.strip() or len(action.label) > MAX_MENU_ACTION_LABEL_LENGTH:
        return f"needs a label of 1 to {MAX_MENU_ACTION_LABEL_LENGTH} characters"
    if not action.media_types or not set(action.media_types) <= MENU_ACTION_MEDIA_TYPES:
        allowed = ", ".join(sorted(MENU_ACTION_MEDIA_TYPES))
        return f"needs media_types from: {allowed}"
    prefix = f"{domain}/"
    if not (
        action.command.startswith(prefix)
        and MENU_ACTION_COMMAND_PATTERN.fullmatch(action.command.removeprefix(prefix))
    ):
        return f"needs a command in the namespace of its provider ('{prefix}...')"
    if action.icon is not None and not MENU_ACTION_ICON_PATTERN.fullmatch(action.icon):
        return "needs an icon in the form of a material design icon name ('mdi-...')"
    if len(kept) >= MAX_MENU_ACTIONS:
        return f"a provider declares at most {MAX_MENU_ACTIONS} menu actions"
    return None
