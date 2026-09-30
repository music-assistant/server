"""Tests for the ssl helpers."""

from __future__ import annotations

import ssl
from typing import TYPE_CHECKING

from music_assistant.helpers import ssl as ssl_helper

if TYPE_CHECKING:
    import pytest


def test_client_contexts_advertise_http11_alpn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verified and unverified client contexts advertise http/1.1 via ALPN."""
    advertised: dict[int, list[str]] = {}
    original = ssl.SSLContext.set_alpn_protocols

    def _spy(self: ssl.SSLContext, protocols: list[str]) -> None:
        advertised[id(self)] = list(protocols)
        original(self, protocols)

    monkeypatch.setattr(ssl.SSLContext, "set_alpn_protocols", _spy)
    ssl_helper._client_context_no_verify.cache_clear()
    contexts = (
        ssl_helper.create_client_context(),
        ssl_helper.create_client_context(ssl_helper.SSLCipherList.MODERN),
        ssl_helper.create_no_verify_ssl_context(),
        ssl_helper.create_no_verify_ssl_context(ssl_helper.SSLCipherList.INSECURE),
    )
    for context in contexts:
        assert advertised.get(id(context)) == ["http/1.1"]
