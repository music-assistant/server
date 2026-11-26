"""Tests for the JSON-RPC API command endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import StreamReader, web
from aiohttp.test_utils import make_mocked_request
from mashumaro import DataClassDictMixin
from music_assistant_models.auth import Scope, User, UserRole
from music_assistant_models.errors import (
    AudioError,
    AuthenticationFailed,
    AuthenticationRequired,
    InsufficientPermissions,
    InvalidCommand,
    InvalidDataError,
    InvalidToken,
    MediaNotFoundError,
    MusicAssistantError,
    PlayerUnavailableError,
    ProviderPermissionDenied,
    ProviderUnavailableError,
    RateLimited,
    ResourceBusyError,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
    UnsupportedFeaturedException,
    UserNotFoundError,
)

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.controllers.translations import SOURCE_FILE, TranslationController
from music_assistant.helpers.api import APICommandHandler
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.models.music_provider import ProviderStreamLimitError

if TYPE_CHECKING:
    from music_assistant.controllers.webserver.controller import WebserverController

COMMAND = "test/command"


@pytest.fixture
async def api_server(webserver: WebserverController) -> WebserverController:
    """
    Return the webserver with a signed-up user and the real translations of the server.

    :param webserver: The webserver controller.
    """
    translations = TranslationController(webserver.mass)
    await translations.setup(MagicMock())
    webserver.mass.translations = translations
    webserver.auth = MagicMock(has_users=True)
    return webserver


async def test_unavailable_player_returns_not_found(api_server: WebserverController) -> None:
    """Test a command for an unavailable player is answered with 404 instead of a server error."""

    async def raise_unavailable() -> None:
        raise PlayerUnavailableError("Player p1 is not available")

    api_server.mass.command_handlers["test/raise"] = APICommandHandler.parse(
        "test/raise", raise_unavailable
    )
    request = MagicMock(
        can_read_body=True,
        remote="127.0.0.1",
        headers={},
        read=AsyncMock(return_value=b'{"message_id": "1", "command": "test/raise", "args": {}}'),
    )

    with patch.object(api_server, "_authenticate_api_command", AsyncMock(return_value=None)):
        response = await api_server._handle_jsonrpc_api_command(request)

    assert response.status == 404
    assert _body(response)["details"] == "The player is not available."


async def test_verbose_log_hides_secrets_of_commands(
    webserver: WebserverController, caplog: pytest.LogCaptureFixture
) -> None:
    """The verbose log shows a received command without its secrets."""

    async def login() -> bool:
        return True

    webserver.auth = MagicMock(has_users=True)
    webserver.mass.command_handlers["test/login"] = APICommandHandler.parse(
        "test/login", login, authenticated=False
    )
    request = MagicMock(
        can_read_body=True,
        remote="127.0.0.1",
        headers={},
        read=AsyncMock(
            return_value=b'{"message_id": "1", "command": "test/login", '
            b'"args": {"username": "someone", "password": "made-up-password"}}'
        ),
    )

    with (
        caplog.at_level(VERBOSE_LOG_LEVEL, logger=webserver.logger.name),
        patch.object(webserver.mass.translations, "ensure_locale_loaded", AsyncMock(), create=True),
    ):
        response = await webserver._handle_jsonrpc_api_command(request)

    assert response.status == 200
    assert "made-up-password" not in caplog.text
    assert '"command":"test/login"' in caplog.text
    assert '"username":"someone","password":"<redacted>"' in caplog.text


async def test_invalid_json_is_rejected_without_logging_it(
    webserver: WebserverController, caplog: pytest.LogCaptureFixture
) -> None:
    """A request with invalid JSON is rejected without logging what it sent."""
    webserver.auth = MagicMock(has_users=True)
    request = MagicMock(
        can_read_body=True,
        remote="127.0.0.1",
        read=AsyncMock(return_value=b'{"password": "made-up-password"'),
    )

    with caplog.at_level(logging.ERROR, logger=webserver.logger.name):
        response = await webserver._handle_jsonrpc_api_command(request)

    assert response.status == 400
    assert response.text == "Invalid JSON"
    assert "made-up-password" not in caplog.text


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (InsufficientPermissions("Admins only"), 403),
        (MediaNotFoundError("No such track"), 404),
        (PlayerUnavailableError("No such player"), 404),
        (ResourceBusyError("Busy"), 409),
        (InvalidDataError("Bad value"), 400),
        (UserNotFoundError("No such user", translation_args=["someone"]), 400),
        (UnsupportedFeaturedException("Not supported"), 400),
        # credentials of a provider account, not those of the caller
        (InvalidToken("Invalid provider token"), 400),
        (AuthenticationFailed("Provider login failed"), 400),
        (ProviderPermissionDenied("Provider refused"), 400),
        (RateLimited("Slow down"), 429),
        (RetriesExhausted("Gave up"), 502),
        (AudioError("Decoder stopped"), 502),
        (ResourceTemporarilyUnavailable("Try later"), 503),
        (ProviderUnavailableError("Provider is not loaded"), 503),
    ],
)
async def test_an_error_of_a_command_answers_its_status_and_error(
    api_server: WebserverController, error: MusicAssistantError, status: int
) -> None:
    """
    Test that an error a command raises answers its status with the error the websocket sends.

    :param api_server: The webserver controller.
    :param error: The error the command raises.
    :param status: The HTTP status the route should answer.
    """
    _register(api_server, _raising(error))

    response = await api_server._handle_jsonrpc_api_command(_request())

    assert response.status == status
    assert "Retry-After" not in response.headers
    body = _body(response)
    assert body["message_id"] == "1"
    assert body["error_code"] == error.error_code
    assert body["details"] == _english(error)


async def test_a_full_stream_slot_answers_409(api_server: WebserverController) -> None:
    """
    Test that a provider without a free stream slot answers a conflict, not a failure.

    :param api_server: The webserver controller.
    """
    provider = MagicMock(max_concurrent_streams=1, instance_id="prov1")
    provider.name = "Provider"
    _register(api_server, _raising(ProviderStreamLimitError(provider, None)))

    response = await api_server._handle_jsonrpc_api_command(_request())

    assert response.status == 409
    assert _body(response)["details"].startswith("Provider has reached its limit of 1")


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (RateLimited("Slow down", backoff_time=30), 429),
        (ResourceTemporarilyUnavailable("Try later", backoff_time=5), 503),
    ],
)
async def test_a_known_wait_answers_retry_after(
    api_server: WebserverController, error: ResourceTemporarilyUnavailable, status: int
) -> None:
    """
    Test that an error that knows how long to wait sends it in the Retry-After header.

    :param api_server: The webserver controller.
    :param error: The error the command raises.
    :param status: The HTTP status the route should answer.
    """
    _register(api_server, _raising(error))

    response = await api_server._handle_jsonrpc_api_command(_request())

    assert response.status == status
    assert response.headers["Retry-After"] == str(error.backoff_time)


async def test_an_error_reads_in_the_language_of_the_client(
    api_server: WebserverController,
) -> None:
    """
    Test that the message of an error is localized for the locale of the request.

    :param api_server: The webserver controller.
    """
    _register(api_server, _raising(MediaNotFoundError("No such track")))

    response = await api_server._handle_jsonrpc_api_command(
        _request(headers={"Accept-Language": "nl-NL"})
    )

    assert response.status == 404
    assert _body(response)["details"] == "Het opgevraagde mediabestand kon niet worden gevonden."


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, KeyError])
async def test_an_unexpected_failure_answers_500_and_is_logged(
    api_server: WebserverController,
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
) -> None:
    """
    Test that a failure the server did not raise on purpose answers 500 without its details.

    :param api_server: The webserver controller.
    :param caplog: The log capture fixture.
    :param error_type: The error the command raises, including the ones the argument parser raises.
    """
    _register(api_server, _raising(error_type("secret internals")))

    with caplog.at_level(logging.ERROR):
        response = await api_server._handle_jsonrpc_api_command(_request())

    assert response.status == 500
    body = _body(response)
    assert body["error_code"] == 999
    assert body["details"] == "Internal server error"
    assert "secret internals" in caplog.text


@pytest.mark.parametrize(
    ("args", "details"),
    [
        ({"count": "many"}, "Value of type <class 'str'> is invalid for count"),
        ({}, "`count` of type `<class 'int'>` is required."),
    ],
)
async def test_invalid_arguments_answer_400_with_the_parser_text(
    api_server: WebserverController, args: dict[str, Any], details: str
) -> None:
    """
    Test that invalid arguments answer 400 with what the parser found wrong, as the websocket does.

    :param api_server: The webserver controller.
    :param args: The arguments of the call.
    :param details: The start of the message the caller should get.
    """

    async def _command(count: int) -> int:
        return count

    _register(api_server, _command)

    response = await api_server._handle_jsonrpc_api_command(_request(args))

    assert response.status == 400
    body = _body(response)
    assert body["error_code"] == InvalidDataError.error_code
    assert body["details"].startswith(details)


async def test_a_dataclass_argument_with_a_missing_field_answers_400(
    api_server: WebserverController,
) -> None:
    """
    Test that a dataclass argument without one of its required fields answers 400.

    :param api_server: The webserver controller.
    """

    async def _command(thing: _Thing) -> str:
        return thing.name

    _register(api_server, _command)

    response = await api_server._handle_jsonrpc_api_command(_request({"thing": {"name": "x"}}))

    assert response.status == 400
    assert 'Field "thing_id"' in _body(response)["details"]


async def test_an_unknown_command_answers_400_with_its_error(
    api_server: WebserverController,
) -> None:
    """
    Test that an unknown command answers 400 with the invalid command error.

    :param api_server: The webserver controller.
    """
    response = await api_server._handle_jsonrpc_api_command(_request())

    assert response.status == 400
    body = _body(response)
    assert body["error_code"] == InvalidCommand.error_code
    assert body["details"] == _english(InvalidCommand())


async def test_a_missing_scope_answers_403_with_its_error(
    api_server: WebserverController,
) -> None:
    """
    Test that a user without the scope of a command gets a 403 with the permissions error.

    :param api_server: The webserver controller.
    """
    _register(api_server, _returning("ok"), required_scope=Scope.ALL)
    user = User(user_id="u1", username="someone", role=UserRole.USER)

    with patch(
        "music_assistant.controllers.webserver.controller.get_authenticated_user",
        AsyncMock(return_value=user),
    ):
        response = await api_server._handle_jsonrpc_api_command(_request())

    assert response.status == 403
    body = _body(response)
    assert body["error_code"] == InsufficientPermissions.error_code
    assert body["details"] == _english(InsufficientPermissions())


async def test_a_missing_login_answers_401_with_its_error(
    api_server: WebserverController,
) -> None:
    """
    Test that a call without a valid token gets a 401 with the authentication error.

    :param api_server: The webserver controller.
    """
    _register(api_server, _returning("ok"), authenticated=True)

    with patch(
        "music_assistant.controllers.webserver.controller.get_authenticated_user",
        AsyncMock(return_value=None),
    ):
        response = await api_server._handle_jsonrpc_api_command(_request())

    assert response.status == 401
    assert response.headers["WWW-Authenticate"] == 'Bearer realm="Music Assistant"'
    body = _body(response)
    assert body["error_code"] == AuthenticationRequired.error_code
    assert body["details"] == _english(AuthenticationRequired())


async def test_a_successful_command_answers_its_result(api_server: WebserverController) -> None:
    """
    Test that a command that succeeds answers 200 with its result.

    :param api_server: The webserver controller.
    """
    _register(api_server, _returning({"answer": 42}))

    response = await api_server._handle_jsonrpc_api_command(_request())

    assert response.status == 200
    assert _body(response) == {"answer": 42}


@dataclass
class _Thing(DataClassDictMixin):
    """An argument type the API parses from a dict."""

    thing_id: str
    name: str


def _english(error: MusicAssistantError) -> str:
    """
    Return the English text the server sends for an error, from its shipped translation source.

    :param error: The error to look up.
    """
    with open(SOURCE_FILE, encoding="utf-8") as source:
        text: str = json.load(source)[f"common.errors.{error.translation_key}"]
    return text.format(*error.translation_args)


def _register(
    webserver: WebserverController,
    func: Callable[..., Any],
    authenticated: bool = False,
    required_scope: Scope | None = None,
) -> None:
    """
    Register a command on the server of the webserver.

    :param webserver: The webserver controller.
    :param func: The function that handles the command.
    :param authenticated: Whether the command requires a signed in user.
    :param required_scope: The scope the command requires.
    """
    webserver.mass.command_handlers[COMMAND] = APICommandHandler.parse(
        COMMAND, func, authenticated=authenticated, required_scope=required_scope
    )


def _raising(error: Exception) -> Callable[[], Any]:
    """
    Return a command function that raises the given error.

    :param error: The error to raise.
    """

    async def _command() -> None:
        raise error

    return _command


def _returning(result: Any) -> Callable[[], Any]:
    """
    Return a command function that returns the given result.

    :param result: The result to return.
    """

    async def _command() -> Any:
        return result

    return _command


def _request(
    args: dict[str, Any] | None = None, headers: dict[str, str] | None = None
) -> web.Request:
    """
    Build a POST request to the API route that calls the test command.

    :param args: The arguments of the command.
    :param headers: Extra headers of the request.
    """
    body = json_dumps({"message_id": "1", "command": COMMAND, "args": args or {}}).encode()
    payload = StreamReader(MagicMock(), 2**16, loop=asyncio.get_running_loop())
    payload.feed_data(body)
    payload.feed_eof()
    return make_mocked_request(
        "POST",
        "/api",
        headers={"Content-Type": "application/json", **(headers or {})},
        app=web.Application(),
        payload=payload,
    )


def _body(response: web.StreamResponse) -> Any:
    """
    Return the decoded JSON body of a response.

    :param response: The response of the route.
    """
    assert isinstance(response, web.Response)
    assert response.content_type == "application/json"
    assert response.body is not None
    return json_loads(bytes(response.body))  # type: ignore[arg-type]
