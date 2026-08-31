"""Tests for the Overcast app based (QR) login flow."""

from __future__ import annotations

from http.cookies import Morsel
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from music_assistant.models.setup_flow import AbortFlow
from music_assistant.providers.overcast.constants import (
    QR_POLL_MAX_INTERVAL,
    QR_TOKEN_PATTERN,
)
from music_assistant.providers.overcast.setup_flow import (
    _claim_session_cookie,
    _mint_login_token,
    _poll_interval,
    _poll_until_approved,
    _qr_image,
)

# what the verify endpoint answers while the code is still waiting to be approved
UNAPPROVED = "0"

# shaped like the real login page, whose attribute order and spacing we do not control
LOGIN_PAGE = (
    '<div id="qrcode" data-token="RJW9kozxtNtJNB9O1KAX" data-then="podcasts"></div>\n'
    '<a id="qrdirectlink" data-target-url="overcast:///auth?t=RJW9kozxtNtJNB9O1KAX&l=browser">'
    "Authenticate with Overcast app</a>"
)


class _FakeResponse:
    def __init__(self, body: str = "") -> None:
        self._body = body

    async def text(self) -> str:
        return self._body

    def raise_for_status(self) -> None:
        """Accept the response, as these fakes only stand in for successful requests."""


class _FakeRequestContext:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSession:
    """Fake aiohttp session returning canned bodies in order."""

    def __init__(self, bodies: list[str], cookies: dict[str, str] | None = None) -> None:
        self._bodies = bodies
        self.requests: list[tuple[str, str]] = []
        self.cookie_jar = _cookie_jar(cookies or {})

    def get(self, url: Any, **kwargs: Any) -> _FakeRequestContext:
        self.requests.append(("GET", str(url)))
        return _FakeRequestContext(_FakeResponse(self._bodies.pop(0)))

    def post(self, url: Any, **kwargs: Any) -> _FakeRequestContext:
        self.requests.append(("POST", str(url)))
        return _FakeRequestContext(_FakeResponse(self._bodies.pop(0)))


def _cookie_jar(cookies: dict[str, str]) -> list[Morsel[str]]:
    """Build the cookie jar shape the flow iterates over."""
    jar: list[Morsel[str]] = []
    for key, value in cookies.items():
        morsel: Morsel[str] = Morsel()
        morsel.set(key, value, value)
        jar.append(morsel)
    return jar


def test_login_token_is_read_from_the_page() -> None:
    """The token and follow-up page are taken from the login page markup."""
    match = QR_TOKEN_PATTERN.search(LOGIN_PAGE)
    assert match is not None
    assert match["token"] == "RJW9kozxtNtJNB9O1KAX"
    assert match["then"] == "podcasts"


async def test_minting_a_token_reads_the_login_page() -> None:
    """Minting a token fetches the login page and returns what it carries."""
    session = _FakeSession([LOGIN_PAGE])

    token, target = await _mint_login_token(session)  # type: ignore[arg-type]

    assert (token, target) == ("RJW9kozxtNtJNB9O1KAX", "podcasts")
    assert session.requests == [("GET", "https://overcast.fm/login")]


async def test_a_page_without_a_code_aborts() -> None:
    """A login page offering no code to scan cannot start the flow."""
    with pytest.raises(AbortFlow):
        await _mint_login_token(_FakeSession(["<html>no code here</html>"]))  # type: ignore[arg-type]


async def test_approval_returns_the_session_cookie() -> None:
    """An approved token leads to the session cookie Overcast leaves behind."""
    session = _FakeSession(["/podcasts", ""], cookies={"o": "abc123"})

    cookie = await _poll_until_approved(session, "tok", "podcasts")  # type: ignore[arg-type]

    assert cookie == "abc123"
    assert session.requests == [
        ("POST", "https://overcast.fm/main/login_qr_verify"),
        ("GET", "https://overcast.fm/podcasts"),
    ]


async def test_polling_continues_while_the_token_is_unapproved() -> None:
    """A placeholder answer means the code has not been approved yet, so keep asking."""
    session = _FakeSession([UNAPPROVED, UNAPPROVED, "/podcasts", ""], cookies={"o": "abc123"})

    with patch("music_assistant.providers.overcast.setup_flow.asyncio.sleep", AsyncMock()):
        cookie = await _poll_until_approved(session, "tok", "podcasts")  # type: ignore[arg-type]

    assert cookie == "abc123"
    assert len([req for req in session.requests if req[0] == "POST"]) == 3


async def test_an_unapproved_code_is_not_taken_for_a_login() -> None:
    """The placeholder answer must not be mistaken for the page to continue to."""
    session = _FakeSession([UNAPPROVED, "/podcasts", ""], cookies={"o": "abc123"})

    with patch("music_assistant.providers.overcast.setup_flow.asyncio.sleep", AsyncMock()):
        cookie = await _poll_until_approved(session, "tok", "podcasts")  # type: ignore[arg-type]

    assert cookie == "abc123"
    assert session.requests[0] == ("POST", "https://overcast.fm/main/login_qr_verify")
    assert session.requests[1] == ("POST", "https://overcast.fm/main/login_qr_verify")


async def test_a_login_without_a_session_aborts() -> None:
    """An approval that leaves no session cookie is not a usable login."""
    session = _FakeSession(["/podcasts", ""], cookies={"other": "value"})

    with pytest.raises(AbortFlow):
        await _claim_session_cookie(session, "/podcasts")  # type: ignore[arg-type]


def test_polling_eases_off() -> None:
    """Repeated checks back off the way the Overcast login page does."""
    assert _poll_interval(1) == 1.0
    assert _poll_interval(60) == 1.0
    assert _poll_interval(61) == 5.0
    assert _poll_interval(181) == 10.0
    assert _poll_interval(281) == QR_POLL_MAX_INTERVAL


def test_the_code_carries_the_app_link() -> None:
    """The code is rendered as an image the setup step can show inline."""
    image = _qr_image("overcast:///auth?t=tok&l=browser")

    assert image.startswith("data:image/svg+xml;")
