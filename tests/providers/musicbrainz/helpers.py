"""Helpers for tests that have ListenBrainz answer from a recorded answer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, Mock

from aiohttp import ClientResponseError

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.musicbrainz.listenbrainz import LB_BASE_URL, ListenBrainzClient

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

RADIOHEAD_MBID = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
TOP_RECORDINGS_URL = f"{LB_BASE_URL}/popularity/top-recordings-for-artist/{RADIOHEAD_MBID}"
# the ranking of Radiohead's recordings ListenBrainz answered on 2026-11-14, trimmed to a few
# of its entries (in their order) and fields
_RECORDED_ANSWER = Path(__file__).parent / "fixtures" / "listenbrainz_top_recordings.json"


def recorded_answer() -> list[dict[str, Any]]:
    """Return the recorded ListenBrainz ranking of Radiohead's recordings."""
    answer: list[dict[str, Any]] = json.loads(_RECORDED_ANSWER.read_text())
    return answer


def own_throttler() -> ThrottlerManager:
    """
    Return a throttler as the client's, without its pacing.

    The client's throttler is shared by every instance, so a cooldown one test arms would
    hold back the next test.
    """
    shared = ListenBrainzClient.throttler
    return ThrottlerManager(
        rate_limit=100,
        period=1,
        retry_attempts=shared.retry_attempts,
        initial_backoff=shared.initial_backoff,
    )


def answer_listenbrainz(
    mass: MusicAssistant,
    status: int = 200,
    body: Any = None,
    headers: dict[str, str] | None = None,
    error: Exception | None = None,
) -> MagicMock:
    """
    Have every ListenBrainz request answered with the given response.

    :param mass: The MusicAssistant instance whose HTTP session sends the requests.
    :param status: The HTTP status of the answer.
    :param body: The JSON body of the answer, the recorded answer when None.
    :param headers: The headers of the answer.
    :param error: Raised by every request instead of an answer.
    :return: The mocked get of the HTTP session, to count the requests with.
    """
    response = MagicMock()
    response.status = status
    response.headers = headers or {}
    response.json = AsyncMock(return_value=recorded_answer() if body is None else body)
    response.raise_for_status = Mock(
        side_effect=(
            ClientResponseError(Mock(), (), status=status, message="error")
            if status >= 400
            else None
        )
    )
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    get = MagicMock(return_value=context, side_effect=error)
    mass._http_session = MagicMock(get=get)
    return get
