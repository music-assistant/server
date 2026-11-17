"""Tests for the OAuth hosted-bounce helpers."""

from __future__ import annotations

import base64
import re

import pytest

from music_assistant.helpers.oauth import HOSTED_CALLBACK_URL, hosted_bounce_redirect


@pytest.mark.parametrize(
    "callback_url",
    [
        "http://100.100.1.191:8095/setup_flow/callback/abc123",
        "https://ma.example.com/prefix/setup_flow/callback/x",
        "http://münchen.local:8095/callback/abc123",
    ],
)
def test_hosted_bounce_redirect_encodes_state(callback_url: str) -> None:
    """The callback URL is carried as unpadded base64url, never as a raw URL."""
    redirect_uri, state = hosted_bounce_redirect(callback_url)

    assert redirect_uri == HOSTED_CALLBACK_URL
    assert re.fullmatch(r"[A-Za-z0-9_-]+", state)
    assert "http" not in state
    padded = state + "=" * (-len(state) % 4)
    assert base64.urlsafe_b64decode(padded).decode() == callback_url
