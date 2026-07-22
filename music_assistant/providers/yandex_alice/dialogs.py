# ruff: noqa: RUF001, RUF003
"""
HTTP handler for the Yandex Dialogs custom-skill webhook (experimental).

Registers a single exact route on the MA webserver — the secret is
**baked into the path string** at registration time, not a route
template variable:

  POST /api/yandex_dialogs/webhook/<secret-as-literal-segment>

Therefore ``request.match_info`` is empty in production; the handler
parses the secret from ``request.path`` (last segment) for the
constant-time compare. Tests that pass an explicit ``match_info`` cover
the alternative branch.

Yandex Dialogs does not send an Authorization header on webhook calls,
so authentication is two-layered:

  1. Path secret (``CONF_DIALOG_WEBHOOK_SECRET``) — random UUID stored
     only in the user's MA config and in the skill's Backend URL. Knowing
     it requires access to the Yandex Dialogs developer console.
  2. ``body.session.skill_id == CONF_DIALOG_SKILL_ID`` — sanity check;
     skill_id is not secret on its own but stops cross-skill misroutes.

A request is rejected with 404 if the secret doesn't match (no leak via
401 timing) and with 401 if the skill_id doesn't match (configured
skill received a payload from a different skill — should never happen).

Session memory: three-tier strategy.

  1. Yandex state envelope — primary. ``state.session`` (per
     conversation), ``state.application`` (per device), ``state.user``
     (per Yandex account, only when account-linked). The application
     and user tiers persist across plugin reloads and MA restarts.
  2. In-process cache — third-tier fallback for surfaces that don't
     reliably echo the state envelope back (notably some Yandex
     Station configurations). LRU keyed by ``user.user_id`` →
     ``application.application_id`` → ``session_id``, with a 5-min
     TTL matching Alice's session inactivity timeout.

Reads check tiers in the order above. Writes mirror to all
applicable tiers via ``_yandex_response``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .constants import (
    DIALOG_RESOLVE_TIMEOUT,
    DIALOG_WEBHOOK_BASE_PATH,
)
from .dialogs_control import (
    ParsedControl,
    control_confirmation,
    execute_control,
    format_list_players,
    parse_control,
)
from .dialogs_grammar import extract_trailing_player_hint
from .dialogs_nlu import (
    _VERB_RE,
    ParsedCommand,
    list_exposed_players,
    parse_command,
    resolve_player,
    resolve_player_candidates,
)
from .dialogs_player import play_for_alice, resolve_query
from .skill_manifest_provider import SkillManifestProvider
from .tts_dictionary import PHRASE_REPLACEMENTS, WORD_REPLACEMENTS

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


_LOGGER = logging.getLogger(__name__)


# P0.2 — TTS pronunciation hints. The `_tts_for` helper rewrites known
# words to add `+` stress markers (Russian) or Cyrillic transliterations
# (foreign artist names) so Alice's TTS reads them naturally. Tables
# live in `tts_dictionary.py` for easier PR contributions; the regex
# matches BOTH Latin and Cyrillic words because foreign artist names
# arrive in Latin (e.g., the user said "Metallica" → command keeps it
# Latin → response text says "Metallica" → tts says "мет+аллика").
_TTS_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+")


def _tts_for(text: str) -> str:
    """
    Add `+` stress markers and foreign-name transliterations for Alice TTS.

    Two passes:
      1. Multi-word phrase replacement (longest first via the table's
         declared order). Required for "Iron Maiden", "Pink Floyd" etc.
         which the per-word regex cannot match across whitespace.
      2. Per-word substitution against ``WORD_REPLACEMENTS`` — covers
         Russian response stresses and single-word foreign names.

    Unknown words pass through unchanged. The map is intentionally small
    and curated — every entry is maintenance debt; add via PR when a
    real-user log shows Alice mispronouncing a specific word.
    """
    if not text:
        return text

    # Phrase pass — case-insensitive whole-substring replacement. Walks
    # the table in declared order so longer phrases (e.g. "red hot chili
    # peppers") match before any sub-string entries. Result drops the
    # original casing on the matched span — for TTS-only output that's
    # acceptable (Alice doesn't render visual casing on voice surfaces;
    # screen surfaces read `text`, not `tts`).
    lowered = text.lower()
    if any(phrase in lowered for phrase, _ in PHRASE_REPLACEMENTS):
        for phrase, replacement in PHRASE_REPLACEMENTS:
            idx = lowered.find(phrase)
            while idx != -1:
                text = text[:idx] + replacement + text[idx + len(phrase) :]
                lowered = text.lower()
                idx = lowered.find(phrase, idx + len(replacement))

    def _sub(match: re.Match[str]) -> str:
        word = match.group(0)
        replacement = WORD_REPLACEMENTS.get(word.lower())
        if replacement is None:
            return word
        if word[:1].isupper():
            return replacement[:1].upper() + replacement[1:]
        return replacement

    return _TTS_WORD_RE.sub(_sub, text)


def _safe_dict(value: Any) -> dict[str, Any]:
    """Return value if it's a dict, else an empty dict (defensive parsing)."""
    return value if isinstance(value, dict) else {}


# Suggestion buttons appended to play-/control-success responses on
# screened surfaces (mobile Alice, station-max, navigator, smart-screen).
# Lets the user tap a follow-up without saying the activation phrase
# again. `hide=False` keeps them on screen until tapped or the next
# response replaces them. Voice-only surfaces (Mini, Pro, dumb speakers)
# don't render buttons — so we omit the field entirely on those.
_PLAYBACK_SUGGESTION_BUTTONS: list[dict[str, Any]] = [
    {"title": "Следующая", "hide": False},
    {"title": "Пауза", "hide": False},
    {"title": "Громче", "hide": False},
    {"title": "Тише", "hide": False},
]


def _has_screen(meta: Any) -> bool:
    """
    Return True if the calling surface has a display.

    Yandex sets ``meta.interfaces.screen = {}`` (empty dict, present-as-key)
    on devices that can render visual elements: mobile Alice, station-max,
    station-2, navigator, smart-screen, tv-app. Audio-only surfaces
    (station-mini, station-pro, dumb speakers) omit the key entirely.
    Used to gate ``buttons`` / ``card`` emission so we don't ship UI
    bits to surfaces that ignore them.
    """
    if not isinstance(meta, dict):
        return False
    interfaces = meta.get("interfaces")
    if not isinstance(interfaces, dict):
        return False
    return "screen" in interfaces


def _without_pending(state: dict[str, Any]) -> dict[str, Any]:
    """
    Return a copy of `state` with disambiguation/elicitation keys removed.

    Strips `pending_command`, `awaiting_query`, and `awaiting_player_id`.
    Used after the disambiguation / slot-elicit flow successfully
    completes so the next turn doesn't accidentally re-enter the saved
    branch.
    """
    transient = {"pending_command", "awaiting_query", "awaiting_player_id"}
    return {k: v for k, v in state.items() if k not in transient}


