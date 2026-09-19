# ruff: noqa: RUF001
"""
Playback-control NLU + executor for the Yandex Dialogs custom skill.

As of v1.4.0 most of the control surface is recognised by Yandex itself
through declarative custom intents (see ``provider.dialogs_grammar``);
this module's regex parser only carries the few phrases that
fundamentally don't fit a static grammar — currently just ``transfer``
("переведи музыку на кухню"), where the destination is a per-user
dynamic enum of player names.

The webhook handler always tries the platform-side
``parse_platform_intent`` first; ``parse_control`` is the fallback for
phrases Yandex didn't pre-classify (e.g. before the skill update has
finished moderation, or for the dynamic-target ``transfer`` family).

The executor (`execute_control`) and confirmation-text helper
(`control_confirmation`) here remain authoritative regardless of which
parser produced the ``ParsedControl`` — they translate it to MA API
calls / spoken responses.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from music_assistant_models.enums import RepeatMode

from .dialogs_nlu import _PUNCT_RE, _SPACE_RE

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


_LOGGER = logging.getLogger(__name__)

ControlAction = Literal[
    "pause",
    "resume",
    "stop",
    "next",
    "previous",
    "volume_up",
    "volume_down",
    "volume_set",
    "volume_relative",  # value = signed delta (+20, -5); executor reads current vol + clamps
    "mute",
    "unmute",
    "list_players",
    "forget_player",
    "now_playing",  # info — handler reads queue.current_item.name
    "shuffle_on",
    "shuffle_off",
    "repeat_off",
    "repeat_one",
    "repeat_all",
    "seek_forward",  # value = positive seconds
    "seek_back",  # value = positive seconds; negated when dispatched
    "seek_start",  # absolute seek to 0
    "transfer",  # player_hint = TARGET player; SOURCE is the saved default
]


@dataclass(frozen=True, slots=True)
class ParsedControl:
    """Result of classifying a Yandex Dialogs voice command as a control action."""

    action: ControlAction
    value: int | None = None
    player_hint: str | None = None


# Transfer playback to a target player. The target name is captured into
# `player_hint`; SOURCE comes from the caller's `default_id`. Stays in
# regex because the destination is a per-user dynamic list of player
# names — a static custom-intent grammar can't enumerate it without
# regenerating the skill on every player-list change.
_TRANSFER_RE = re.compile(
    r"^(?:переведи|перенеси|продолжи)\s+(?:музыку\s+)?(?:на|в)\s+(?P<target>.+)$",
    re.IGNORECASE,
)


def parse_control(
    text: str,
    entities: list[Any] | None = None,  # noqa: ARG001  -- kept for backwards-compatible signature
) -> ParsedControl | None:
    """
    Classify a voice utterance as a regex-handled control command, or None.

    Recognises only ``transfer`` ("переведи на X") in v1.4.0+. Every
    other control command is recognised by Yandex itself through
    declarative custom intents (see :func:`provider.dialogs_grammar.parse_platform_intent`).

    Returns ``None`` when the phrase isn't a transfer — the caller
    typically falls through to the play-command parser.

    The ``entities`` parameter is preserved for API compatibility with
    callers that used to feed YANDEX.NUMBER through this function for
    relative-volume parsing (now handled platform-side).
    """
    if not text:
        return None
    cleaned = _PUNCT_RE.sub(" ", text)
    cleaned = _SPACE_RE.sub(" ", cleaned).strip()
    cleaned = re.sub(r"^алиса[,\s]+", "", cleaned, flags=re.IGNORECASE)
    if not cleaned:
        return None
    if tmatch := _TRANSFER_RE.match(cleaned):
        return ParsedControl(
            action="transfer",
            player_hint=tmatch.group("target").strip().lower(),
        )
    return None


# ---------------------------------------------------------------------------
# Executor + confirmation
# ---------------------------------------------------------------------------


def _plural_ru(n: int, forms: tuple[str, str, str]) -> str:
    """
    Pick the correct Russian quantitative form for `n`.

    :param n: The number.
    :param forms: ``(form_for_1, form_for_2_to_4, form_for_5_plus)``.

    Russian quantitative agreement:
      1, 21, 31, … → form_for_1 (e.g. "колонку")
      2-4, 22-24, … → form_for_2_to_4 ("колонки")
      0, 5-20, 25-30, … → form_for_5_plus ("колонок")
    """
    n_abs = abs(n)
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return forms[0]
    if 2 <= n_abs % 10 <= 4 and not 12 <= n_abs % 100 <= 14:
        return forms[1]
    return forms[2]


def format_list_players(players: list[Any]) -> str:
    """Build the spoken response listing exposed players for `list_players` action."""
    n = len(players)
    if n == 0:
        return "Не вижу ни одной колонки."
    names = ", ".join(getattr(p, "name", None) or p.player_id for p in players)
    if n == 1:
        return f"Вижу одну колонку: {names}."
    word = _plural_ru(n, ("колонку", "колонки", "колонок"))
    return f"Вижу {n} {word}: {names}."


def control_confirmation(control: ParsedControl) -> str:  # noqa: PLR0911
    """
    User-facing confirmation text for a control action.

    Caveat: ``list_players`` is **not** confirmed here — the handler builds
    the response text from the live player list via ``format_list_players``.
    """
    action = control.action
    if action == "pause":
        return "Пауза."
    if action == "resume":
        return "Продолжаю."
    if action == "stop":
        return "Остановил."
    if action == "next":
        return "Следующая."
    if action == "previous":
        return "Предыдущая."
    if action == "volume_up":
        return "Громче."
    if action == "volume_down":
        return "Тише."
    if action == "volume_set":
        return f"Громкость {control.value}."
    if action == "volume_relative":
        delta = control.value or 0
        if delta > 0:
            return f"Громче на {delta}."
        if delta < 0:
            return f"Тише на {-delta}."
        return "Готово."
    if action == "mute":
        return "Звук выключен."
    if action == "unmute":
        return "Звук включен."
    if action == "forget_player":
        return "Хорошо, забыл колонку. В следующий раз спрошу."
    if action == "shuffle_on":
        return "Включил перемешивание."
    if action == "shuffle_off":
        return "Выключил перемешивание."
    if action == "repeat_off":
        return "Выключил повтор."
    if action == "repeat_one":
        return "Повтор песни."
    if action == "repeat_all":
        return "Повтор очереди."
    if action == "seek_forward":
        return f"Перемотал на {control.value} секунд вперёд."
    if action == "seek_back":
        return f"Перемотал на {control.value} секунд назад."
    if action == "seek_start":
        return "Перемотал к началу."
    # list_players / now_playing / transfer — handler computes the real
    # text (live data) and never calls this. Placeholder for safety.
    return "Готово."


async def execute_control(  # noqa: PLR0915
    mass: MusicAssistant,
    control: ParsedControl,
    player: Any,
) -> None:
    """
    Dispatch a ParsedControl to the matching MA command.

    Errors are logged and swallowed — Alice has already been told the
    action was accepted; we don't have a channel to surface failures
    back into the same conversation.

    Note: ``list_players`` is a member of ``ControlAction`` for typing
    convenience, but it's an *informational* query handled inline by
    ``DialogsWebhookHandler._handle_control`` (which never calls this
    function for it). The explicit branch below makes that contract
    safe — a stray call won't silently no-op, it logs and returns.
    """
    pid = player.player_id
    action = control.action
    try:
        if action == "pause":
            await mass.player_queues.pause(pid)
        elif action == "resume":
            await mass.player_queues.resume(pid)
        elif action == "stop":
            await mass.player_queues.stop(pid)
        elif action == "next":
            await mass.player_queues.next(pid)
        elif action == "previous":
            await mass.player_queues.previous(pid)
        elif action == "volume_up":
            await mass.players.cmd_volume_up(pid)
        elif action == "volume_down":
            await mass.players.cmd_volume_down(pid)
        elif action == "volume_set":
            value = max(0, min(100, control.value or 0))
            await mass.players.cmd_volume_set(pid, value)
        elif action == "volume_relative":
            # Read current volume, apply signed delta, clamp [0, 100].
            # Falls back to 50 if the player exposes no volume_level
            # (some virtual players do); the user feedback ("Громче на 20")
            # then becomes a no-op rather than mis-targeting.
            delta = control.value or 0
            current = getattr(player, "volume_level", None)
            if not isinstance(current, int | float):
                current = 50
            new_value = max(0, min(100, int(current) + int(delta)))
            await mass.players.cmd_volume_set(pid, new_value)
        elif action == "mute":
            await mass.players.cmd_volume_mute(pid, True)
        elif action == "unmute":
            await mass.players.cmd_volume_mute(pid, False)
        elif action == "list_players":
            # Informational query — the handler builds the response
            # text from a live `list_exposed_players(...)` call and
            # never dispatches here. If we somehow got called for
            # this action it's a caller bug, not something to silently
            # ignore.
            _LOGGER.warning(
                "execute_control called with action='list_players'; "
                "this is informational and should be handled by the "
                "webhook handler, not dispatched here. Skipping.",
            )
        elif action == "forget_player":
            # State-management query — the handler clears the cached
            # default-player from session/application/cache state and
            # never dispatches here. Defensive branch.
            _LOGGER.warning(
                "execute_control called with action='forget_player'; "
                "this is a state-management op handled by the webhook "
                "handler, not dispatched here. Skipping.",
            )
        elif action == "shuffle_on":
            await mass.player_queues.set_shuffle(pid, shuffle_enabled=True)
        elif action == "shuffle_off":
            await mass.player_queues.set_shuffle(pid, shuffle_enabled=False)
        elif action == "repeat_off":
            # NB: set_repeat is sync, not async — do NOT await.
            mass.player_queues.set_repeat(pid, RepeatMode.OFF)
        elif action == "repeat_one":
            mass.player_queues.set_repeat(pid, RepeatMode.ONE)
        elif action == "repeat_all":
            mass.player_queues.set_repeat(pid, RepeatMode.ALL)
        elif action == "seek_forward":
            await mass.player_queues.skip(pid, seconds=control.value or 0)
        elif action == "seek_back":
            await mass.player_queues.skip(pid, seconds=-(control.value or 0))
        elif action == "seek_start":
            await mass.player_queues.seek(pid, position=0)
        elif action in ("now_playing", "transfer"):
            # Live-data / multi-player actions — the handler builds the
            # response from queue.current_item / transfer_queue and
            # never dispatches here. Defensive branch.
            _LOGGER.warning(
                "execute_control called with action=%r — handled by webhook "
                "handler, not dispatched here. Skipping.",
                action,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _LOGGER.exception("execute_control(%s) failed for player %s", action, pid)
