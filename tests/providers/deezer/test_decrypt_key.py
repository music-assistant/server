"""Test that a build without the bundled decrypt key says so."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from music_assistant_models.errors import AudioError

from music_assistant.providers.deezer.streaming import DeezerStreamingManager

APP_VAR = "music_assistant.providers.deezer.streaming.app_var"
TRACK_ID = "3135556"


@pytest.mark.parametrize("secret", ["", "short"])
def test_missing_decrypt_key_is_reported(monkeypatch: pytest.MonkeyPatch, secret: str) -> None:
    """A secret that is missing or truncated must not end up as an IndexError."""
    monkeypatch.setattr(APP_VAR, lambda _name: secret)
    streaming = DeezerStreamingManager(Mock())

    with pytest.raises(AudioError, match="No Deezer decrypt key"):
        streaming._get_blowfish_key(TRACK_ID)


def test_decrypt_key_is_still_derived(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guards against a check that rejects a perfectly good secret."""
    monkeypatch.setattr(APP_VAR, lambda _name: "0" * 16)
    streaming = DeezerStreamingManager(Mock())

    assert len(streaming._get_blowfish_key(TRACK_ID)) == 16
