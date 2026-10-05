"""Unit tests for the Apple Music setup flow helpers."""

from unittest.mock import MagicMock

import pytest
from aiohttp import ClientError

from music_assistant.providers.apple_music.setup_flow import _app_token_accepted


class _FakeRequestCtx:
    """Minimal async context manager mimicking aiohttp's request context."""

    def __init__(self, status: int) -> None:
        self._response = MagicMock(status=status)

    async def __aenter__(self) -> MagicMock:
        return self._response

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _make_mass(status: int) -> MagicMock:
    mass = MagicMock()
    mass.http_session.get = MagicMock(return_value=_FakeRequestCtx(status))
    return mass


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, True),
        (401, False),
        (403, False),
        # a throttled or broken /v1/test says nothing about the token itself
        (429, None),
        (500, None),
        (503, None),
    ],
)
@pytest.mark.asyncio
async def test_app_token_accepted_per_status(status: int, expected: bool | None) -> None:
    """Only an explicit rejection by Apple marks the developer token as invalid."""
    assert await _app_token_accepted(_make_mass(status), "app-token") is expected


@pytest.mark.asyncio
async def test_app_token_accepted_empty_token_is_rejected() -> None:
    """An empty (e.g. not provisioned) token is rejected without calling the API."""
    mass = _make_mass(200)
    assert await _app_token_accepted(mass, "") is False
    mass.http_session.get.assert_not_called()


@pytest.mark.asyncio
async def test_app_token_accepted_network_error_is_inconclusive() -> None:
    """An unreachable API does not make a valid token look invalid."""
    mass = MagicMock()
    mass.http_session.get = MagicMock(side_effect=ClientError("boom"))
    assert await _app_token_accepted(mass, "app-token") is None
