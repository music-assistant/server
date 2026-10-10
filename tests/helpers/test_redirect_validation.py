"""Tests for redirect URL validation and auth-code redirect building."""

from __future__ import annotations

import pytest

from music_assistant.helpers.redirect_validation import (
    build_code_redirect_url,
    is_allowed_redirect_url,
)


def test_build_code_redirect_url_plain() -> None:
    """A code query param is appended to a URL without an existing query or fragment."""
    assert (
        build_code_redirect_url("https://example.com/cb", "tok")
        == "https://example.com/cb?code=tok"
    )


def test_build_code_redirect_url_existing_query() -> None:
    """The code param is joined with & when the URL already has a query string."""
    assert (
        build_code_redirect_url("https://example.com/cb?foo=bar", "tok")
        == "https://example.com/cb?foo=bar&code=tok"
    )


def test_build_code_redirect_url_inserts_before_fragment() -> None:
    """The code param lands in the query string, before any hash fragment."""
    assert (
        build_code_redirect_url("https://example.com/#/onboard", "tok")
        == "https://example.com/?code=tok#/onboard"
    )


def test_build_code_redirect_url_fragment_with_existing_query() -> None:
    """A URL with both query and fragment keeps the code param in the query part."""
    assert (
        build_code_redirect_url("https://example.com/?a=1#/x", "tok")
        == "https://example.com/?a=1&code=tok#/x"
    )


def test_build_code_redirect_url_extra_params() -> None:
    """Extra params are appended after the code param, with keys and values URL-encoded."""
    assert (
        build_code_redirect_url("https://example.com/cb", "tok", {"onboard": "true"})
        == "https://example.com/cb?code=tok&onboard=true"
    )
    assert (
        build_code_redirect_url("https://example.com/cb", "tok", {"a key": "a value"})
        == "https://example.com/cb?code=tok&a%20key=a%20value"
    )


def test_build_code_redirect_url_trailing_separator() -> None:
    """A trailing ? or & in the URL is reused instead of adding another separator."""
    assert (
        build_code_redirect_url("https://example.com/cb?", "tok")
        == "https://example.com/cb?code=tok"
    )
    assert (
        build_code_redirect_url("https://example.com/cb?foo=bar&", "tok")
        == "https://example.com/cb?foo=bar&code=tok"
    )


def test_build_code_redirect_url_encodes_token() -> None:
    """Reserved characters in the token are URL-encoded."""
    assert (
        build_code_redirect_url("https://example.com/cb", "a b/c")
        == "https://example.com/cb?code=a%20b%2Fc"
    )


@pytest.mark.parametrize(
    ("url", "external_url", "expected"),
    [
        ("https://ma.example.com/#/home", "https://ma.example.com", (True, "trusted")),
        ("https://example.com/ma/#/home", "https://example.com/ma", (True, "trusted")),
        ("https://ma.example.com/#/home", None, (True, "external")),
        ("https://other.example.com/#/home", "https://ma.example.com", (True, "external")),
        ("http://ma.example.com/#/home", "https://ma.example.com", (True, "external")),
        ("https://app.music-assistant.io/#/home", "https://ma.example.com", (True, "external")),
    ],
)
def test_is_allowed_redirect_url_trusts_the_external_url(
    url: str, external_url: str | None, expected: tuple[bool, str]
) -> None:
    """A redirect to the configured External URL is trusted, any other public host is not."""
    assert (
        is_allowed_redirect_url(url, base_url="http://192.168.1.10:8095", external_url=external_url)
        == expected
    )
