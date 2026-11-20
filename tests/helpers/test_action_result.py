"""Tests for what the result of a provider's action may hand to a client."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from music_assistant_models.config_entries import ConfigActionResult
from music_assistant_models.enums import ProviderTier

from music_assistant.helpers.action_result import without_insecure_open_url


def _provider(tier: ProviderTier) -> Any:
    """Return a stand-in provider instance of the given tier."""
    return SimpleNamespace(instance_id="frame--1", manifest=SimpleNamespace(tier=tier))


@pytest.mark.parametrize(
    ("tier", "url", "kept"),
    [
        (ProviderTier.COMMUNITY, "https://example.org/connect", True),
        (ProviderTier.COMMUNITY, "HTTPS://example.org/connect", True),
        (ProviderTier.COMMUNITY, "http://example.org/connect", False),
        (ProviderTier.COMMUNITY, "ftp://example.org/connect", False),
        (ProviderTier.COMMUNITY, "/mcp/connect", False),
        (ProviderTier.CORE, "http://192.168.1.5:8095/mcp/connect", True),
    ],
)
def test_open_url_https_only_for_community_providers(
    caplog: pytest.LogCaptureFixture, tier: ProviderTier, url: str, kept: bool
) -> None:
    """A community provider's url is only opened over https; a built-in one keeps http."""
    answered = ConfigActionResult(message="done", open_url=url)

    with caplog.at_level(logging.WARNING):
        result = without_insecure_open_url(_provider(tier), answered)

    assert result.open_url == (url if kept else None)
    assert result.message == "done"
    assert ("not https" in caplog.text) is not kept


def test_result_of_the_provider_never_changed() -> None:
    """A url is dropped on a copy, so a result the provider hands to others keeps it."""
    answered = ConfigActionResult(open_url="http://example.org/connect")

    result = without_insecure_open_url(_provider(ProviderTier.COMMUNITY), answered)

    assert result is not answered
    assert answered.open_url == "http://example.org/connect"
    kept = ConfigActionResult(open_url="https://example.org/connect")
    assert without_insecure_open_url(_provider(ProviderTier.COMMUNITY), kept) is kept
