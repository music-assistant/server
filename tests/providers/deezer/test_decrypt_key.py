"""Test validation of the Deezer decryption key."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from music_assistant_models.errors import AudioError

from music_assistant.providers.deezer.streaming import DeezerStreamingManager

APP_VAR = "music_assistant.providers.deezer.streaming.app_var"
TRACK_ID = "3135556"


@pytest.mark.parametrize("secret", ["", "short", "0" * 17])
def test_missing_decrypt_key_is_reported(monkeypatch: pytest.MonkeyPatch, secret: str) -> None:
    """A secret that is missing or truncated must not end up as an IndexError."""
    monkeypatch.setattr(APP_VAR, lambda _name: secret)
    streaming = DeezerStreamingManager(Mock())

    with pytest.raises(AudioError, match="Deezer decryption key is missing or invalid"):
        streaming._get_blowfish_key(TRACK_ID)


def test_decrypt_key_is_still_derived(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid secret produces the expected track key."""
    monkeypatch.setattr(APP_VAR, lambda _name: "0" * 16)
    streaming = DeezerStreamingManager(Mock())

    assert streaming._get_blowfish_key(TRACK_ID) == ";h37<nkdeo365:e8"


async def test_missing_key_fails_before_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject encrypted playback before fetching audio or logging a listen."""
    monkeypatch.setattr(APP_VAR, lambda _name: "")
    provider = Mock()
    streaming = DeezerStreamingManager(provider)
    details = Mock(data={"track_id": TRACK_ID})

    with pytest.raises(AudioError, match="Deezer decryption key is missing or invalid"):
        await anext(streaming._stream_encrypted_track(details))

    provider.mass.http_session.get.assert_not_called()
    provider.mass.create_task.assert_not_called()
