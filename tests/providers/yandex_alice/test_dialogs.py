# ruff: noqa: RUF001, RUF002
"""Tests for provider/dialogs.py — webhook handler."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.enums import QueueOption, RepeatMode

from music_assistant.providers.yandex_alice.dialogs import (
    _STATE_CACHE_TTL_SEC,
    DialogsWebhookHandler,
    _tts_for,
)

if TYPE_CHECKING:
    from aiohttp import web


@dataclass
class MockPlayer:
    """Minimal player stub for webhook handler tests."""

    player_id: str = "p1"
    name: str = "Кухня"
    available: bool = True
    enabled: bool = True
    synced_to: str | None = None
    supported_features: set[str] = field(default_factory=set)
    powered: bool = True


class _MockPlayers:
    def __init__(self, players: list[MockPlayer]) -> None:
        """Initialise with a fixed player list."""
        self._players = players
        self.cmd_power = AsyncMock()

    def all_players(self) -> list[MockPlayer]:
        """Return all players."""
        return list(self._players)

    def get_player(self, player_id: str) -> MockPlayer | None:
        """Return player by id or None."""
        return next((p for p in self._players if p.player_id == player_id), None)


def _make_mass(players: list[MockPlayer], search_track: object = None) -> MagicMock:
    mass = MagicMock()
    mass.players = _MockPlayers(players)
    mass.music = MagicMock()

    @dataclass
    class _SearchResults:
        artists: list[object] = field(default_factory=list)
        albums: list[object] = field(default_factory=list)
        tracks: list[object] = field(default_factory=list)
        playlists: list[object] = field(default_factory=list)

    if search_track is not None:
        mass.music.search = AsyncMock(return_value=_SearchResults(tracks=[search_track]))
    else:
        mass.music.search = AsyncMock(return_value=_SearchResults())

    mass.music_providers = []
    mass.providers = []
    mass.player_queues = MagicMock()
    mass.player_queues.play_media = AsyncMock()
    mass.webserver = MagicMock()
    mass.webserver.register_dynamic_route = MagicMock(return_value=lambda: None)
    # mass.create_task must actually schedule the coroutine so fire-and-forget
    # tasks run when the test awaits asyncio.sleep(0).
    mass.create_task = lambda coro, **_kw: asyncio.ensure_future(coro)
    return mass


_TEST_SECRET = "topsecret"


def _build_request(body: dict[str, Any], secret: str = _TEST_SECRET) -> web.Request:
    """Build a mocked aiohttp Request that returns the given JSON body."""
    req = make_mocked_request(
        "POST",
        f"/api/yandex_dialogs/webhook/{secret}",
        match_info={"secret": secret},
    )
    req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _response_body(resp: web.Response) -> dict[str, Any]:
    """Decode a web.json_response body into a dict for assertions."""
    decoded: dict[str, Any] = json.loads(resp.body)  # type: ignore[arg-type]
    return decoded


@pytest.mark.asyncio
class TestDialogsWebhookHandler:
    """End-to-end tests for the webhook entry point."""

    def _make_handler(self, mass: MagicMock, **kwargs: object) -> DialogsWebhookHandler:
        """Build a handler with sensible test defaults."""
        return DialogsWebhookHandler(
            mass,
            skill_id=str(kwargs.get("skill_id", "skill-uuid-1")),
            webhook_secret=str(kwargs.get("webhook_secret", "topsecret")),
            exposed_player_ids=kwargs.get("exposed_player_ids"),  # type: ignore[arg-type]
        )

    async def test_register_routes_calls_mass_webserver(self) -> None:
        """register_routes calls register_dynamic_route with the correct URL."""
        mass = _make_mass([])
        handler = self._make_handler(mass)
        handler.register_routes()
        mass.webserver.register_dynamic_route.assert_called_once()
        path_arg = mass.webserver.register_dynamic_route.call_args[0][0]
        assert path_arg == "/api/yandex_dialogs/webhook/topsecret"

    async def test_unregister_routes(self) -> None:
        """unregister_routes calls the unregister callback returned by register_dynamic_route."""
        mass = _make_mass([])
        unregister = MagicMock()
        mass.webserver.register_dynamic_route = MagicMock(return_value=unregister)
        handler = self._make_handler(mass)
        handler.register_routes()
        handler.unregister_routes()
        unregister.assert_called_once()

    async def test_secret_mismatch_returns_404(self) -> None:
        """Webhook request with wrong URL secret is rejected with 404."""
        mass = _make_mass([])
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1"},
            "request": {"command": "включи Metallica"},
        }
        req = make_mocked_request(
            "POST",
            "/api/yandex_dialogs/webhook/wrong",
            match_info={"secret": "wrong"},
        )
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
        resp = await handler._handle_webhook(req)
        assert resp.status == 404

    async def test_secret_parsed_from_path_when_no_match_info(self) -> None:
        """
        Cover the production secret-from-path fallback in `_handle_webhook`.

        Production registers an exact route (no `{secret}` variable), so
        `request.match_info` is empty and the handler parses the secret
        from `request.path`. This test passes `match_info={}` to exercise
        that branch.
        """
        track = MagicMock(uri="library://track/123", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на кухне"},
        }
        req = make_mocked_request(
            "POST",
            f"/api/yandex_dialogs/webhook/{_TEST_SECRET}",
            match_info={},
        )
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
        resp = await handler._handle_webhook(req)
        # If path parsing works, secret matches and we reach the play branch (200).
        assert resp.status == 200

    async def test_skill_id_mismatch_returns_401(self) -> None:
        """Payload with wrong skill_id is rejected with 401."""
        mass = _make_mass([])
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "different-skill", "session_id": "s1"},
            "request": {"command": "включи Metallica"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 401

    async def test_malformed_json_returns_graceful_response(self) -> None:
        """A request body that cannot be decoded never escapes as HTTP 500."""
        mass = _make_mass([])
        handler = self._make_handler(mass)
        req = make_mocked_request(
            "POST",
            f"/api/yandex_dialogs/webhook/{_TEST_SECRET}",
            match_info={"secret": _TEST_SECRET},
        )
        req.json = AsyncMock(side_effect=ValueError("invalid json"))  # type: ignore[method-assign]

        resp = await handler._handle_webhook(req)

        assert resp.status == 200
        assert "пошло не так с запросом" in _response_body(resp)["response"]["text"].lower()

    async def test_non_object_json_returns_graceful_response(self) -> None:
        """A valid JSON value with the wrong top-level type is rejected safely."""
        mass = _make_mass([])
        handler = self._make_handler(mass)
        req = make_mocked_request(
            "POST",
            f"/api/yandex_dialogs/webhook/{_TEST_SECRET}",
            match_info={"secret": _TEST_SECRET},
        )
        req.json = AsyncMock(return_value=["not", "an", "object"])  # type: ignore[method-assign]

        resp = await handler._handle_webhook(req)

        assert resp.status == 200
        assert "пошло не так с запросом" in _response_body(resp)["response"]["text"].lower()

    async def test_session_new_empty_command_greets(self) -> None:
        """New session with empty command returns 200 greeting without playing."""
        mass = _make_mass([])
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": True},
            "request": {"command": ""},
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 200
        mass.player_queues.play_media.assert_not_awaited()

    async def test_unknown_player_asks_for_clarification(self) -> None:
        """Command mentioning an unknown player returns 200 without playing."""
        mass = _make_mass([MockPlayer(player_id="p1", name="Спальня")])
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на Кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 200
        mass.player_queues.play_media.assert_not_awaited()

    async def test_no_results_says_not_found(self) -> None:
        """No search results returns 200 without playing."""
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи nonexistent на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 200
        mass.player_queues.play_media.assert_not_awaited()

    async def test_full_happy_path_starts_play_media(self) -> None:
        """Resolved track triggers play_media on the correct player."""
        track = MagicMock(uri="library://track/123", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 200
        # Allow the fire-and-forget task to run.
        await asyncio.sleep(0)
        mass.player_queues.play_media.assert_awaited_once()
        call_kwargs = mass.player_queues.play_media.call_args.kwargs
        assert call_kwargs["queue_id"] == "p1"
        assert call_kwargs["media"] is track

    async def test_background_play_failure_is_logged_at_default_level(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed fire-and-forget play remains diagnosable without DEBUG logging."""
        track = MagicMock(uri="library://track/123", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        mass.player_queues.play_media = AsyncMock(side_effect=RuntimeError("cannot play"))
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на кухне"},
        }

        with caplog.at_level("ERROR", logger="music_assistant.providers.yandex_alice.dialogs"):
            resp = await handler._handle_webhook(_build_request(body))
            await asyncio.sleep(0)

        assert resp.status == 200
        assert any("Background playback failed for player p1" in r.message for r in caplog.records)

    async def test_dangerous_context_log_redacts_command(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        Flagged content must NOT leak into DEBUG logs even at operator's request.

        Copilot review on PR #18: the structured "Webhook recv" line was
        emitting `cmd=...` and `raw=...` *before* the dangerous_context
        refusal branch, so flagged phrases ended up in
        $HOME/.musicassistant/musicassistant.log when DEBUG was on.
        """
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        handler = self._make_handler(mass)
        sensitive = "очень плохая фраза которую яндекс пометил"
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": sensitive,
                "original_utterance": sensitive,
                "markup": {"dangerous_context": True},
            },
        }
        with caplog.at_level("DEBUG", logger="music_assistant.providers.yandex_alice.dialogs"):
            await handler._handle_webhook(_build_request(body))
        # Flagged content must not be present in any log record.
        for record in caplog.records:
            assert sensitive not in record.getMessage()
        # Confirm we DID emit the structured log line (with the redaction marker)
        # — silent skip would also satisfy the negative assertion above and is
        # not what we want.
        assert any("redacted: dangerous_context" in r.getMessage() for r in caplog.records)

    async def test_unexpected_inner_exception_returns_graceful_fallback(self) -> None:
        """
        An unexpected raise from inner dispatch surfaces as a Russian fallback, not HTTP 500.

        Flagged in the upstream PR review (#3843, @chrisuthe): only the
        ``request.json()`` parse was guarded; everything afterwards
        (parsers, search, dispatch) bubbled to aiohttp → HTTP 500 →
        Alice silence. The handler now wraps the post-auth body in
        ``try / except`` to keep the user-facing response intact.
        """
        # Make `mass.players.all_players` raise — this triggers inside the
        # play-resolve path so the exception happens DEEP in dispatch,
        # well past the auth gate and parser pass.
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        mass.players.all_players = MagicMock(side_effect=RuntimeError("boom"))
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        # Critical: 200 OK with a Russian fallback, NOT HTTP 500.
        assert resp.status == 200
        body_out = _response_body(resp)
        assert "что-то пошло не так" in body_out["response"]["text"].lower()
        # Session continues so the user can re-issue a command.
        assert body_out["response"]["end_session"] is False


@pytest.mark.asyncio
class TestSuggestionButtons:
    """Phase 1 / P1.3: play- and control-success responses surface follow-up buttons on screen."""

    def _make_handler(self, mass: MagicMock) -> DialogsWebhookHandler:
        return DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)

    async def test_play_success_emits_buttons_on_screen(self) -> None:
        """Play-success on screened surface includes Следующая/Пауза/Громче/Тише buttons."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = self._make_handler(mass)
        body = {
            "meta": {"interfaces": {"screen": {}}},
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        body_out = _response_body(resp)
        button_titles = [b["title"] for b in body_out["response"]["buttons"]]
        assert button_titles == ["Следующая", "Пауза", "Громче", "Тише"]

    async def test_play_success_no_buttons_voice_only(self) -> None:
        """Play-success on a voice-only surface omits buttons entirely."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = self._make_handler(mass)
        body = {
            # No meta.interfaces — voice-only (Yandex Mini etc.)
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        body_out = _response_body(resp)
        assert "buttons" not in body_out["response"]

    async def test_control_success_emits_buttons_on_screen(self) -> None:
        """Control-success (e.g. pause) on screened surface includes the same buttons."""
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        mass.player_queues.pause = AsyncMock()
        handler = self._make_handler(mass)
        body = {
            "meta": {"interfaces": {"screen": {}}},
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "пауза на кухне",
                "nlu": {"intents": {"control.pause": {}}},
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        body_out = _response_body(resp)
        button_titles = [b["title"] for b in body_out["response"]["buttons"]]
        assert button_titles == ["Следующая", "Пауза", "Громче", "Тише"]


@pytest.mark.asyncio
class TestPlatformIntentDispatch:
    """Phase 2: request.nlu.intents pre-classification takes precedence over regex."""

    def _handler(self, mass: MagicMock) -> DialogsWebhookHandler:
        return DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)

    async def test_control_pause_via_platform_intent(self) -> None:
        """`request.nlu.intents['control.pause']` → ParsedControl(action='pause')."""
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        mass.player_queues.pause = AsyncMock()
        handler = self._handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "пауза",
                "nlu": {"intents": {"control.pause": {}}},
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        # Pause was dispatched even though command="пауза" would also match regex.
        mass.player_queues.pause.assert_awaited_once_with("p1")

    async def test_play_my_wave_via_platform_intent(self) -> None:
        """`request.nlu.intents['play.my_wave']` → ParsedCommand(kind='my_wave')."""
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        # _resolve_my_wave returns None when yandex_music provider absent → handler
        # surfaces "не нашёл такую музыку" but still went through the my_wave path.
        handler = self._handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                # `command` is the noisy raw — wouldn't normally classify as my_wave,
                # but the platform intent overrides it.
                "command": "что-то совсем другое",
                "nlu": {"intents": {"play.my_wave": {}}},
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        body_out = _response_body(resp)
        # Platform path responded — it didn't fall through to "не понял".
        # When yandex_music isn't available, the response is a graceful
        # "не нашёл такую музыку" rather than "не понял команду".
        assert "не понял" not in body_out["response"]["text"].lower()

    async def test_unrecognised_intent_falls_back_to_regex_for_transfer(self) -> None:
        """
        Unknown form_name + a transfer phrase → regex parse_control catches it.

        After v1.4.0 ``parse_control`` only handles ``transfer`` (the
        per-user dynamic enum that can't fit a static intent grammar).
        This is the documented fallback path — the platform-side
        intent is unrecognised, so we drop into regex, and only the
        transfer family is recognised there.
        """
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ]
        )
        mass.player_queues.transfer_queue = AsyncMock()
        handler = self._handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "переведи на спальню",
                "nlu": {"intents": {"unknown.intent": {}}},
            },
            "state": {"session": {"last_player_id": "p1"}},
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        mass.player_queues.transfer_queue.assert_awaited_once()

    async def test_empty_intents_block_falls_through_to_play_parser(self) -> None:
        """
        Empty ``intents={}`` + non-transfer phrase → play parser runs.

        After v1.4.0 the regex control parser only recognises
        ``transfer``; a phrase like "следующая" with no platform-side
        match no longer dispatches as control. The handler is expected
        to treat it as a graceful fallback (the play search path
        produces "не нашёл такую музыку: ...") rather than crashing.
        """
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        mass.player_queues.next = AsyncMock()
        handler = self._handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "следующая",
                "nlu": {"intents": {}},
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        # Control wasn't dispatched (no platform intent + regex doesn't cover it).
        mass.player_queues.next.assert_not_awaited()
        # Handler responded gracefully (HTTP 200).
        assert resp.status == 200


@pytest.mark.asyncio
class TestBuiltInIntents:
    """Phase 2 follow-up: YANDEX.REJECT / YANDEX.HELP handling in pending flows."""

    def _handler(self, mass: MagicMock) -> DialogsWebhookHandler:
        return DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)

    async def test_reject_in_pending_disambiguation_cancels(self) -> None:
        """YANDEX.REJECT clears pending_command and ends session with confirmation."""
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        handler = self._handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "отмена",
                "nlu": {"intents": {"YANDEX.REJECT": {}}},
            },
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    }
                }
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        assert body_out["response"]["end_session"] is True
        assert "отменил" in body_out["response"]["text"].lower()
        # pending_command cleared from session_state on response.
        assert "pending_command" not in body_out["session_state"]
        mass.player_queues.play_media.assert_not_awaited()

    async def test_reject_in_awaiting_query_cancels(self) -> None:
        """YANDEX.REJECT in slot-elicit ('Что включить?') also exits cleanly."""
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        handler = self._handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "неважно",
                "nlu": {"intents": {"YANDEX.REJECT": {}}},
            },
            "state": {"session": {"awaiting_query": True}},
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        assert body_out["response"]["end_session"] is True
        assert "awaiting_query" not in body_out["session_state"]

    async def test_reject_with_no_pending_falls_through(self) -> None:
        """
        YANDEX.REJECT outside of any prompt context → falls through to normal flow.

        The intent isn't a free-standing 'cancel app' signal — the user
        could just be talking. If parse_command also can't make sense of
        'отмена', it lands as a normal "не нашёл" search response.
        """
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        handler = self._handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "отмена",
                "nlu": {"intents": {"YANDEX.REJECT": {}}},
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        # NOT the cancel response — handler fell through to play-search.
        assert "отменил" not in body_out["response"]["text"].lower()

    async def test_help_in_pending_emits_disambiguation_hint(self) -> None:
        """YANDEX.HELP during disambiguation tells the user how to answer."""
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        handler = self._handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "помоги",
                "nlu": {"intents": {"YANDEX.HELP": {}}},
            },
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    }
                }
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        assert body_out["response"]["end_session"] is False
        assert "колонки" in body_out["response"]["text"].lower()

    async def test_help_in_awaiting_emits_query_hint(self) -> None:
        """YANDEX.HELP during slot-elicit suggests example queries."""
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        handler = self._handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "что я могу",
                "nlu": {"intents": {"YANDEX.HELP": {}}},
            },
            "state": {"session": {"awaiting_query": True}},
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        assert body_out["response"]["end_session"] is False
        assert "артиста" in body_out["response"]["text"].lower()

    async def test_help_clean_state_emits_generic_hint(self) -> None:
        """YANDEX.HELP with no in-flight prompt → generic example."""
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        handler = self._handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "помощь",
                "nlu": {"intents": {"YANDEX.HELP": {}}},
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        assert "включи рок" in body_out["response"]["text"].lower()


@pytest.mark.asyncio
class TestVoiceContinuation:
    """Phase 1 / P1.4: opt-in `end_session=false` after play / control success."""

    async def test_play_success_ends_session_by_default(self) -> None:
        """Without the toggle, play-success closes the session (today's UX)."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        body_out = _response_body(resp)
        assert body_out["response"]["end_session"] is True

    async def test_play_success_keeps_session_open_when_continuation_on(self) -> None:
        """With continuation on, play-success keeps the conversation alive."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = DialogsWebhookHandler(
            mass,
            skill_id="skill-uuid-1",
            webhook_secret=_TEST_SECRET,
            voice_continuation=True,
        )
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        body_out = _response_body(resp)
        assert body_out["response"]["end_session"] is False

    async def test_control_success_keeps_session_open_when_continuation_on(self) -> None:
        """Continuation also applies to control-success (pause / volume / etc.)."""
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        mass.player_queues.pause = AsyncMock()
        handler = DialogsWebhookHandler(
            mass,
            skill_id="skill-uuid-1",
            webhook_secret=_TEST_SECRET,
            voice_continuation=True,
        )
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "пауза на кухне",
                "nlu": {"intents": {"control.pause": {}}},
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        body_out = _response_body(resp)
        assert body_out["response"]["end_session"] is False

    async def test_stop_action_ends_session_even_with_continuation_on(self) -> None:
        """`стоп / выключи` always closes the session regardless of the toggle."""
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        mass.player_queues.stop = AsyncMock()
        handler = DialogsWebhookHandler(
            mass,
            skill_id="skill-uuid-1",
            webhook_secret=_TEST_SECRET,
            voice_continuation=True,
        )
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "стоп на кухне",
                "nlu": {"intents": {"control.stop": {}}},
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        body_out = _response_body(resp)
        assert body_out["response"]["end_session"] is True


# ---------------------------------------------------------------------------
# Yandex state envelope (P0.1) + tts split (P0.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestStatePersistence:
    """Tests that the handler reads/writes Yandex state envelope correctly."""

    def _make_handler(self, mass: MagicMock) -> DialogsWebhookHandler:
        return DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)

    async def test_resolved_player_persisted_in_session_and_application_state(self) -> None:
        """Successful play writes last_player_id to session_state and application_state."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        body_out = _response_body(resp)
        assert body_out["session_state"]["last_player_id"] == "p1"
        assert body_out["application_state"]["last_player_id"] == "p1"
        # No user identity in the request → no user_state_update.
        assert "user_state_update" not in body_out

    async def test_user_state_written_when_user_id_present(self) -> None:
        """When session.user.user_id is set, response merges preferred_player_id."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = self._make_handler(mass)
        body = {
            "session": {
                "skill_id": "skill-uuid-1",
                "session_id": "s1",
                "new": False,
                "user": {"user_id": "yandex-user-1"},
            },
            "request": {"command": "включи Metallica на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        body_out = _response_body(resp)
        assert body_out["user_state_update"] == {"preferred_player_id": "p1"}

    async def test_default_player_priority_session_over_application(self) -> None:
        """When command has no player hint, session.last_player_id wins over application's."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [MockPlayer(player_id="p1", name="Кухня"), MockPlayer(player_id="p2", name="Спальня")],
            search_track=track,
        )
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Beatles"},
            "state": {
                "session": {"last_player_id": "p1"},
                "application": {"last_player_id": "p2"},
            },
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p1"

    async def test_default_player_falls_through_to_application(self) -> None:
        """No session.last_player_id — application_state wins."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [MockPlayer(player_id="p1", name="Кухня"), MockPlayer(player_id="p2", name="Спальня")],
            search_track=track,
        )
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Beatles"},
            "state": {"application": {"last_player_id": "p2"}},
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p2"

    async def test_default_player_falls_through_to_user(self) -> None:
        """Both session and application empty — user.preferred_player_id wins."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [MockPlayer(player_id="p1", name="Кухня"), MockPlayer(player_id="p2", name="Спальня")],
            search_track=track,
        )
        handler = self._make_handler(mass)
        body = {
            "session": {
                "skill_id": "skill-uuid-1",
                "session_id": "s1",
                "new": False,
                "user": {"user_id": "yandex-user-1"},
            },
            "request": {"command": "включи Beatles"},
            "state": {"user": {"preferred_player_id": "p2"}},
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p2"

    async def test_user_id_echo_falls_back_to_nested(self) -> None:
        """When root session.user_id is missing, echo the nested session.user.user_id."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = self._make_handler(mass)
        body = {
            "session": {
                "skill_id": "skill-uuid-1",
                "session_id": "s1",
                "new": False,
                # No root "user_id"; only the nested one.
                "user": {"user_id": "yandex-user-42"},
            },
            "request": {"command": "включи Metallica на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        assert body_out["session"]["user_id"] == "yandex-user-42"

    async def test_session_state_preserved_on_player_not_found(self) -> None:
        """Even on error, existing session_state is echoed back so other keys aren't lost."""
        mass = _make_mass([MockPlayer(player_id="p1", name="Спальня")])
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на Кухне"},
            "state": {"session": {"foo": "bar"}},
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        assert body_out["session_state"] == {"foo": "bar"}


class TestTtsHelper:
    """Tests for _tts_for stress-mark substitution."""

    def test_known_russian_word_gets_stress_mark(self) -> None:
        """A known Russian word has `+` injected before the stressed vowel."""
        assert _tts_for("Включаю джаз") == "Включ+аю джаз"

    def test_unknown_word_passes_through(self) -> None:
        """A word not in the dict is unchanged."""
        assert _tts_for("Привет мир") == "Привет мир"

    def test_empty_input(self) -> None:
        """Empty input is returned as-is."""
        assert _tts_for("") == ""

    def test_capitalisation_preserved(self) -> None:
        """Original capitalisation of the first letter is preserved."""
        # All-lowercase original.
        assert _tts_for("включаю джаз") == "включ+аю джаз"
        # Capitalised original.
        assert _tts_for("Включаю джаз") == "Включ+аю джаз"

    def test_foreign_band_transliterated(self) -> None:
        """Latin band names are transliterated to Cyrillic with stress marks."""
        # Single-word foreign band (regex pass).
        assert _tts_for("Включаю Metallica") == "Включ+аю Мет+аллика"
        # Lowercase form preserved.
        assert _tts_for("включаю metallica") == "включ+аю мет+аллика"

    def test_foreign_phrase_transliterated(self) -> None:
        """Multi-word foreign band names are matched via the phrase pass."""
        result = _tts_for("Включаю Iron Maiden на кухне")
        assert "+айрон м+эйден" in result.lower()
        # Russian response words still get their stress mark in the same call.
        assert "Включ+аю" in result

    def test_phrase_pass_handles_overlap(self) -> None:
        """Longer phrases match before shorter sub-phrases (declared order)."""
        # "imagine dragons" must win over the single-word "imagine" entry.
        result = _tts_for("Imagine Dragons")
        assert "имадж+ин др+агонс" in result.lower()


@pytest.mark.asyncio
class TestTtsResponseField:
    """Test that the handler emits separate text + tts in the response envelope."""

    async def test_response_tts_differs_from_text_when_known_word_used(self) -> None:
        """Happy path response has different `tts` from `text` when stress-mark fires."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        text = body_out["response"]["text"]
        tts = body_out["response"]["tts"]
        assert text != tts
        assert "включ+аю" in tts.lower()


# ---------------------------------------------------------------------------
# Control commands integration (P0.6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestControlCommandsIntegration:
    """Integration tests for the control branch in _handle_webhook."""

    def _setup_mass_with_control_methods(self, players: list[MockPlayer]) -> MagicMock:
        mass = _make_mass(players)
        mass.player_queues.pause = AsyncMock()
        mass.player_queues.resume = AsyncMock()
        mass.player_queues.stop = AsyncMock()
        mass.player_queues.next = AsyncMock()
        mass.player_queues.previous = AsyncMock()
        mass.player_queues.set_shuffle = AsyncMock()
        mass.player_queues.set_repeat = MagicMock()  # NB: sync
        mass.player_queues.skip = AsyncMock()
        mass.player_queues.seek = AsyncMock()
        mass.player_queues.transfer_queue = AsyncMock()
        mass.player_queues.get = MagicMock(return_value=None)
        mass.players.cmd_volume_up = AsyncMock()
        mass.players.cmd_volume_down = AsyncMock()
        mass.players.cmd_volume_set = AsyncMock()
        mass.players.cmd_volume_mute = AsyncMock()
        return mass

    async def test_pause_command_calls_player_queues_pause(self) -> None:
        """'пауза на кухне' → mass.player_queues.pause(p1) and confirms in response."""
        mass = self._setup_mass_with_control_methods([MockPlayer(player_id="p1", name="Кухня")])
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "пауза на кухне",
                "nlu": {"intents": {"control.pause": {}}},
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        mass.player_queues.pause.assert_awaited_once_with("p1")
        body_out = _response_body(resp)
        assert body_out["response"]["text"] == "Пауза."
        # State persisted as in play branch.
        assert body_out["session_state"]["last_player_id"] == "p1"
        assert body_out["application_state"]["last_player_id"] == "p1"
        # play_media should NOT be called for control commands.
        mass.player_queues.play_media.assert_not_awaited()

    async def test_volume_set_command(self) -> None:
        """'громкость 50 на кухне' → cmd_volume_set(p1, 50)."""
        mass = self._setup_mass_with_control_methods([MockPlayer(player_id="p1", name="Кухня")])
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "громкость 50 на кухне",
                "nlu": {
                    "intents": {
                        "control.volume_set": {
                            "slots": {"level": {"type": "YANDEX.NUMBER", "value": 50}}
                        }
                    }
                },
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        mass.players.cmd_volume_set.assert_awaited_once_with("p1", 50)

    async def test_control_uses_default_player_from_state(self) -> None:
        """A control phrase without explicit hint uses state.session.last_player_id."""
        mass = self._setup_mass_with_control_methods(
            [MockPlayer(player_id="p1", name="Кухня"), MockPlayer(player_id="p2", name="Спальня")]
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "пауза",
                "nlu": {"intents": {"control.pause": {}}},
            },
            "state": {"session": {"last_player_id": "p2"}},
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        mass.player_queues.pause.assert_awaited_once_with("p2")

    async def test_control_unknown_player_asks_for_clarification(self) -> None:
        """Control command with an unknown player hint returns a clarification."""
        mass = self._setup_mass_with_control_methods([MockPlayer(player_id="p1", name="Спальня")])
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "пауза на гостиной",
                "nlu": {"intents": {"control.pause": {}}},
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 200
        mass.player_queues.pause.assert_not_awaited()
        body_out = _response_body(resp)
        assert "Не нашёл колонку «гостиной»" in body_out["response"]["text"]

    async def test_forget_player_clears_state_tiers(self) -> None:
        """
        'забудь колонку' clears last_player_id from session/application/cache.

        After the user picks a player via disambiguation, every later play
        command without an explicit hint plays on it (by design — sticky
        default for ergonomics). Saying 'забудь колонку' resets that so
        the next ambiguous command asks again.
        """
        mass = self._setup_mass_with_control_methods(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ]
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        # Pre-seed cache with a stale default-player.
        handler._state_cache["user:u1"] = (
            {"last_player_id": "p1", "pending_command": {"query": "stale"}},
            time.monotonic(),
        )
        body = {
            "session": {
                "skill_id": "skill-uuid-1",
                "session_id": "s1",
                "new": False,
                "user": {"user_id": "u1"},
            },
            "request": {
                "command": "забудь колонку",
                "nlu": {"intents": {"control.forget_player": {}}},
            },
            "state": {
                "session": {"last_player_id": "p1", "awaiting_query": True},
                "application": {
                    "last_player_id": "p1",
                    "pending_command": {"query": "stale"},
                },
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        assert "Хорошо" in body_out["response"]["text"]
        # last_player_id removed from session and application state.
        assert "last_player_id" not in body_out["session_state"]
        assert "last_player_id" not in body_out["application_state"]
        assert "awaiting_query" not in body_out["session_state"]
        assert "pending_command" not in body_out["application_state"]
        # user_state_update sets preferred_player_id to None (Yandex
        # protocol: None = delete the key from merged user state).
        assert body_out["user_state_update"] == {"preferred_player_id": None}
        # Cache rewritten with no last_player_id.
        cached = handler._cache_get({"user": {"user_id": "u1"}})
        assert "last_player_id" not in cached
        assert "pending_command" not in cached

    async def test_list_players_returns_player_names(self) -> None:
        """'сколько колонок видишь' → response with the count and names of exposed players."""
        mass = self._setup_mass_with_control_methods(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
                MockPlayer(player_id="p3", name="Гостиная"),
            ]
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "сколько колонок видишь",
                "nlu": {"intents": {"control.list_players": {}}},
            },
            "state": {
                "session": {"awaiting_query": True},
                "application": {"pending_command": {"query": "stale"}},
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 200
        body_out = _response_body(resp)
        text = body_out["response"]["text"]
        assert "Вижу 3 колонки" in text
        assert "Кухня" in text
        assert "Спальня" in text
        assert "Гостиная" in text
        # Informational query — keep the mic open for follow-ups.
        assert body_out["response"]["end_session"] is False
        assert "awaiting_query" not in body_out["session_state"]
        assert "pending_command" not in body_out["application_state"]
        # No playback or control was dispatched.
        mass.player_queues.pause.assert_not_awaited()
        mass.player_queues.play_media.assert_not_awaited()

    async def test_list_players_skips_unavailable(self) -> None:
        """Only available + enabled + non-synced players are counted."""
        mass = self._setup_mass_with_control_methods(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Disabled", enabled=False),
                MockPlayer(player_id="p3", name="Unavailable", available=False),
                MockPlayer(player_id="p4", name="Synced", synced_to="leader"),
            ]
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "какие колонки",
                "nlu": {"intents": {"control.list_players": {}}},
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        text = body_out["response"]["text"]
        assert "Вижу одну колонку: Кухня" in text
        assert "Disabled" not in text
        assert "Unavailable" not in text
        assert "Synced" not in text

    async def test_control_no_hint_no_default_asks_for_player(self) -> None:
        """
        Control with no hint + no default + multi-player → ask for the player.

        Previously responded with the misleading "Не нашёл колонку «(не указано)»";
        now the message tells the user to specify the player.
        """
        mass = self._setup_mass_with_control_methods(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ]
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "пауза",
                "nlu": {"intents": {"control.pause": {}}},
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 200
        mass.player_queues.pause.assert_not_awaited()
        body_out = _response_body(resp)
        text = body_out["response"]["text"]
        assert "(не указано)" not in text
        assert "на какой колонке" in text.lower()

    # -------------------------------------------------------------------
    # v1.9.0 — six new commands
    # -------------------------------------------------------------------

    async def test_now_playing_returns_track(self) -> None:
        """
        'что играет на кухне' → reads queue.current_item.name.

        Yandex pre-classifies "что играет" via control.now_playing intent;
        the player_hint "кухне" is recovered from the trailing "на" suffix
        of the raw command text.
        """
        mass = self._setup_mass_with_control_methods([MockPlayer(player_id="p1", name="Кухня")])
        # Mock a queue with a current item.
        queue = MagicMock()
        queue.current_item = MagicMock(name="The Beatles - Let It Be")
        queue.current_item.name = "The Beatles - Let It Be"
        mass.player_queues.get = MagicMock(return_value=queue)
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "что играет на кухне",
                "nlu": {"intents": {"control.now_playing": {}}},
            },
            "state": {
                "session": {"awaiting_query": True},
                "application": {"pending_command": {"query": "stale"}},
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        assert "The Beatles - Let It Be" in body_out["response"]["text"]
        assert "awaiting_query" not in body_out["session_state"]
        assert "pending_command" not in body_out["application_state"]
        # No MA mutation.
        mass.player_queues.pause.assert_not_awaited()
        mass.player_queues.play_media.assert_not_awaited()

    async def test_now_playing_idle_queue(self) -> None:
        """'что играет' on an idle queue → 'ничего не играет'."""
        mass = self._setup_mass_with_control_methods([MockPlayer(player_id="p1", name="Кухня")])
        queue = MagicMock()
        queue.current_item = None
        mass.player_queues.get = MagicMock(return_value=queue)
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "что играет на кухне",
                "nlu": {"intents": {"control.now_playing": {}}},
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        assert "ничего не играет" in body_out["response"]["text"]

    async def test_shuffle_on(self) -> None:
        """'перемешай на кухне' → set_shuffle(p1, True)."""
        mass = self._setup_mass_with_control_methods([MockPlayer(player_id="p1", name="Кухня")])
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "перемешай на кухне",
                "nlu": {"intents": {"control.shuffle_on": {}}},
            },
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        mass.player_queues.set_shuffle.assert_awaited_once_with("p1", shuffle_enabled=True)

    async def test_shuffle_off(self) -> None:
        """'выключи перемешивание на кухне' → set_shuffle(p1, False)."""
        mass = self._setup_mass_with_control_methods([MockPlayer(player_id="p1", name="Кухня")])
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "выключи перемешивание на кухне",
                "nlu": {"intents": {"control.shuffle_off": {}}},
            },
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        mass.player_queues.set_shuffle.assert_awaited_once_with("p1", shuffle_enabled=False)

    async def test_repeat_one(self) -> None:
        """'повтори песню на кухне' → set_repeat(p1, RepeatMode.ONE) — sync, not awaited."""
        mass = self._setup_mass_with_control_methods([MockPlayer(player_id="p1", name="Кухня")])
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "повтори песню на кухне",
                "nlu": {"intents": {"control.repeat_one": {}}},
            },
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        mass.player_queues.set_repeat.assert_called_once_with("p1", RepeatMode.ONE)

    async def test_seek_forward_minute(self) -> None:
        """
        'перемотай вперёд на 1 минуту на кухне' → skip(p1, 60).

        Yandex extracts amount=1 (YANDEX.NUMBER) and unit="minutes"
        (custom time_unit entity); the runtime mapper multiplies by 60.
        """
        mass = self._setup_mass_with_control_methods([MockPlayer(player_id="p1", name="Кухня")])
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "перемотай вперёд на 1 минуту на кухне",
                "nlu": {
                    "intents": {
                        "control.seek_forward": {
                            "slots": {
                                "amount": {"type": "YANDEX.NUMBER", "value": 1},
                                "unit": {"type": "time_unit", "value": "minutes"},
                            }
                        }
                    }
                },
            },
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        mass.player_queues.skip.assert_awaited_once_with("p1", seconds=60)

    async def test_seek_back_seconds(self) -> None:
        """'назад на 30 секунд на кухне' → skip(p1, -30)."""
        mass = self._setup_mass_with_control_methods([MockPlayer(player_id="p1", name="Кухня")])
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "перемотай назад на 30 секунд на кухне",
                "nlu": {
                    "intents": {
                        "control.seek_back": {
                            "slots": {
                                "amount": {"type": "YANDEX.NUMBER", "value": 30},
                                "unit": {"type": "time_unit", "value": "seconds"},
                            }
                        }
                    }
                },
            },
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        mass.player_queues.skip.assert_awaited_once_with("p1", seconds=-30)

    async def test_seek_start(self) -> None:
        """'к началу на кухне' → seek(p1, position=0)."""
        mass = self._setup_mass_with_control_methods([MockPlayer(player_id="p1", name="Кухня")])
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "к началу на кухне",
                "nlu": {"intents": {"control.seek_start": {}}},
            },
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        mass.player_queues.seek.assert_awaited_once_with("p1", position=0)

    async def test_transfer_to_target(self) -> None:
        """'переведи на спальню' with default=p1 → transfer_queue(p1, p2); last_player_id→p2."""
        mass = self._setup_mass_with_control_methods(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ]
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "переведи на спальню"},
            "state": {
                "session": {"last_player_id": "p1", "awaiting_query": True},
                "application": {
                    "last_player_id": "p1",
                    "pending_command": {"query": "stale"},
                },
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        mass.player_queues.transfer_queue.assert_awaited_once_with(
            source_queue_id="p1", target_queue_id="p2"
        )
        body_out = _response_body(resp)
        assert "Спальня" in body_out["response"]["text"]
        assert body_out["session_state"]["last_player_id"] == "p2"
        assert body_out["application_state"]["last_player_id"] == "p2"
        assert "awaiting_query" not in body_out["session_state"]
        assert "pending_command" not in body_out["application_state"]

    async def test_background_transfer_failure_is_logged_at_default_level(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed fire-and-forget transfer is logged explicitly at ERROR."""
        mass = self._setup_mass_with_control_methods(
            [MockPlayer(player_id="p1", name="Кухня"), MockPlayer(player_id="p2", name="Спальня")]
        )
        mass.player_queues.transfer_queue = AsyncMock(side_effect=RuntimeError("cannot transfer"))
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "переведи на спальню"},
            "state": {"session": {"last_player_id": "p1"}},
        }

        with caplog.at_level("ERROR", logger="music_assistant.providers.yandex_alice.dialogs"):
            resp = await handler._handle_webhook(_build_request(body))
            await asyncio.sleep(0)

        assert resp.status == 200
        assert any(
            "Background queue transfer failed: p1 -> p2" in r.message for r in caplog.records
        )

    async def test_transfer_no_default_replies_with_hint(self) -> None:
        """Transfer without saved last_player_id replies with 'сначала включи'."""
        mass = self._setup_mass_with_control_methods(
            [MockPlayer(player_id="p1", name="Кухня"), MockPlayer(player_id="p2", name="Спальня")]
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "переведи на спальню"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        assert "Сначала включи" in body_out["response"]["text"]
        mass.player_queues.transfer_queue.assert_not_awaited()

    async def test_transfer_to_same_player(self) -> None:
        """'переведи на кухню' when default already = кухня → 'уже играет'."""
        mass = self._setup_mass_with_control_methods(
            [MockPlayer(player_id="p1", name="Кухня"), MockPlayer(player_id="p2", name="Спальня")]
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "переведи на кухню"},
            "state": {"session": {"last_player_id": "p1"}},
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        assert "Уже играет" in body_out["response"]["text"]
        mass.player_queues.transfer_queue.assert_not_awaited()

    async def test_add_to_queue_preserved_through_disambiguation(self) -> None:
        """
        Ambiguous "добавь Iron Maiden" → disambiguation → user picks → ADD survives.

        Without this fix, the disambiguation flow rebuilt ParsedCommand
        from `pending_command` without `enqueue_option`, so the replay
        would hit play_media() without `option` (default REPLACE)
        instead of `QueueOption.ADD`.
        """
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня большая"),
                MockPlayer(player_id="p2", name="Кухня маленькая"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        # Turn 1: ambiguous "добавь Iron Maiden на кухне" → disambig prompt.
        body1 = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "добавь Iron Maiden на кухне"},
        }
        resp1 = await handler._handle_webhook(_build_request(body1))
        body_out1 = _response_body(resp1)
        # Pending command must carry enqueue_option across the prompt.
        assert body_out1["session_state"]["pending_command"]["enqueue_option"] == "add"
        mass.player_queues.play_media.assert_not_awaited()
        # Turn 2: ordinal "первая" → replay pending → play_media with ADD option.
        body2 = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "первая"},
            "state": {"session": body_out1["session_state"]},
        }
        await handler._handle_webhook(_build_request(body2))
        await asyncio.sleep(0)
        mass.player_queues.play_media.assert_awaited_once()
        assert mass.player_queues.play_media.call_args.kwargs["option"] == QueueOption.ADD

    async def test_add_to_queue_uses_queue_option_add(self) -> None:
        """'добавь Metallica на кухне' → play_media(option=QueueOption.ADD)."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "добавь Metallica на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        mass.player_queues.play_media.assert_awaited_once()
        call_kwargs = mass.player_queues.play_media.call_args.kwargs
        assert call_kwargs["queue_id"] == "p1"
        assert call_kwargs["option"] == QueueOption.ADD
        # radio_mode forced off for add-to-queue.
        assert call_kwargs["radio_mode"] is False
        body_out = _response_body(resp)
        assert "Добавил" in body_out["response"]["text"]
        assert "в очередь" in body_out["response"]["text"]


# ---------------------------------------------------------------------------
# Disambiguation (P0.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDisambiguation:
    """End-to-end tests for the disambiguation prompt + pending-command replay."""

    async def test_multiple_matches_returns_disambiguation_prompt(self) -> None:
        """Two candidates on a screened surface → response carries buttons + pending_command."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня большая"),
                MockPlayer(player_id="p2", name="Кухня маленькая"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "meta": {"interfaces": {"screen": {}}},
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 200
        body_out = _response_body(resp)
        assert body_out["response"]["end_session"] is False
        assert "buttons" in body_out["response"]
        button_titles = {b["title"] for b in body_out["response"]["buttons"]}
        assert button_titles == {"Кухня большая", "Кухня маленькая"}
        # pending_command is saved with the original play intent + the
        # ordered candidate IDs for voice ordinal resolution.
        pending = body_out["session_state"]["pending_command"]
        assert pending["kind"] == "search"
        assert pending["query"] == "metallica"
        assert pending["radio_mode"] is True
        assert pending["candidate_ids"] == ["p1", "p2"]
        # Nothing is played yet.
        mass.player_queues.play_media.assert_not_awaited()

    async def test_disambiguation_voice_only_omits_buttons(self) -> None:
        """Voice-only surface (no meta.interfaces.screen) → prompt without buttons."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня большая"),
                MockPlayer(player_id="p2", name="Кухня маленькая"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            # No meta.interfaces — defaults to voice-only (Yandex Mini etc.)
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 200
        body_out = _response_body(resp)
        assert body_out["response"]["end_session"] is False
        # Voice prompt with ordinals is still present, just without buttons.
        assert "buttons" not in body_out["response"]
        assert "первая" in body_out["response"]["text"].lower()
        # Pending command still saved for voice-ordinal resolution.
        pending = body_out["session_state"]["pending_command"]
        assert pending["candidate_ids"] == ["p1", "p2"]

    async def test_button_press_resolves_pending(self) -> None:
        """ButtonPressed payload.player_id triggers a play of the saved pending_command."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня большая"),
                MockPlayer(player_id="p2", name="Кухня маленькая"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "type": "ButtonPressed",
                "command": "Кухня большая",
                "payload": {"player_id": "p1"},
            },
            "state": {
                "session": {
                    "pending_command": {"kind": "search", "query": "metallica", "radio_mode": True},
                },
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        mass.player_queues.play_media.assert_awaited_once()
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p1"
        # pending_command is cleared from the response state.
        body_out = _response_body(resp)
        assert "pending_command" not in body_out["session_state"]
        assert body_out["session_state"]["last_player_id"] == "p1"

    async def test_slot_elicit_with_hint_persists_player(self) -> None:
        """
        'включи на кухне' (player set, no query) elicits + saves hinted player.

        Previously fell through to "Не нашёл такую музыку: ." — the user
        clearly wants something, just didn't name it. Now elicits and
        plays the follow-up on the hinted player without re-stating it.
        """
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        # Turn 1: "включи на кухне" — no query, hint=кухне
        body1 = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи на кухне"},
        }
        resp1 = await handler._handle_webhook(_build_request(body1))
        body_out1 = _response_body(resp1)
        # Slot-elicit response with hinted player saved.
        assert "Что включить" in body_out1["response"]["text"]
        assert body_out1["session_state"]["awaiting_query"] is True
        assert body_out1["session_state"]["awaiting_player_id"] == "p1"
        assert body_out1["application_state"]["awaiting_player_id"] == "p1"
        mass.player_queues.play_media.assert_not_awaited()

        # Turn 2: "Metallica" — should play on p1 (the saved hint)
        body2 = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "Metallica"},
            "state": {
                "session": {
                    "awaiting_query": True,
                    "awaiting_player_id": "p1",
                },
            },
        }
        await handler._handle_webhook(_build_request(body2))
        await asyncio.sleep(0)
        mass.player_queues.play_media.assert_awaited_once()
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p1"

    async def test_slot_elicit_when_query_empty(self) -> None:
        """Bare verb (empty query) → 'Что включить?' + awaiting_query=True."""
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 200
        body_out = _response_body(resp)
        assert body_out["response"]["end_session"] is False
        assert "Что включить" in body_out["response"]["text"]
        assert body_out["session_state"]["awaiting_query"] is True
        # Nothing played.
        mass.player_queues.play_media.assert_not_awaited()

    async def test_followup_with_awaiting_query_resolves(self) -> None:
        """Next utterance after slot-elicit is treated as the play query."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "Metallica"},
            "state": {"session": {"awaiting_query": True}},
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        mass.player_queues.play_media.assert_awaited_once()
        body_out = _response_body(resp)
        # awaiting_query is cleared on success.
        assert "awaiting_query" not in body_out["session_state"]

    async def test_control_during_awaiting_query_dispatches_control(self) -> None:
        """
        Slot-elicit was active, but the user pivots to a control phrase.

        "Включи." → "Что включить?" (awaiting_query=True). Then the user
        says "пауза на кухне" — this must dispatch a control command, not
        get prefixed with "включи " and turned into a search query.
        """
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        mass.player_queues.pause = AsyncMock()
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "command": "пауза на кухне",
                "nlu": {"intents": {"control.pause": {}}},
            },
            "state": {"session": {"awaiting_query": True}},
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        mass.player_queues.pause.assert_awaited_once_with("p1")
        # awaiting_query must be cleared on successful control dispatch.
        body_out = _response_body(resp)
        assert "awaiting_query" not in body_out["session_state"]
        # play_media not called — this was a control, not a play.
        mass.player_queues.play_media.assert_not_awaited()

    async def test_followup_full_play_command_does_not_double_prefix(self) -> None:
        """Follow-up like 'включи Yesterday' is parsed as-is, not double-prefixed."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Yesterday"},
            "state": {"session": {"awaiting_query": True}},
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        mass.player_queues.play_media.assert_awaited_once()
        # The search call must use "yesterday" (after parser strips "включи"),
        # not "включи yesterday".
        search_query = mass.music.search.call_args.kwargs["search_query"]
        assert search_query == "yesterday"

    async def test_play_no_hint_no_default_offers_disambiguation(self) -> None:
        """
        Play branch: no hint + no default + 2+ players → disambiguation prompt.

        Without this, the user would see "Не нашёл колонку «(не указано)»".
        """
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "meta": {"interfaces": {"screen": {}}},
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        assert body_out["response"]["end_session"] is False
        assert "buttons" in body_out["response"]
        button_titles = {b["title"] for b in body_out["response"]["buttons"]}
        assert button_titles == {"Кухня", "Спальня"}
        # pending_command saved with the original play intent + candidate_ids.
        # Order is significant — used as the index space for voice ordinal
        # resolution ("первая" → candidate_ids[0]).
        pending = body_out["session_state"]["pending_command"]
        assert pending["kind"] == "search"
        assert pending["query"] == "metallica"
        assert pending["radio_mode"] is True
        assert pending["candidate_ids"] == ["p1", "p2"]
        mass.player_queues.play_media.assert_not_awaited()

    async def test_button_payload_validated_against_exposed_set(self) -> None:
        """
        ButtonPressed with a payload targeting a non-exposed player is rejected.

        Defence-in-depth: even though Yandex echoes our own payload back,
        we never trust the player_id without re-checking it's currently
        exposed/enabled/available.
        """
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "type": "ButtonPressed",
                "command": "Гостиная",
                "payload": {"player_id": "p99-not-in-set"},
            },
            "state": {
                "session": {
                    "pending_command": {"kind": "search", "query": "metallica", "radio_mode": True},
                },
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        # play_media must NOT be awaited — invalid payload should not play.
        mass.player_queues.play_media.assert_not_awaited()
        # Status is still 200; the handler falls through, but no playback.
        assert resp.status == 200

    async def test_disambiguation_clears_awaiting_query(self) -> None:
        """
        Slot-elicit → multi-match → disambiguation prompt drops awaiting_query.

        Without this, the next user utterance ("Кухня маленькая") would get
        auto-prefixed with "включи " by the awaiting-query branch and miss
        the pending-command resolver.
        """
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня большая"),
                MockPlayer(player_id="p2", name="Кухня маленькая"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        # Simulate the awaiting_query → ambiguous-resolution turn.
        body = {
            "meta": {"interfaces": {"screen": {}}},
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "Metallica на кухне"},
            "state": {"session": {"awaiting_query": True}},
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        # Disambiguation prompt is returned (multi-match).
        assert body_out["response"]["end_session"] is False
        assert "buttons" in body_out["response"]
        # And the response carries pending_command but NOT awaiting_query.
        assert "pending_command" in body_out["session_state"]
        assert "awaiting_query" not in body_out["session_state"]

    async def test_voice_ordinal_resolves_pending(self) -> None:
        """User answers disambiguation with 'первая' → first candidate is picked."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "первая"},
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    },
                },
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        mass.player_queues.play_media.assert_awaited_once()
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p1"

    async def test_voice_ordinal_second_candidate(self) -> None:
        """'вторая' picks the second candidate from candidate_ids."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "вторая"},
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    },
                },
            },
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p2"

    async def test_ordinal_out_of_range_reasks_does_not_fall_through(self) -> None:
        """
        User says 'третья' when only 2 candidates → re-ask, don't search for 'третья'.

        Without this, the ordinal would be parsed but skip the lookup,
        the free-text path would parse the utterance as a search query,
        and a default-player resolution might play "третья" on some
        random player.
        """
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "meta": {"interfaces": {"screen": {}}},
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "третья"},
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    },
                },
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        body_out = _response_body(resp)
        # Disambiguation re-asked, not played.
        assert body_out["response"]["end_session"] is False
        assert "buttons" in body_out["response"]
        # pending_command still set (with same candidate set).
        assert body_out["session_state"]["pending_command"]["candidate_ids"] == ["p1", "p2"]
        mass.player_queues.play_media.assert_not_awaited()

    async def test_ordinal_targets_unexposed_player_reasks(self) -> None:
        """User picks a valid ordinal but the indexed player has been removed → re-ask."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        # Only p1 exposed now — p2 is gone since the buttons were sent.
        mass = _make_mass(
            [MockPlayer(player_id="p1", name="Кухня")],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "вторая"},
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    },
                },
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        body_out = _response_body(resp)
        # Re-asked with the remaining exposed candidate (p1).
        assert body_out["response"]["end_session"] is False
        assert body_out["session_state"]["pending_command"]["candidate_ids"] == ["p1"]
        mass.player_queues.play_media.assert_not_awaited()

    async def test_in_process_cache_recovers_when_yandex_drops_state(self) -> None:
        """
        Reproduce the screenless-Station bug from the dev console transcript.

        Yandex doesn't echo `state.session` OR `state.application` back
        on the next turn, despite us setting both on the previous
        response. The in-process state cache (keyed by user.user_id /
        application_id) is the third-tier fallback that recovers.
        """
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Проигрыватель"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        sess_common = {
            "skill_id": "skill-uuid-1",
            "user": {"user_id": "yandex-user-1"},
            "application": {"application_id": "yandex-app-1"},
        }
        # Turn 1: disambig fires + saves cache entry.
        await handler._handle_webhook(
            _build_request(
                {
                    "session": {**sess_common, "session_id": "s1", "new": False},
                    "request": {"command": "включи джаз"},
                }
            )
        )
        await asyncio.sleep(0)
        cached = handler._cache_get(
            {
                "user": {"user_id": "yandex-user-1"},
                "application": {"application_id": "yandex-app-1"},
            }
        )
        assert cached["pending_command"]["query"] == "джаз"
        # Turn 2: NO `state` field in request — mimics dev-console emulator.
        await handler._handle_webhook(
            _build_request(
                {
                    "session": {**sess_common, "session_id": "s1", "new": False},
                    "request": {"command": "кухня"},
                }
            )
        )
        await asyncio.sleep(0)
        # Played the pending command (джаз) on p1 (Кухня).
        mass.player_queues.play_media.assert_awaited_once()
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p1"

    async def test_in_process_cache_resolves_via_ordinal(self) -> None:
        """Same as above, but turn 2 says '2' (ordinal) — also resolves via cache."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Проигрыватель"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        sess_common = {
            "skill_id": "skill-uuid-1",
            "user": {"user_id": "yandex-user-1"},
            "application": {"application_id": "yandex-app-1"},
        }
        await handler._handle_webhook(
            _build_request(
                {
                    "session": {**sess_common, "session_id": "s1", "new": False},
                    "request": {"command": "включи джаз"},
                }
            )
        )
        await asyncio.sleep(0)
        await handler._handle_webhook(
            _build_request(
                {
                    "session": {**sess_common, "session_id": "s1", "new": False},
                    "request": {"command": "2"},
                }
            )
        )
        await asyncio.sleep(0)
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p2"

    async def test_in_process_cache_ttl_expiry(self) -> None:
        """Cached state expires after `_STATE_CACHE_TTL_SEC`; later calls don't see it."""
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        # Inject an expired entry.
        handler._state_cache["user:u1"] = (
            {"pending_command": {"kind": "search", "query": "old"}},
            time.monotonic() - _STATE_CACHE_TTL_SEC - 1,
        )
        assert handler._cache_get({"user": {"user_id": "u1"}}) == {}
        assert "user:u1" not in handler._state_cache

    async def test_pending_command_falls_back_to_application_state(self) -> None:
        """
        Yandex didn't echo `state.session` but kept `state.application` — still resolves.

        Reproduces the screenless-Station bug where the second turn of
        a disambiguation arrives without the `pending_command` we put in
        `state.session`. The same record is mirrored in `state.application`
        so the handler can recover.
        """
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Проигрыватель"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "Проигрыватель"},
            "state": {
                # state.session is empty — Yandex didn't echo it back.
                "application": {
                    "pending_command": {
                        "kind": "search",
                        "query": "джаз",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    },
                },
            },
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        mass.player_queues.play_media.assert_awaited_once()
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p2"

    async def test_disambiguation_writes_pending_to_application_state(self) -> None:
        """
        The disambiguation prompt mirrors `pending_command` to application_state.

        Without this, devices that drop `state.session` between turns can
        never complete the disambiguation flow.
        """
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Проигрыватель"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "meta": {"interfaces": {"screen": {}}},
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи джаз"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        # Disambiguation triggered.
        assert "buttons" in body_out["response"]
        # Pending mirrored in BOTH session_state and application_state.
        assert body_out["session_state"]["pending_command"]["candidate_ids"] == ["p1", "p2"]
        assert body_out["application_state"]["pending_command"]["candidate_ids"] == ["p1", "p2"]

    async def test_voice_ordinal_with_filler(self) -> None:
        """
        Filler-padded ordinal answers ('выбираю первую', 'хочу вторую') resolve.

        On smart speakers users naturally pad voice replies with filler;
        the strict-anchor regex from v1.8.2 missed these.
        """
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "выбираю первую"},
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    },
                },
            },
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p1"

    async def test_voice_accusative_adjective(self) -> None:
        """
        Accusative-case answer 'большую' resolves to 'Кухня большая'.

        Caught by the new `ую` suffix in `_INFLECTION_SUFFIXES`.
        """
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня большая"),
                MockPlayer(player_id="p2", name="Кухня маленькая"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "большую"},
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    },
                },
            },
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p1"

    async def test_voice_accusative_noun(self) -> None:
        """Accusative noun 'Кухню' resolves to 'Кухня' via the new `ю` suffix."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "Кухню"},
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    },
                },
            },
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p1"

    async def test_voice_ordinal_digit(self) -> None:
        """A bare digit ('2') also works as an ordinal."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "2"},
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    },
                },
            },
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p2"

    async def test_freetext_narrows_to_candidate_set(self) -> None:
        """
        Free-text answer is matched only against the saved candidate IDs.

        With 3 exposed players (Кухня большая, Кухня маленькая, Гостиная)
        and a saved candidate set covering only the two kitchens, saying
        'большая' must pick "Кухня большая" — even though 'большая'
        could ambiguously refer to several players in a larger set.
        """
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня большая"),
                MockPlayer(player_id="p2", name="Кухня маленькая"),
                MockPlayer(player_id="p3", name="Гостиная большая"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "большая"},
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    },
                },
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        # Must pick p1 (Кухня большая, in candidate set) — not p3
        # (also matches "большая" but excluded from candidate_ids).
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p1"

    async def test_freetext_followup_resolves_pending(self) -> None:
        """User says 'на кухне маленькой' after the disambiguation question — plays on p2."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня большая"),
                MockPlayer(player_id="p2", name="Кухня маленькая"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "на кухне маленькой"},
            "state": {
                "session": {
                    "pending_command": {"kind": "search", "query": "metallica", "radio_mode": True},
                },
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        mass.player_queues.play_media.assert_awaited_once()
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p2"
