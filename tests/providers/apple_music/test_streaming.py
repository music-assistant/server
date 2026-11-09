"""Unit tests for the Widevine license request of the Apple Music streaming manager."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.apple_music.streaming import (
    AppleMusicStreamingManager,
    LicenseRefusedError,
)

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
        "https://key.example", MagicMock(), {"uri": "u"}, "song 1", headers={"x-extra": "1"}
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
    # the answer is read whatever content type the license server declares
    response = post.return_value.__aenter__.return_value
    assert response.json.await_args.kwargs["content_type"] is None


async def test_an_answer_without_a_license_closes_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal inside a successful answer is an error, and the CDM session is closed."""
    manager, cdm = _manager(monkeypatch, {"status": 1, "errorCode": 5})

    with pytest.raises(LicenseRefusedError, match=r"song 1 \(status 1, error 5\)") as raised:
        await manager.get_content_key("https://key.example", MagicMock(), {}, "song 1")

    assert (raised.value.status, raised.value.error_code) == (1, 5)
    cdm.close.assert_called_once_with("session")


@pytest.mark.parametrize(("check_status", "refused"), [(True, True), (False, False)])
async def test_a_non_zero_status_with_a_license_is_refused_only_when_asked(
    monkeypatch: pytest.MonkeyPatch, check_status: bool, refused: bool
) -> None:
    """The live radio treats any non-zero status as a refusal, songs keep the old check."""
    manager, cdm = _manager(monkeypatch, {"status": 2, "license": "granted"})

    if refused:
        with pytest.raises(LicenseRefusedError):
            await manager.get_content_key(
                "https://key.example", MagicMock(), {}, "live radio", check_status=check_status
            )
        cdm.parse_license.assert_not_called()
    else:
        key, _ = await manager.get_content_key(
            "https://key.example", MagicMock(), {}, "song 1", check_status=check_status
        )
        assert key == "aabb"


async def test_an_empty_answer_is_an_error_naming_the_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty body reads as None, which is no license answer at all."""
    manager, cdm = _manager(monkeypatch, {})
    response = manager.provider.mass.http_session.post.return_value.__aenter__.return_value
    response.json = AsyncMock(return_value=None)

    with pytest.raises(MediaNotFoundError, match="license answer for song 1") as err:
        await manager.get_content_key("https://key.example", MagicMock(), {}, "song 1")

    assert not isinstance(err.value, LicenseRefusedError)
    cdm.close.assert_called_once_with("session")


async def test_a_license_without_a_content_key_names_the_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A license that holds no content key is an error naming the item, not a refusal."""
    manager, cdm = _manager(monkeypatch, {"status": 0, "license": "granted"})
    cdm.get_keys.return_value = [MagicMock(type="SIGNING")]

    with pytest.raises(MediaNotFoundError, match="Unable to get decryption key for song 1") as err:
        await manager.get_content_key("https://key.example", MagicMock(), {}, "song 1")

    assert not isinstance(err.value, LicenseRefusedError)
