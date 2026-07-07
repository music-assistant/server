"""Tests for redirect URL validation and auth-code redirect building."""

from __future__ import annotations

from music_assistant.helpers.redirect_validation import build_code_redirect_url


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
