"""Tests for redirect URL validation and auth-code redirect building."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import make_mocked_request

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


@pytest.mark.parametrize(
    "url",
    [
        # backslash in the authority and in the path
        "http://evil.example\\@192.168.1.5/",
        "http://evil.example\\@localhost:8095/x",
        "https://evil.example\\@127.0.0.1/",
        "http://192.168.1.10:8095\\evil.example/",
        "http://192.168.1.10:8095/\\evil.example/",
        "https://my.home-assistant.io/\\evil.example",
        "musicassistant://auth\\callback",
        # userinfo, with and without a password
        "http://evil.example@192.168.1.5/",
        "http://user@localhost:8095/",
        "http://user:pass@127.0.0.1/",
        "https://user:pass@ma.example.com/",
        "http://evil%5C@10.0.0.1/",
        "http://@10.0.0.1/",
        # control characters and whitespace
        "http://evil.example\t@10.0.0.1/",
        "http://10.0.0.1/\tx",
        "http://10.0.0.1\r\n/",
        "http://10.0.0.1/\n",
        "http://10.0.0.1/a\x00b",
        "http://10.0.0.1/\x1f",
        "http://10.0.0.1/\x7f",
        " http://10.0.0.1/",
        "http://10.0.0.1/ ",
        "http://10.0.0.1/a b",
        "\thttps://my.home-assistant.io/",
    ],
)
def test_is_allowed_redirect_url_blocks_ambiguous_urls(url: str) -> None:
    """URLs that browsers and urlparse read differently are refused outright."""
    assert is_allowed_redirect_url(
        url, base_url="https://ma.example.com", external_url="https://ma.example.com"
    ) == (False, "blocked")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("musicassistant://auth/callback", (True, "trusted")),
        ("https://my.home-assistant.io/redirect/oauth?x=1", (True, "trusted")),
        ("http://homeassistant.local/", (True, "trusted")),
        ("https://homeassistant.local/lovelace", (True, "trusted")),
        ("http://localhost:8095/#/home", (True, "trusted")),
        ("http://127.0.0.1:8095/?a=1&b=2", (True, "trusted")),
        ("http://[::1]:8095/x?a=1", (True, "trusted")),
        ("http://[fd00::1]:8095/", (True, "trusted")),
        ("http://192.168.1.20:8095/path?q=1#/frag", (True, "trusted")),
        ("http://10.0.0.1/", (True, "trusted")),
        ("https://ma.example.com/#/home", (True, "trusted")),
        ("https://other.example.com/cb?a=%40b%5C", (True, "external")),
        ("https://other.example.com/cb?email=a@b.example", (True, "external")),
        ("/relative/path", (False, "blocked")),
        ("javascript:alert(1)", (False, "blocked")),
    ],
)
def test_is_allowed_redirect_url_keeps_legitimate_urls(
    url: str, expected: tuple[bool, str]
) -> None:
    """Legitimate return URLs keep their classification."""
    assert (
        is_allowed_redirect_url(
            url, base_url="http://192.168.1.10:8095", external_url="https://ma.example.com"
        )
        == expected
    )


def test_is_allowed_redirect_url_same_origin_request() -> None:
    """A same-origin URL stays trusted, one with userinfo is refused."""
    request = make_mocked_request("GET", "/", headers={"Host": "ma.example.com:8095"})
    assert is_allowed_redirect_url("https://ma.example.com:8095/#/home", request) == (
        True,
        "trusted",
    )
    assert is_allowed_redirect_url("https://evil.example@ma.example.com:8095/", request) == (
        False,
        "blocked",
    )
