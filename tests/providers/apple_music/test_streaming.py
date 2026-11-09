"""Unit tests for the Widevine license request of the Apple Music streaming manager."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.apple_music.streaming import AppleMusicStreamingManager

MODULE = "music_assistant.providers.apple_music.streaming"


def _manager(monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any]) -> tuple[Any, MagicMock]:
    """Return a streaming manager whose license server gives the given answer."""
    provider: Any = MagicMock(_music_app_token="app", _music_user_token="user")
    response = MagicMock()
    response.json = AsyncMock(return_value=answer)
    provider.mass.http_session.post.return_value.__aenter__.return_value = response
    provider.api_client.throttler.acquire.return_value = AsyncMock()
    cdm = MagicMock()
    cdm.open.return_value = "session"
    cdm.get_license_challenge.return_value = b"challenge"
    cdm.get_keys.return_value = [
        MagicMock(type="SIGNING"),
        MagicMock(type="CONTENT", key=bytes.fromhex("aabb")),
    ]
    monkeypatch.setattr(f"{MODULE}.Cdm.from_device", MagicMock(return_value=cdm))
    monkeypatch.setattr(f"{MODULE}.Device", MagicMock())
    return AppleMusicStreamingManager(provider), cdm


async def test_a_license_gives_the_content_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The challenge goes out with the request fields, the content key comes back."""
    manager, cdm = _manager(monkeypatch, {"status": 0, "license": "granted"})

    key, answer = await manager.get_content_key(
        "https://key.example", MagicMock(), {"uri": "u"}, headers={"x-extra": "1"}
    )

    assert (key, answer) == ("aabb", {"status": 0, "license": "granted"})
    post = manager.provider.mass.http_session.post
    assert post.call_args.args == ("https://key.example",)
    assert json.loads(post.call_args.kwargs["data"]) == {
        "challenge": "Y2hhbGxlbmdl",
        "key-system": "com.widevine.alpha",
        "uri": "u",
    }
    assert post.call_args.kwargs["headers"]["x-extra"] == "1"
    assert post.call_args.kwargs["headers"]["media-user-token"] == "user"
    cdm.parse_license.assert_called_once_with("session", "granted")
    cdm.close.assert_called_once_with("session")


async def test_an_answer_without_a_license_closes_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal inside a successful answer is an error, and the CDM session is closed."""
    manager, cdm = _manager(monkeypatch, {"status": 1, "errorCode": 5})

    with pytest.raises(MediaNotFoundError, match="status 1, error 5"):
        await manager.get_content_key("https://key.example", MagicMock(), {})

    cdm.close.assert_called_once_with("session")
