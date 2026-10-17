"""Tests for sending likes and dislikes on Pandora station tracks as thumbs up/down."""

from __future__ import annotations

from typing import Any

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError

from music_assistant.providers.pandora import SUPPORTED_FEATURES
from music_assistant.providers.pandora.constants import ADD_FEEDBACK_ENDPOINT
from music_assistant.providers.pandora.provider import PandoraProvider

from .test_provider import STATION_ID, _provider, _tracks


def _feedback_provider(
    tracks: list[dict[str, Any]] | None = None,
) -> tuple[PandoraProvider, list[dict[str, Any]]]:
    """Build a provider holding one fragment of `tracks`, recording every addFeedback body."""
    tracks = tracks if tracks is not None else _tracks()
    for track in tracks:
        track.setdefault("trackToken", f"token-{track['pandoraId']}")
    provider = _provider(payloads=[tracks])
    sent: list[dict[str, Any]] = []
    inner = provider._api_request

    async def _api(
        method: str, url: str, data: dict[str, Any] | None = None, **kw: Any
    ) -> dict[str, Any]:
        if url == ADD_FEEDBACK_ENDPOINT:
            sent.append(data or {})
            return {"feedbackId": "fb-1"}
        return await inner(method, url, data, **kw)

    provider._api_request = _api  # type: ignore[method-assign, assignment]
    return provider, sent


def test_track_favorites_are_editable() -> None:
    """The provider declares track favorite editing, so likes and dislikes reach it."""
    assert ProviderFeature.FAVORITE_TRACKS_EDIT in SUPPORTED_FEATURES


async def test_like_sends_thumbs_up_with_the_tracks_token() -> None:
    """A like posts the retained fragment's trackToken with isPositive=True."""
    provider, sent = _feedback_provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)  # retains the fragment
    await provider.set_favorite("TR:S1", MediaType.TRACK, True)
    assert sent == [{"trackToken": "token-TR:S1", "isPositive": True}]


async def test_dislike_sends_thumbs_down() -> None:
    """A dislike posts isPositive=False."""
    provider, sent = _feedback_provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider.set_favorite("TR:S2", MediaType.TRACK, False)
    assert sent == [{"trackToken": "token-TR:S2", "isPositive": False}]


@pytest.mark.parametrize(
    ("media_type", "favorite"),
    [(MediaType.TRACK, None), (MediaType.ALBUM, True), (MediaType.ARTIST, False)],
)
async def test_clear_and_non_tracks_send_nothing(
    media_type: MediaType, favorite: bool | None
) -> None:
    """Clearing a rating, or rating anything but a track, makes no call to Pandora."""
    provider, sent = _feedback_provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider.set_favorite("TR:S1", media_type, favorite)
    assert sent == []


async def test_unknown_track_is_refused_without_a_call() -> None:
    """A track no retained fragment holds is refused before any call to Pandora."""
    provider, sent = _feedback_provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    with pytest.raises(MediaNotFoundError):
        await provider.set_favorite("TR:nope", MediaType.TRACK, True)
    assert sent == []


async def test_track_without_token_is_refused() -> None:
    """A retained track that came without a trackToken cannot be rated."""
    tracks = _tracks()
    tracks[0]["trackToken"] = ""
    provider, sent = _feedback_provider(tracks)
    await provider.get_dynamic_radio_tracks(STATION_ID)
    with pytest.raises(InvalidDataError):
        await provider.set_favorite("TR:S0", MediaType.TRACK, True)
    assert sent == []


async def test_feedback_disallowed_by_pandora_is_refused() -> None:
    """A track Pandora marks allowFeedback=False is refused before any call."""
    tracks = _tracks()
    tracks[0]["allowFeedback"] = False
    provider, sent = _feedback_provider(tracks)
    await provider.get_dynamic_radio_tracks(STATION_ID)
    with pytest.raises(InvalidDataError):
        await provider.set_favorite("TR:S0", MediaType.TRACK, False)
    assert sent == []