# Ordinal voice-disambiguation patterns. The user picks a candidate by
# position ("первая", "выбираю первую", "номер три"). Used when a
# screenless audio device makes button-tap impossible.
#
# Two pattern families (all matched via ``re.search`` — leading filler
# words like "ну", "хочу", "выбираю", "давай" don't kill the match):
#
# 1. Russian ordinal stems (``перв\w*`` etc.) — case-insensitive
#    word-prefix match. Catches every morphological form ("первая",
#    "первый", "первое", "первую", "первой", "первом", …) without
#    enumerating each.
# 2. Cardinal numbers and digits — anchored ``^…$`` so only a
#    bare-number utterance counts ("один", "1", "номер один").
#    "У меня один вариант" must NOT silently pick the first.
_ORDINAL_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\bперв\w*\b", re.IGNORECASE), 0),
    (re.compile(r"\bвтор\w*\b", re.IGNORECASE), 1),
    (re.compile(r"\bтреть\w*\b", re.IGNORECASE), 2),
    (re.compile(r"\bчетв[её]рт\w*\b", re.IGNORECASE), 3),
    (re.compile(r"\bпят\w*\b", re.IGNORECASE), 4),
    # Cardinals — whole-utterance only.
    (re.compile(r"^(?:номер\s+)?(?:один|1)$", re.IGNORECASE), 0),
    (re.compile(r"^(?:номер\s+)?(?:два|2)$", re.IGNORECASE), 1),
    (re.compile(r"^(?:номер\s+)?(?:три|3)$", re.IGNORECASE), 2),
    (re.compile(r"^(?:номер\s+)?(?:четыре|4)$", re.IGNORECASE), 3),
    (re.compile(r"^(?:номер\s+)?(?:пять|5)$", re.IGNORECASE), 4),
)


def _parse_ordinal_choice(text: str) -> int | None:
    """
    Parse 'первая' / 'выбираю первую' / 'номер три' / '2' as 0-based index.

    Returns the index, or None if no ordinal/cardinal pattern matched.
    Tolerates leading filler words ("ну", "хочу", "выбираю", "давай")
    since users often pad voice replies on smart speakers.
    """
    if not text:
        return None
    cleaned = text.strip().lower()
    if not cleaned:
        return None
    for pattern, index in _ORDINAL_PATTERNS:
        if pattern.search(cleaned):
            return index
    return None


# Russian ordinal labels used in the disambiguation prompt.
_ORDINAL_LABELS: tuple[str, ...] = (
    "первая",
    "вторая",
    "третья",
    "четвёртая",
    "пятая",
)


# In-process state cache (TTL + LRU). Third-tier fallback when Yandex
# doesn't echo `state.session` / `state.application` back on the next
# turn — a quirk reproduced from the Yandex Station dev-console
# emulator, where the request body for every turn after the first
# arrived without any `state.*` field at all. Without this cache the
# disambiguation flow would loop indefinitely on those surfaces.
# The cache is keyed by the most stable identifier from the request
# envelope (preference: `session.user.user_id` →
# `session.application.application_id` → `session.session_id`).
_STATE_CACHE_TTL_SEC = 300  # 5 min — Alice session inactivity timeout
_STATE_CACHE_MAX = 200


