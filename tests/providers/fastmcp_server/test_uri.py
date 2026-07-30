"""Tests for ``provider.resources._uri.parse_resource_uri``."""

from __future__ import annotations

import pytest

from music_assistant.providers.fastmcp_server.resources._uri import ResourceURI, parse_resource_uri


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("library://artist/123", ResourceURI("library", "artist", "123")),
        ("library://album/abc-DEF_42", ResourceURI("library", "album", "abc-DEF_42")),
        ("library://track/track%3A1", ResourceURI("library", "track", "track%3A1")),
        ("library://playlist/p1", ResourceURI("library", "playlist", "p1")),
        ("library://radio/r:1", ResourceURI("library", "radio", "r:1")),
        ("library://podcast/p1", ResourceURI("library", "podcast", "p1")),
        ("library://audiobook/a1", ResourceURI("library", "audiobook", "a1")),
        ("player://kitchen", ResourceURI("player", None, "kitchen")),
        ("player://livingroom_sonos", ResourceURI("player", None, "livingroom_sonos")),
        ("queue://q1", ResourceURI("queue", None, "q1")),
    ],
)
def test_parse_valid(uri: str, expected: ResourceURI) -> None:
    """Valid URIs round-trip into ResourceURI."""
    assert parse_resource_uri(uri) == expected


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "noscheme",
        "ftp://library/x",
        "library://",
        "library:///",
        "library://artist/",
        "library://artist/../etc/passwd",
        "library://artist/has spaces",
        "library://unknownkind/x",
        "player://",
        "queue://",
    ],
)
def test_parse_invalid(uri: str) -> None:
    """Invalid URIs raise ``ValueError``."""
    with pytest.raises(ValueError, match=r".+"):
        parse_resource_uri(uri)


def test_path_traversal_rejected_in_id() -> None:
    """Two-dot sequences anywhere in the body are rejected as a defence-in-depth measure."""
    with pytest.raises(ValueError, match=r".+"):
        parse_resource_uri("library://artist/..foo")
