"""
Trailing "на <player>" suffix recovery for platform-classified intents.

Yandex's static custom intents can't enumerate the per-user list of
player names, so the destination hint at the end of the spoken phrase
("пауза **на кухне**", "следующая **на спальне**") never makes it into
``request.nlu.intents.<form>.slots``. The webhook handler recovers it
from the raw command text after :class:`SkillManifestProvider` has
already produced a :class:`ParsedControl`.

This module is the only piece of provider-level NLU postprocessing
left after the v1.6.0 manifest refactor — :func:`build_grammar` /
:func:`build_entities` / :func:`parse_platform_intent` / the hardcoded
``_CONTROL_INTENT_MAP`` are all gone, replaced by data-driven dispatch
in :class:`SkillManifestProvider`.
"""

from __future__ import annotations

import re

# "на" boundary used to peel the trailing player-hint suffix off a
# platform-parsed command text. Yandex's static custom intents can't
# enumerate the per-user list of player names, so the suffix is
# recovered from the raw command text and attached to the ParsedControl
# alongside.
_NA_BOUNDARY_RE = re.compile(r"\s+на\s+", re.IGNORECASE)

# Hint candidates that are clearly not a player name. Phrases containing
# multiple "на" tokens (e.g. "громкость на 50 на кухне", "перемотай на 30
# секунд", "поставь на паузу на кухне") would otherwise misroute the
# slot-side "на N <unit>" or the action-side "на <noun>" as a hint.
#
# - ``_HINT_UNIT_WORDS`` covers unit nouns that follow numeric slots
#   ("на 30 секунд", "на 50 процентов").
# - ``_HINT_ACTION_WORDS`` covers action-content nouns from grammars
#   that themselves use "на <noun>" (currently only "паузу" from the
#   ``control.pause`` intent in skill.toml — "поставь на паузу" / "на
#   паузу"). When a new intent grammar introduces another such token,
#   add it here.
_HINT_UNIT_WORDS: frozenset[str] = frozenset(
    {
        "секунда",
        "секунды",
        "секунд",
        "сек",
        "минута",
        "минуты",
        "минут",
        "мин",
        "процент",
        "процента",
        "процентов",
    }
)
_HINT_ACTION_WORDS: frozenset[str] = frozenset(
    {
        "паузу",
    }
)


def extract_trailing_player_hint(text: str) -> str | None:
    """
    Return the lower-cased trailing "на <player>" suffix, or None.

    Examples:
        ``"пауза на кухне"`` → ``"кухне"``
        ``"поставь на паузу на кухне"`` → ``"кухне"`` (only the rightmost
        "на " is taken)
        ``"перемотай вперёд на 30 секунд"`` → ``None`` (the suffix is a
        slot value, not a player name)
        ``"громкость на 50"`` → ``None``

    The suffix is rejected when its first token starts with a digit or
    is one of the unit words ("секунд", "минут", "процентов" and their
    morphological variants) — those follow "на" as part of a numeric
    slot, not as a destination player.
    """
    if not text:
        return None
    parts = _NA_BOUNDARY_RE.split(text)
    if len(parts) < 2:
        return None
    hint = parts[-1].strip().lower()
    if not hint:
        return None
    first_token = hint.split(maxsplit=1)[0]
    if first_token[0].isdigit():
        return None
    if first_token in _HINT_UNIT_WORDS:
        return None
    # Single-token hint matching an action-content noun ("паузу") is
    # part of the action phrase, not a destination player.
    if " " not in hint and hint in _HINT_ACTION_WORDS:
        return None
    return hint