class DialogsWebhookHandler:
    """Handles incoming voice-command webhook calls from a Yandex Dialogs skill."""

    def __init__(
        self,
        mass: MusicAssistant,
        *,
        skill_id: str,
        webhook_secret: str,
        exposed_player_ids: set[str] | None = None,
        voice_continuation: bool = False,
        logger: logging.Logger | None = None,
        manifest_provider: SkillManifestProvider | None = None,
    ) -> None:
        """
        Initialize the handler.

        :param mass: MusicAssistant instance.
        :param skill_id: Configured ``CONF_DIALOG_SKILL_ID``; payloads
            with a different ``session.skill_id`` are rejected.
        :param webhook_secret: Random secret embedded in the webhook URL.
        :param exposed_player_ids: Optional restriction set; only these
            players are addressable by voice (passed to the player resolver).
        :param voice_continuation: When True, play- and control-success
            responses keep the conversation open (``end_session=False``)
            so the user can issue follow-ups without re-saying the
            activation phrase. Default False preserves today's voice-UX.
            Stop / pause-with-no-resume utterances still close the session
            via the existing control path.
        :param logger: Optional logger override.
        """
        self._mass = mass
        self._skill_id = skill_id
        self._webhook_secret = webhook_secret
        self._exposed_player_ids = exposed_player_ids
        self._voice_continuation = voice_continuation
        self._logger = logger or _LOGGER
        self._unregister_callbacks: list[Callable[[], None]] = []
        self._manifest_provider = manifest_provider or SkillManifestProvider(mass)
        # In-process state cache; see _STATE_CACHE_TTL_SEC / _MAX.
        self._state_cache: OrderedDict[str, tuple[dict[str, Any], float]] = OrderedDict()
        # Diagnostics counters surfaced via ``get_diagnostics()`` on the
        # plugin instance — used by the Advanced section of the config form
        # to show "N webhooks today, last X seconds ago".
        self._webhook_call_count: int = 0
        self._authenticated_call_count: int = 0
        self._last_webhook_ts: float | None = None

    @property
    def webhook_call_count(self) -> int:
        """Total Yandex webhook invocations this process has handled."""
        return self._webhook_call_count

    @property
    def authenticated_call_count(self) -> int:
        """
        Webhook calls that passed the skill_id + secret gate.

        Counts every authenticated request — including slot-elicitation,
        disambiguation, and info-only intents — not just calls that
        resolved into a player action. Use this as a "we are receiving
        traffic from Yandex" health signal, not as a "voice commands
        actually did something" metric.
        """
        return self._authenticated_call_count

    @property
    def last_webhook_ts(self) -> float | None:
        """Wall-clock timestamp of the most recent webhook (or None)."""
        return self._last_webhook_ts

    def register_routes(self) -> None:
        """Register the webhook route on mass.webserver."""
        path = f"{DIALOG_WEBHOOK_BASE_PATH}/{self._webhook_secret}"
        redacted = f"{DIALOG_WEBHOOK_BASE_PATH}/...{self._webhook_secret[-4:]}"
        try:
            unregister = self._mass.webserver.register_dynamic_route(
                path, self._handle_webhook, "POST"
            )
        except RuntimeError:
            self._logger.exception("Failed to register Dialogs webhook route %s", redacted)
            raise
        self._unregister_callbacks.append(unregister)
        self._logger.info("Dialogs webhook registered at %s", redacted)

    def unregister_routes(self) -> None:
        """Unregister the webhook route."""
        for cb in self._unregister_callbacks:
            try:
                cb()
            except Exception:
                self._logger.debug("Error unregistering dialog route", exc_info=True)
        self._unregister_callbacks.clear()

    def replace_manifest_provider(self, manifest_provider: SkillManifestProvider) -> None:
        """Activate a newly loaded manifest snapshot after a config action."""
        self._manifest_provider = manifest_provider

    def _cache_key(self, session: dict[str, Any]) -> str | None:
        """
        Pick the most stable identifier for the in-process state cache.

        Preference order: ``session.user.user_id`` (per Yandex account,
        most specific) → ``session.application.application_id`` (per
        device) → ``session.session_id`` (per conversation). Returns
        ``None`` if none are available — caller skips caching.
        """
        user = session.get("user")
        if isinstance(user, dict):
            uid = user.get("user_id")
            if isinstance(uid, str) and uid:
                return f"user:{uid}"
        app = session.get("application")
        if isinstance(app, dict):
            aid = app.get("application_id")
            if isinstance(aid, str) and aid:
                return f"app:{aid}"
        sid = session.get("session_id")
        if isinstance(sid, str) and sid:
            return f"session:{sid}"
        return None

    def _cache_get(self, session: dict[str, Any]) -> dict[str, Any]:
        """Return the cached state for this caller, or {} if absent / expired."""
        key = self._cache_key(session)
        if key is None:
            return {}
        entry = self._state_cache.get(key)
        if entry is None:
            return {}
        state, ts = entry
        if time.monotonic() - ts > _STATE_CACHE_TTL_SEC:
            self._state_cache.pop(key, None)
            return {}
        # LRU touch.
        self._state_cache.move_to_end(key)
        return state

    def _cache_put(self, session: dict[str, Any], state: dict[str, Any]) -> None:
        """
        Save state for this caller (LRU + TTL eviction).

        Pass an empty / cleared state dict (rather than skipping the
        call) when the action explicitly drops pending/awaiting — this
        ensures the cache reflects the post-action state and a stale
        pending_command doesn't resurface on the next turn.
        """
        key = self._cache_key(session)
        if key is None:
            return
        self._state_cache[key] = (dict(state), time.monotonic())
        self._state_cache.move_to_end(key)
        while len(self._state_cache) > _STATE_CACHE_MAX:
            self._state_cache.popitem(last=False)

    # -------------------------------------------------------------------
    # Webhook entry point
    # -------------------------------------------------------------------

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        # Path secret already enforced by the route URL — getting here means
        # the secret matches. Still constant-time-compare it via the captured
        # path arg in case aiohttp routing ever changes.
        url_secret = request.match_info.get("secret") or request.path.rsplit("/", 1)[-1]
        if not secrets.compare_digest(url_secret, self._webhook_secret):
            return web.Response(status=404)

        # Counters: tick on every reachable POST so the Advanced LABEL can
        # show the user when Yandex last hit us. Intent counter ticks only
        # when we successfully dispatched a player action — see _dispatch_*.
        self._webhook_call_count += 1
        self._last_webhook_ts = time.time()

        try:
            body = await request.json()
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._yandex_response(
                incoming_session={},
                text="Что-то пошло не так с запросом.",
            )
        if not isinstance(body, dict):
            return self._yandex_response(
                incoming_session={},
                text="Что-то пошло не так с запросом.",
            )

        session = body.get("session") or {}
        if not isinstance(session, dict):
            session = {}
        req = body.get("request") or {}
        if not isinstance(req, dict):
            req = {}
        meta = body.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        has_screen = _has_screen(meta)

        # skill_id sanity check — reject if absent or mismatched.
        incoming_skill_id = str(session.get("skill_id") or "")
        if not incoming_skill_id or not secrets.compare_digest(incoming_skill_id, self._skill_id):
            self._logger.warning(
                "Rejecting dialog payload: skill_id %r != configured %r",
                incoming_skill_id or "<missing>",
                self._skill_id,
            )
            return web.Response(status=401)

        # Past the skill_id gate the request is genuine traffic from our
        # registered Yandex skill. Counts ALL authenticated requests
        # (slot elicitation, disambiguation, info-only) — NOT just calls
        # that resolve into a player action. Health signal only.
        self._authenticated_call_count += 1

        # Wrap post-auth dispatch so any unexpected exception (parser,
        # search, MA dispatch, response builder) surfaces as a graceful
        # Russian fallback instead of an aiohttp HTTP 500 → Alice silence.
        # Logs the original exception for the operator to debug from
        # `$HOME/.musicassistant/musicassistant.log`. Flagged in the
        # upstream PR review (#3843, @chrisuthe) as a regression risk.
        try:
            return await self._handle_authenticated_request(
                body=body, session=session, req=req, has_screen=has_screen
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception(
                "Unhandled error in dialog webhook handler — "
                "responding with generic fallback (session_id=%s)",
                session.get("session_id", ""),
            )
            text = "Что-то пошло не так. Попробуй ещё раз."
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                end_session=False,
            )

    async def _handle_authenticated_request(  # noqa: PLR0915
        self,
        *,
        body: dict[str, Any],
        session: dict[str, Any],
        req: dict[str, Any],
        has_screen: bool,
    ) -> web.Response:
        """
        Dispatch the request body once authentication has cleared.

        Wrapped in ``try / except`` by the caller so any unexpected raise
        from a parser, the resolver, or MA dispatch lands as a graceful
        fallback response rather than HTTP 500. Returns ``web.Response``.

        :param body: Parsed JSON envelope.
        :param session: ``body["session"]`` already coerced to dict.
        :param req: ``body["request"]`` already coerced to dict.
        :param has_screen: Result of :func:`_has_screen` on the request.
        """
        # State buckets. Three-tier read priority:
        #   1. ``state.session``  — per-conversation, set by us last turn.
        #   2. ``state.application`` — per-device, mirrored fallback.
        #   3. In-process cache — server-side LRU keyed by user_id /
        #      application_id, last-resort for surfaces (notably the
        #      Yandex Station dev console emulator) that don't echo
        #      `state.*` back at all.
        state = body.get("state") or {}
        if not isinstance(state, dict):
            state = {}
        session_state_in = _safe_dict(state.get("session"))
        app_state_in = _safe_dict(state.get("application"))
        user_state_in = _safe_dict(state.get("user"))
        cached_state = self._cache_get(session)

        default_id_raw = (
            session_state_in.get("last_player_id")
            or app_state_in.get("last_player_id")
            or user_state_in.get("preferred_player_id")
            or cached_state.get("last_player_id")
        )
        default_id = str(default_id_raw) if default_id_raw else None

        is_new = bool(session.get("new"))
        command = str(req.get("command") or "").strip()
        original_utterance = str(req.get("original_utterance") or "").strip()

        # request.markup.dangerous_context — Yandex flags suicide/violence/
        # hate content before passing the phrase through. If raised, refuse
        # gracefully without engaging music search; passing flagged content
        # to mass.music.search is bad PR (and may surface a result keyed off
        # the flagged words).
        markup = req.get("markup") or {}
        if not isinstance(markup, dict):
            markup = {}
        dangerous_context = bool(markup.get("dangerous_context"))

        # Pending-command / awaiting-query lookups follow the same
        # three-tier order as default_id: session → application →
        # in-process cache. Yandex Station devices in particular
        # sometimes drop both `state.session` AND `state.application`
        # between SimpleUtterance turns — the cache is what makes
        # disambiguation actually work on those surfaces.
        pending_in = session_state_in.get("pending_command")
        if not isinstance(pending_in, dict):
            pending_in = app_state_in.get("pending_command")
        if not isinstance(pending_in, dict):
            pending_in = cached_state.get("pending_command")
        awaiting_in = (
            bool(session_state_in.get("awaiting_query"))
            or bool(app_state_in.get("awaiting_query"))
            or bool(cached_state.get("awaiting_query"))
        )

        # Single summary log per incoming request — surfaces the wire-shape
        # bits we route on. Sensitive fields (skill_id, webhook_secret,
        # raw payload IDs) are excluded; user/session IDs are opaque
        # tokens and DEBUG is opt-in, so they're included as-is.
        # `original_utterance` is logged when it differs from the
        # normalised `command` (Yandex strips punctuation and converts
        # spelled-out numbers; the raw form helps misclassification
        # post-mortems).
        # Flagged content (`dangerous_context=true`) is redacted from
        # both `cmd` and the raw suffix — Yandex flags suicide / hate /
        # violence phrasings and we don't want to persist any of that
        # in DEBUG logs even at the operator's request.
        if dangerous_context:
            cmd_for_log: str | None = "<redacted: dangerous_context>"
            raw_suffix = ""
        else:
            cmd_for_log = command
            raw_suffix = (
                f" raw={original_utterance!r}"
                if original_utterance and original_utterance != command
                else ""
            )
        self._logger.debug(
            "Webhook recv: cmd=%r%s req_type=%s is_new=%s pending=%s "
            "(session=%s app=%s cache=%s) awaiting=%s default_player=%s "
            "dangerous=%s session_id=%s",
            cmd_for_log,
            raw_suffix,
            req.get("type", "SimpleUtterance"),
            is_new,
            bool(pending_in),
            bool(session_state_in.get("pending_command")),
            bool(app_state_in.get("pending_command")),
            bool(cached_state.get("pending_command")),
            awaiting_in,
            default_id,
            dangerous_context,
            session.get("session_id", ""),
        )

        if is_new and not command:
            text = "Привет! Скажи, что включить и на какой колонке."
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                end_session=False,
                session_state=session_state_in,
            )

        if not command:
            text = "Не понял команду. Скажи, например: включи рок на кухне."
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                end_session=False,
                session_state=session_state_in,
            )

        if dangerous_context:
            # Refuse gracefully and end session. Don't engage NLU or search
            # so a flagged phrase never lands in mass.music.search results
            # or in our logs as an "intent". Drop pending/awaiting state
            # so the next conversation starts clean.
            self._logger.info(
                "Dropping flagged-content request (dangerous_context=true); session_id=%s",
                session.get("session_id", ""),
            )
            text = "Не понял команду."
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                end_session=True,
                session_state=_without_pending(session_state_in),
            )

        # P0.6 — try control commands (pause/next/volume/...) FIRST, on
        # the raw command. Doing this before the awaiting-query synthesis
        # lets the user pivot from a slot-elicit prompt straight into a
        # control intent ("Включи." → "Что включить?" → "пауза на кухне")
        # without the prefix-prepend turning it into "включи пауза…".
        # If control matches, drop any pending/awaiting state — the user
        # is no longer in either of those flows.
        # `entities` (request.nlu.entities) feeds parse_control's
        # YANDEX.NUMBER fallback for relative-volume phrasings where
        # the regex didn't anchor on a digit.
        nlu = req.get("nlu") or {}
        if not isinstance(nlu, dict):
            nlu = {}
        nlu_entities = nlu.get("entities") if isinstance(nlu.get("entities"), list) else None
        nlu_intents = nlu.get("intents") if isinstance(nlu.get("intents"), dict) else None

        # Built-in YANDEX.* intents — emitted automatically by Yandex once
        # any custom grammar is declared. Two we care about today:
        #
        # * YANDEX.REJECT ("отмена / нет / неважно / отстань") — back out
        #   of any in-flight prompt. Clears pending_command / awaiting_query
        #   and ends the session so the user can speak again from scratch.
        # * YANDEX.HELP ("помоги / что я могу / помощь") — surface a
        #   contextual hint depending on the current prompt; keeps state
        #   so the user can answer the original question afterwards.
        #
        # YANDEX.CONFIRM and YANDEX.REPEAT aren't wired today: confirm is
        # ambiguous in our flows (which player are you confirming?) and
        # repeat would require caching the last response on session_state
        # — both deferred to a later session.
        if isinstance(nlu_intents, dict):
            if "YANDEX.REJECT" in nlu_intents and (pending_in or awaiting_in):
                self._logger.debug("YANDEX.REJECT in pending/awaiting state → cancel")
                text = "Хорошо, отменил."
                return self._yandex_response(
                    incoming_session=session,
                    text=text,
                    tts=_tts_for(text),
                    end_session=True,
                    session_state=_without_pending(session_state_in),
                    application_state=_without_pending(app_state_in),
                )
            if "YANDEX.HELP" in nlu_intents:
                if pending_in:
                    text = "Скажи имя колонки или её номер из списка."
                elif awaiting_in:
                    text = "Скажи имя артиста, песни, альбома или плейлиста."
                else:
                    text = "Скажи, например: включи рок на кухне."
                self._logger.debug("YANDEX.HELP → contextual hint")
                return self._yandex_response(
                    incoming_session=session,
                    text=text,
                    tts=_tts_for(text),
                    end_session=False,
                    # Preserve pending/awaiting state so the user can answer the
                    # original prompt right after this hint.
                    session_state=session_state_in,
                )

        # Phase 2 — platform-pre-classified intents take precedence over
        # the regex parsers. When grammar matched the phrase upstream,
        # the result lands in `request.nlu.intents.<form_name>`; map it
        # back to our existing ParsedControl / ParsedCommand and skip the
        # regex pass. Falls through to the regex parsers when the block
        # is empty (no grammar declared or no match).
        platform = self._manifest_provider.parse_intent(nlu_intents)
        if isinstance(platform, ParsedControl):
            # Yandex's static intent grammar can't enumerate the user's
            # per-skill list of player names, so the "на <player>" suffix
            # ("пауза на кухне") doesn't make it into the slots. Recover
            # the hint from the raw command text and attach it here.
            if platform.player_hint is None:
                hint = extract_trailing_player_hint(command)
                if hint:
                    platform = dataclasses.replace(platform, player_hint=hint)
            self._logger.debug("Platform intent → control %r (skipping regex parser)", platform)
            return self._handle_control(
                session=session,
                control=platform,
                default_id=default_id,
                session_state_in=_without_pending(session_state_in),
                app_state_in=app_state_in,
                has_screen=has_screen,
            )
        if isinstance(platform, ParsedCommand):
            self._logger.debug("Platform intent → play %r (skipping regex parser)", platform)
            return await self._dispatch_play(
                session=session,
                parsed=platform,
                default_id=default_id,
                session_state_in=session_state_in,
                app_state_in=app_state_in,
                has_screen=has_screen,
            )

        if control := parse_control(command, entities=nlu_entities):
            self._logger.debug("Parsed dialog control %r → %r", command, control)
            return self._handle_control(
                session=session,
                control=control,
                default_id=default_id,
                session_state_in=_without_pending(session_state_in),
                app_state_in=app_state_in,
                has_screen=has_screen,
            )

        # P0.4 — awaiting-query re-entry. If the previous turn asked "Что
        # включить?" and the new utterance isn't a control phrase, treat
        # it as the missing query slot. Prepend a synthetic "включи " so
        # the existing kind classifier runs ("песню X", "альбом Y",
        # "мою волну", etc.). Skip the synthetic prefix if the user
        # already said one of the verbs.
        if awaiting_in and not _VERB_RE.match(command):
            command = f"включи {command}"
            self._logger.debug("Awaiting-query branch: synthesised cmd=%r", command)
        # If slot-elicit was triggered with a player hint that resolved
        # to a single exposed player, the follow-up turn should play on
        # that player. Surface it as `default_id` so the resolver picks
        # it without the user re-stating "на кухне".
        if awaiting_in and not default_id:
            saved_pid = (
                session_state_in.get("awaiting_player_id")
                or app_state_in.get("awaiting_player_id")
                or cached_state.get("awaiting_player_id")
            )
            if saved_pid:
                default_id = str(saved_pid)
                self._logger.debug(
                    "Awaiting-query branch: restored hinted player as default_id=%s",
                    default_id,
                )

        # P0.3 — pending-command re-entry. If a previous turn asked the
        # user to disambiguate which player to use, the new utterance (or
        # button press) carries the answer; replay the saved play intent.
        # `pending_in` was merged from `state.session` and `state.application`
        # earlier so this works even on devices that don't preserve
        # session-state between SimpleUtterance turns.
        if isinstance(pending_in, dict):
            pending: dict[str, Any] = pending_in
            self._logger.debug(
                "Pending-command branch: kind=%s query=%r radio=%s; cmd=%r payload=%s",
                pending.get("kind"),
                pending.get("query"),
                pending.get("radio_mode"),
                command,
                bool(_safe_dict(req.get("payload")).get("player_id")),
            )
            replay_response = await self._try_resume_pending(
                session=session,
                req=req,
                command=command,
                pending=pending,
                session_state_in=session_state_in,
                app_state_in=app_state_in,
                has_screen=has_screen,
            )
            if replay_response is not None:
                return replay_response
            self._logger.debug(
                "Pending-command branch: could not resume — falling through to parse_command"
            )

        parsed = parse_command(command)
        self._logger.debug("Parsed dialog command %r → %r", command, parsed)
        return await self._dispatch_play(
            session=session,
            parsed=parsed,
            default_id=default_id,
            session_state_in=session_state_in,
            app_state_in=app_state_in,
            has_screen=has_screen,
        )

    # -------------------------------------------------------------------
    # Play dispatch (slot-elicit + resolve + disambiguate + play)
    # -------------------------------------------------------------------

    async def _dispatch_play(
        self,
        *,
        session: dict[str, Any],
        parsed: ParsedCommand,
        default_id: str | None,
        session_state_in: dict[str, Any],
        app_state_in: dict[str, Any],
        has_screen: bool = True,
    ) -> web.Response:
        """Slot-elicit / resolve player / disambiguate / play (or fail)."""
        # P0.4 — slot elicitation: bare verb with no actionable content.
        # Triggers whenever the query slot is empty, even if the user
        # specified a player hint ("включи на кухне"). Falling through
        # would respond "Не нашёл такую музыку: ." which is confusing —
        # the user clearly *wants* something, just didn't name it yet.
        # If a hint resolves to a single exposed player, save its id as
        # `awaiting_player_id` so the follow-up turn plays on it.
        if parsed.kind == "search" and not parsed.query:
            self._logger.debug(
                "Slot-elicit branch: empty query (hint=%r), asking 'Что включить?'",
                parsed.player_hint,
            )
            awaiting_player_id: str | None = None
            if parsed.player_hint:
                hinted_candidates = resolve_player_candidates(
                    self._mass,
                    parsed.player_hint,
                    default_id=default_id,
                    exposed_ids=self._exposed_player_ids,
                )
                if len(hinted_candidates) == 1:
                    awaiting_player_id = hinted_candidates[0].player_id
            text = "Что включить? Можно сказать имя артиста, песни или плейлиста."
            elicit_state: dict[str, Any] = {"awaiting_query": True}
            if awaiting_player_id:
                elicit_state["awaiting_player_id"] = awaiting_player_id
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                end_session=False,
                session_state={**_without_pending(session_state_in), **elicit_state},
                # Mirror to application_state so the next turn can find
                # the flag even if Yandex didn't echo `state.session`.
                application_state={**_without_pending(app_state_in), **elicit_state},
            )

        candidates = resolve_player_candidates(
            self._mass,
            parsed.player_hint,
            default_id=default_id,
            exposed_ids=self._exposed_player_ids,
        )
        if not candidates:
            # Special case: no hint, no default, multiple exposed players.
            # `resolve_player_candidates` returns [] with no hint when it
            # can't pick deterministically — for the user that's ambiguity,
            # not "not found". Surface all exposed players for
            # disambiguation instead of the misleading "не нашёл колонку
            # «(не указано)»".
            if parsed.player_hint is None and default_id is None:
                all_exposed = list_exposed_players(self._mass, exposed_ids=self._exposed_player_ids)
                if len(all_exposed) >= 2:
                    self._logger.debug(
                        "Play branch: no hint + no default + %d exposed → "
                        "disambiguation across all exposed players",
                        len(all_exposed),
                    )
                    return self._build_disambiguation_response(
                        session=session,
                        parsed=parsed,
                        candidates=all_exposed,
                        session_state_in=session_state_in,
                        app_state_in=app_state_in,
                        has_screen=has_screen,
                    )
            hint = parsed.player_hint or "(не указано)"
            self._logger.info(
                "Play branch: no player resolved for hint=%r (default_id=%s); "
                "responding 'не нашёл колонку'",
                parsed.player_hint,
                default_id,
            )
            text = f"Не нашёл колонку «{hint}». Скажи, например: на кухне."
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                end_session=False,
                session_state=session_state_in,
            )
        if len(candidates) > 1:
            self._logger.debug(
                "Play branch: ambiguous, %d candidates → disambiguation prompt",
                len(candidates),
            )
            return self._build_disambiguation_response(
                session=session,
                parsed=parsed,
                candidates=candidates,
                session_state_in=session_state_in,
                app_state_in=app_state_in,
                has_screen=has_screen,
            )

        self._logger.debug(
            "Play branch: resolved → player %s (%s)",
            candidates[0].name or candidates[0].player_id,
            candidates[0].player_id,
        )
        return await self._play_with_player(
            session=session,
            parsed=parsed,
            player=candidates[0],
            base_session_state=session_state_in,
            base_app_state=app_state_in,
            has_screen=has_screen,
        )

    # -------------------------------------------------------------------
    # Control execution helper (P0.6)
    # -------------------------------------------------------------------

    def _handle_control(  # noqa: PLR0915
        self,
        *,
        session: dict[str, Any],
        control: Any,
        default_id: str | None,
        session_state_in: dict[str, Any],
        app_state_in: dict[str, Any],
        has_screen: bool = True,
    ) -> web.Response:
        """Resolve player + dispatch a control action; build response."""
        # A control intent abandons any outstanding play disambiguation / query
        # prompt.  Normalise both Yandex state tiers here so every early return
        # below (including informational and validation responses) publishes the
        # same clean state.  Some screenless surfaces do not echo session_state,
        # so clearing only that bucket lets stale application_state resurface.
        session_state_in = _without_pending(session_state_in)
        app_state_in = _without_pending(app_state_in)

        # list_players is informational — no player resolution / dispatch.
        if control.action == "list_players":
            players = list_exposed_players(self._mass, exposed_ids=self._exposed_player_ids)
            text = format_list_players(players)
            self._logger.debug(
                "Control list_players → %d player(s): %s",
                len(players),
                [getattr(p, "name", None) or p.player_id for p in players],
            )
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                end_session=False,
                session_state=session_state_in,
                application_state=app_state_in,
            )

        # now_playing — info query about the current track. Reads
        # `mass.player_queues.get(pid).current_item.name` (already
        # pre-formatted as "Artist - Title" or stream title for radio).
        if control.action == "now_playing":
            target_player = resolve_player(
                self._mass,
                control.player_hint,
                default_id=default_id,
                exposed_ids=self._exposed_player_ids,
            )
            if target_player is None:
                if control.player_hint:
                    text = f"Не нашёл колонку «{control.player_hint}». Скажи, например: на кухне."
                else:
                    text = "Скажи, на какой колонке. Например: что играет на кухне."
                return self._yandex_response(
                    incoming_session=session,
                    text=text,
                    tts=_tts_for(text),
                    end_session=False,
                    session_state=session_state_in,
                    application_state=app_state_in,
                )
            queue = None
            try:
                queue = self._mass.player_queues.get(target_player.player_id)
            except Exception:
                self._logger.debug("player_queues.get failed", exc_info=True)
            current = getattr(queue, "current_item", None) if queue is not None else None
            current_name = getattr(current, "name", None) if current is not None else None
            display_name = target_player.name or target_player.player_id
            if current_name:
                text = f"Сейчас играет: {current_name}."
            else:
                text = f"На {display_name} сейчас ничего не играет."
            self._logger.debug(
                "Control now_playing on %s → %r",
                target_player.player_id,
                current_name,
            )
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                end_session=False,
                session_state=session_state_in,
                application_state=app_state_in,
            )

        # transfer — moves the queue from the saved default player to
        # the named target player. SOURCE = `default_id` (last-used);
        # TARGET = `control.player_hint` (parsed from "переведи на X").
        if control.action == "transfer":
            if not default_id:
                text = "Не понял, откуда переводить. Сначала включи музыку на колонке."
                return self._yandex_response(
                    incoming_session=session,
                    text=text,
                    tts=_tts_for(text),
                    end_session=False,
                    session_state=session_state_in,
                    application_state=app_state_in,
                )
            target_candidates = resolve_player_candidates(
                self._mass,
                control.player_hint,
                default_id=None,
                exposed_ids=self._exposed_player_ids,
            )
            if not target_candidates:
                hint = control.player_hint or "(не указано)"
                text = f"Не нашёл колонку «{hint}». Скажи, например: на кухне."
                return self._yandex_response(
                    incoming_session=session,
                    text=text,
                    tts=_tts_for(text),
                    end_session=False,
                    session_state=session_state_in,
                    application_state=app_state_in,
                )
            if len(target_candidates) > 1:
                # Multi-match: reuse the disambiguation flow, but the
                # pending intent for replay is "transfer this queue".
                # We don't currently support resuming a transfer through
                # `_try_resume_pending` (it's coupled to play intent),
                # so for now just respond with a clarification.
                names = ", ".join(p.name or p.player_id for p in target_candidates[:5])
                text = f"Не понял на какую колонку. Уточни: {names}?"
                return self._yandex_response(
                    incoming_session=session,
                    text=text,
                    tts=_tts_for(text),
                    end_session=False,
                    session_state=session_state_in,
                    application_state=app_state_in,
                )
            target = target_candidates[0]
            target_name = target.name or target.player_id
            if target.player_id == default_id:
                text = f"Уже играет на {target_name}."
                return self._yandex_response(
                    incoming_session=session,
                    text=text,
                    tts=_tts_for(text),
                    end_session=False,
                    session_state=session_state_in,
                    application_state=app_state_in,
                )
            self._logger.info(
                "Control transfer: %s → %s",
                default_id,
                target.player_id,
            )
            self._mass.create_task(
                self._transfer_queue_logged(
                    source_queue_id=default_id,
                    target_queue_id=target.player_id,
                )
            )
            new_session_state = {**session_state_in, "last_player_id": target.player_id}
            new_app_state = {**app_state_in, "last_player_id": target.player_id}
            user_obj_t = session.get("user") or {}
            user_state_update_t: dict[str, Any] | None = None
            if isinstance(user_obj_t, dict) and user_obj_t.get("user_id"):
                user_state_update_t = {"preferred_player_id": target.player_id}
            text = f"Перевожу на {target_name}."
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                session_state=new_session_state,
                application_state=new_app_state,
                user_state_update=user_state_update_t,
            )

        # forget_player clears the saved default-player from all three
        # state tiers (session / application / cache) AND emits a
        # `user_state_update` with `preferred_player_id: None` so the
        # next play command without an explicit hint asks the user to
        # pick again. Doesn't need a target — purely state management.
        if control.action == "forget_player":
            self._logger.info("Control forget_player → clearing last_player_id from all tiers")
            new_session_state = {k: v for k, v in session_state_in.items() if k != "last_player_id"}
            new_app_state = {k: v for k, v in app_state_in.items() if k != "last_player_id"}
            user_obj_forget = session.get("user") or {}
            user_state_update_forget: dict[str, Any] | None = None
            if isinstance(user_obj_forget, dict) and user_obj_forget.get("user_id"):
                # Yandex spec: a key set to None in `user_state_update`
                # tells the platform to delete it from the merged
                # user-scoped state.
                user_state_update_forget = {"preferred_player_id": None}
            text = control_confirmation(control)
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                end_session=False,
                session_state=new_session_state,
                application_state=new_app_state,
                user_state_update=user_state_update_forget,
            )

        player = resolve_player(
            self._mass,
            control.player_hint,
            default_id=default_id,
            exposed_ids=self._exposed_player_ids,
        )
        if player is None:
            self._logger.info(
                "Control %s: no player resolved (hint=%r, default_id=%s)",
                control.action,
                control.player_hint,
                default_id,
            )
            # Distinguish "no hint + ambiguous" from "hint given but unknown"
            # so the message matches the actual cause.
            if control.player_hint:
                text = f"Не нашёл колонку «{control.player_hint}». Скажи, например: на кухне."
            else:
                text = "Скажи, на какой колонке. Например: пауза на кухне."
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                end_session=False,
                session_state=session_state_in,
                application_state=app_state_in,
            )
        self._logger.debug(
            "Control %s → player %s (%s) value=%s",
            control.action,
            player.name or player.player_id,
            player.player_id,
            control.value,
        )
        self._mass.create_task(execute_control(self._mass, control, player))
        # Clear any pending disambiguation / awaiting-query state from
        # both tiers — the user took a different path. (`session_state_in`
        # was already cleaned by the caller with `_without_pending`; do
        # the same defensively here for application_state.)
        new_session_state = {**session_state_in, "last_player_id": player.player_id}
        new_app_state = {
            **_without_pending(app_state_in),
            "last_player_id": player.player_id,
        }
        user_obj = session.get("user") or {}
        user_state_update: dict[str, Any] | None = None
        if isinstance(user_obj, dict) and user_obj.get("user_id"):
            user_state_update = {"preferred_player_id": player.player_id}
        text = control_confirmation(control)
        # Stop is the natural session-end signal — even with voice
        # continuation enabled, "стоп / выключи" should hand the mic
        # back to the user instead of staying in the skill listening loop.
        end_session = True if control.action == "stop" else not self._voice_continuation
        return self._yandex_response(
            incoming_session=session,
            text=text,
            tts=_tts_for(text),
            end_session=end_session,
            session_state=new_session_state,
            application_state=new_app_state,
            user_state_update=user_state_update,
            buttons=_PLAYBACK_SUGGESTION_BUTTONS if has_screen else None,
        )

    # -------------------------------------------------------------------
    # Play execution helper (shared by initial flow and pending replay)
    # -------------------------------------------------------------------

    async def _play_with_player(
        self,
        *,
        session: dict[str, Any],
        parsed: ParsedCommand,
        player: Any,
        base_session_state: dict[str, Any],
        base_app_state: dict[str, Any],
        has_screen: bool = True,
    ) -> web.Response:
        """Search media, fire-and-forget play, build response with persisted state."""
        try:
            media = await asyncio.wait_for(
                resolve_query(self._mass, parsed), timeout=DIALOG_RESOLVE_TIMEOUT
            )
        except TimeoutError:
            self._logger.warning(
                "Music search timed out (>%.1fs) for query %r",
                DIALOG_RESOLVE_TIMEOUT,
                parsed.query,
            )
            text = "Поиск занял слишком долго, попробуй ещё раз."
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                session_state=_without_pending(base_session_state),
                application_state=_without_pending(base_app_state),
            )

        if media is None:
            text = f"Не нашёл такую музыку: {parsed.query}."
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                session_state=_without_pending(base_session_state),
                application_state=_without_pending(base_app_state),
            )

        # Fire-and-forget — Alice has a 4.5s budget; play_media may take longer
        # to actually start streaming. mass.create_task tracks the task in the
        # MA lifecycle (cancelled on shutdown) and logs unhandled exceptions.
        self._mass.create_task(
            self._play_for_alice_logged(
                player_id=player.player_id,
                media=media,
                radio_mode=parsed.radio_mode,
                enqueue_option=parsed.enqueue_option,
            )
        )

        new_session_state = {
            **_without_pending(base_session_state),
            "last_player_id": player.player_id,
        }
        # Also clear pending/awaiting from `application_state` — it was
        # mirrored there as a fallback for devices that don't preserve
        # `state.session` between turns.
        new_app_state = {
            **_without_pending(base_app_state),
            "last_player_id": player.player_id,
        }
        user_obj = session.get("user") or {}
        user_state_update: dict[str, Any] | None = None
        if isinstance(user_obj, dict) and user_obj.get("user_id"):
            user_state_update = {"preferred_player_id": player.player_id}

        spoken_query = parsed.query or "музыку"
        player_label = player.name or player.player_id
        if parsed.enqueue_option == "add":
            text = f"Добавил {spoken_query} в очередь на {player_label}."
        elif parsed.enqueue_option == "next":
            text = f"Поставил {spoken_query} следующим на {player_label}."
        else:
            text = f"Включаю {spoken_query} на {player_label}."
        return self._yandex_response(
            incoming_session=session,
            text=text,
            tts=_tts_for(text),
            end_session=not self._voice_continuation,
            session_state=new_session_state,
            application_state=new_app_state,
            user_state_update=user_state_update,
            buttons=_PLAYBACK_SUGGESTION_BUTTONS if has_screen else None,
        )

    async def _play_for_alice_logged(
        self,
        *,
        player_id: str,
        media: Any,
        radio_mode: bool,
        enqueue_option: str | None,
    ) -> None:
        """Run fire-and-forget playback while preserving actionable error logs."""
        try:
            await play_for_alice(
                self._mass,
                player_id,
                media,
                radio_mode=radio_mode,
                enqueue_option=enqueue_option,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception("Background playback failed for player %s", player_id)

    async def _transfer_queue_logged(
        self,
        *,
        source_queue_id: str,
        target_queue_id: str,
    ) -> None:
        """Run a queue transfer in the background and log failures at ERROR."""
        try:
            await self._mass.player_queues.transfer_queue(
                source_queue_id=source_queue_id,
                target_queue_id=target_queue_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception(
                "Background queue transfer failed: %s -> %s",
                source_queue_id,
                target_queue_id,
            )

    # -------------------------------------------------------------------
    # Disambiguation (P0.3)
    # -------------------------------------------------------------------

    def _build_disambiguation_response(
        self,
        *,
        session: dict[str, Any],
        parsed: ParsedCommand,
        candidates: list[Any],
        session_state_in: dict[str, Any],
        app_state_in: dict[str, Any] | None = None,
        has_screen: bool = True,
    ) -> web.Response:
        """
        Ask the user which player to use — voice-first, with optional buttons.

        Most Yandex Stations are screenless audio devices, so the prompt
        has to make voice answer obvious. We enumerate candidates with
        Russian ordinals (`первая` / `вторая` / …) so a user can say
        either the player name (free-text fallback) or the position.
        Buttons are emitted only on screened surfaces; voice-only devices
        get the same prompt without the button payload.
        """
        # Yandex caps ItemsList at 5 anyway; cap our buttons to the same.
        capped = candidates[:5]
        names = [p.name or p.player_id for p in capped]

        # Voice prompt: ordinal-labelled list + explicit voice instruction.
        # Example for 2 candidates:
        #   "На какой колонке? Первая — Кухня большая, вторая — Кухня
        #    маленькая. Скажи название или номер."
        labelled = [f"{_ORDINAL_LABELS[i]} — {name}" for i, name in enumerate(names)]
        text = "На какой колонке? " + ", ".join(labelled) + ". Скажи название или номер."
        buttons: list[dict[str, Any]] | None = (
            [
                {
                    "title": (p.name or p.player_id)[:64],
                    "payload": {"player_id": p.player_id},
                    "hide": True,
                }
                for p in capped
            ]
            if has_screen
            else None
        )
        # Clear any prior `awaiting_query` / `pending_command` before
        # writing the new one, and include the saved `pending_command`.
        # The same pending entry is mirrored to BOTH `session_state` and
        # `application_state` because some Yandex devices (notably
        # screenless Stations) don't reliably echo `state.session` back
        # across SimpleUtterance turns. The application tier persists
        # per-device — it survives session resets and is honoured on
        # every surface we've tested. Reads in `_handle_webhook` merge
        # the two tiers (session preferred, application as fallback).
        pending_command: dict[str, Any] = {
            "kind": parsed.kind,
            "query": parsed.query[:200],
            "radio_mode": parsed.radio_mode,
            # Ordered list of player IDs we offered. Used by
            # `_try_resume_pending` to (a) resolve "первая"/"вторая"
            # to a specific player by position, (b) re-narrow free-text
            # matching to just these candidates so a short distinguisher
            # wins even if a third matching player exists outside the
            # disambiguation set.
            "candidate_ids": [p.player_id for p in capped],
        }
        # Preserve the enqueue option (add / next) across the
        # disambiguation re-entry — without this an ambiguous
        # "добавь Iron Maiden" would resume as REPLACE after the
        # user picks the player, defeating the add-to-queue intent.
        if parsed.enqueue_option is not None:
            pending_command["enqueue_option"] = parsed.enqueue_option
        new_session_state = {
            **_without_pending(session_state_in),
            "pending_command": pending_command,
        }
        new_app_state = {
            **_without_pending(app_state_in or {}),
            "pending_command": pending_command,
        }
        return self._yandex_response(
            incoming_session=session,
            text=text,
            tts=_tts_for(text),
            end_session=False,
            session_state=new_session_state,
            application_state=new_app_state,
            buttons=buttons,
        )

    async def _try_resume_pending(
        self,
        *,
        session: dict[str, Any],
        req: dict[str, Any],
        command: str,
        pending: dict[str, Any],
        session_state_in: dict[str, Any],
        app_state_in: dict[str, Any],
        has_screen: bool = True,
    ) -> web.Response | None:
        """
        Attempt to resume a saved pending_command using button payload or text.

        Returns a response if the pending command was resumed (success or
        decided failure). Returns None when the new utterance doesn't
        resolve to a player at all — caller falls through to normal
        parse_command flow.
        """
        chosen_player: Any = None
        candidate_ids_raw = pending.get("candidate_ids")
        candidate_ids: list[str] = (
            [str(x) for x in candidate_ids_raw if isinstance(x, str)]
            if isinstance(candidate_ids_raw, list)
            else []
        )
        # Preserve enqueue intent (add / next) across the disambiguation
        # re-entry; otherwise an ambiguous "добавь Iron Maiden" would
        # replay as REPLACE after the user picks a player.
        enqueue_raw = pending.get("enqueue_option")
        pending_enqueue: str | None = enqueue_raw if isinstance(enqueue_raw, str) else None
        exposed = list_exposed_players(self._mass, exposed_ids=self._exposed_player_ids)
        exposed_by_id = {p.player_id: p for p in exposed}

        # Step 1 — Button press. Direct UI signal on surfaces with a
        # screen. Validate against the currently exposed set
        # (defence-in-depth: stale / crafted payloads are rejected).
        payload = req.get("payload")
        if isinstance(payload, dict):
            pid = payload.get("player_id")
            if isinstance(pid, str):
                chosen_player = exposed_by_id.get(pid)
                if chosen_player is None:
                    self._logger.warning(
                        "Pending replay: ButtonPressed payload player_id=%r "
                        "not in exposed-player set; ignoring",
                        pid,
                    )

        # Step 2 — Free-text first. Lets named answers ("Кухня большая"
        # / "большая" / "маленькую") and even hypothetical players whose
        # names contain ordinal words ("Спальня первая") win over the
        # purely-positional ordinal interpretation. Narrow the resolver
        # to the saved candidate set so a short distinguisher like
        # "большая" doesn't accidentally pick an unrelated third player
        # outside the disambiguation set.
        if chosen_player is None:
            followup = parse_command(command)
            hint = followup.player_hint or command
            narrowed_ids: set[str] | None
            if candidate_ids:
                narrowed_ids = set(candidate_ids)
                if self._exposed_player_ids is not None:
                    narrowed_ids &= self._exposed_player_ids
            else:
                narrowed_ids = self._exposed_player_ids
            candidates = resolve_player_candidates(
                self._mass,
                hint,
                default_id=None,
                exposed_ids=narrowed_ids,
            )
            if len(candidates) == 1:
                chosen_player = candidates[0]
                self._logger.debug(
                    "Pending replay: free-text → player %s",
                    chosen_player.name or chosen_player.player_id,
                )
            elif len(candidates) > 1:
                # Still ambiguous — re-ask with the saved play intent.
                return self._build_disambiguation_response(
                    session=session,
                    parsed=ParsedCommand(
                        kind=str(pending.get("kind", "search")),  # type: ignore[arg-type]
                        query=str(pending.get("query", "")),
                        radio_mode=bool(pending.get("radio_mode", False)),
                        enqueue_option=pending_enqueue,  # type: ignore[arg-type]
                    ),
                    candidates=candidates,
                    session_state_in=session_state_in,
                    app_state_in=app_state_in,
                    has_screen=has_screen,
                )

        # Step 3 — voice ordinal ("первая", "выбираю первую", "номер
        # три"). Last because we want named answers to win even when
        # they happen to contain an ordinal word ("Спальня первая").
        # On screenless smart speakers ordinal is the natural reply
        # when none of the names is easy to pronounce.
        if chosen_player is None:
            ordinal = _parse_ordinal_choice(command)
            if ordinal is not None:
                target_pid: str | None = (
                    candidate_ids[ordinal] if 0 <= ordinal < len(candidate_ids) else None
                )
                if target_pid is not None:
                    chosen_player = exposed_by_id.get(target_pid)
                    if chosen_player is not None:
                        self._logger.debug(
                            "Pending replay: voice ordinal %d → player %s",
                            ordinal,
                            chosen_player.name or chosen_player.player_id,
                        )
                # If the ordinal couldn't be resolved (out of range, or
                # the indexed player is no longer exposed), the user
                # clearly *meant* to pick from the disambiguation list —
                # re-ask with whichever candidates are still exposed
                # instead of falling through and mis-interpreting
                # "третья" as a play query.
                if chosen_player is None:
                    still_available = [
                        exposed_by_id[pid] for pid in candidate_ids if pid in exposed_by_id
                    ]
                    if still_available:
                        self._logger.info(
                            "Pending replay: ordinal=%d unresolvable; "
                            "re-asking with %d remaining candidate(s)",
                            ordinal,
                            len(still_available),
                        )
                        return self._build_disambiguation_response(
                            session=session,
                            parsed=ParsedCommand(
                                kind=str(pending.get("kind", "search")),  # type: ignore[arg-type]
                                query=str(pending.get("query", "")),
                                radio_mode=bool(pending.get("radio_mode", False)),
                                enqueue_option=pending_enqueue,  # type: ignore[arg-type]
                            ),
                            candidates=still_available,
                            session_state_in=session_state_in,
                            app_state_in=app_state_in,
                            has_screen=has_screen,
                        )
                    # else: no candidates remain at all — fall through.

        if chosen_player is None:
            return None

        replay = ParsedCommand(
            kind=str(pending.get("kind", "search")),  # type: ignore[arg-type]
            query=str(pending.get("query", "")),
            radio_mode=bool(pending.get("radio_mode", False)),
            enqueue_option=pending_enqueue,  # type: ignore[arg-type]
        )
        return await self._play_with_player(
            session=session,
            parsed=replay,
            player=chosen_player,
            base_session_state=session_state_in,
            base_app_state=app_state_in,
            has_screen=has_screen,
        )

    # -------------------------------------------------------------------
    # Yandex Dialogs response envelope
    # -------------------------------------------------------------------

    def _yandex_response(
        self,
        *,
        incoming_session: dict[str, Any],
        text: str,
        tts: str | None = None,
        end_session: bool = True,
        session_state: dict[str, Any] | None = None,
        application_state: dict[str, Any] | None = None,
        user_state_update: dict[str, Any] | None = None,
        buttons: list[dict[str, Any]] | None = None,
        card: dict[str, Any] | None = None,
    ) -> web.Response:
        """
        Build a Yandex Dialogs response envelope.

        ``session_state`` / ``application_state`` are full overwrites per
        Yandex spec; ``user_state_update`` is merged into the existing
        user-scoped state (set keys to None to clear). Omit a parameter
        to leave that bucket unchanged on Yandex's side.

        ``card`` accepts one of the three Yandex card shapes:
        ``BigImage`` (single image + title + description),
        ``ItemsList`` (1-5 items, each with image + title), or
        ``ImageGallery`` (1-7 images). Yandex silently drops the field
        on voice-only surfaces, so callers must still gate emission on
        ``meta.interfaces.screen`` to avoid wasted bandwidth and to
        honour the buttons-on-screen-only contract.

        Side effect: any time we set ``session_state`` or
        ``application_state``, the merged value is also written to the
        in-process state cache as a third-tier fallback (see
        ``_cache_put``). The cache is what makes disambiguation work
        on Yandex Station devices that don't echo `state.*` back.
        """
        # Yandex envelopes carry two user_id fields: the deprecated root
        # `session.user_id` (always present in current API revisions for
        # backwards compatibility) and the nested `session.user.user_id`
        # (set only when the user is account-linked). Prefer the root for
        # historical reasons but fall back to the nested form so the
        # echo doesn't leak an empty string if a future Yandex API
        # revision drops the root field.
        user_id = incoming_session.get("user_id") or _safe_dict(incoming_session.get("user")).get(
            "user_id", ""
        )
        echoed = {
            "session_id": incoming_session.get("session_id", ""),
            "message_id": incoming_session.get("message_id", 0),
            "user_id": user_id,
        }
        response_body: dict[str, Any] = {
            "text": text,
            "tts": tts if tts is not None else text,
            "end_session": end_session,
        }
        if buttons:
            response_body["buttons"] = buttons
        if card:
            response_body["card"] = card
        payload: dict[str, Any] = {
            "version": "1.0",
            "session": echoed,
            "response": response_body,
        }
        if session_state is not None:
            payload["session_state"] = session_state
        if application_state is not None:
            payload["application_state"] = application_state
        if user_state_update is not None:
            payload["user_state_update"] = user_state_update

        # Mirror state into the in-process cache. Prefer session_state
        # (most specific to the current conversation); fall back to
        # application_state if only that was set. We store a merged
        # snapshot so reads on the next turn pick up everything.
        cache_state: dict[str, Any] = {}
        if application_state is not None:
            cache_state.update(application_state)
        if session_state is not None:
            cache_state.update(session_state)
        if cache_state or session_state is not None or application_state is not None:
            self._cache_put(incoming_session, cache_state)

        return web.json_response(payload)
