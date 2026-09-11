"""Test the cookie handling of the YouTube Music setup."""

import pytest
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.ytmusic.helpers import build_headers, normalize_cookie

RAW_COOKIE = "VISITOR_INFO1_LIVE=abc; __Secure-3PAPISID=secret/value; SID=xyz"


def test_raw_header_value_is_kept() -> None:
    """The plain Cookie header value passes through unchanged (apart from whitespace)."""
    assert normalize_cookie(f"  {RAW_COOKIE}\n") == RAW_COOKIE


def test_header_line_prefix_is_stripped() -> None:
    """A header copied including its name loses the 'Cookie:' prefix."""
    assert normalize_cookie(f"Cookie: {RAW_COOKIE}") == RAW_COOKIE
    assert normalize_cookie(f"cookie:{RAW_COOKIE}") == RAW_COOKIE


def test_wrapped_header_is_rejoined() -> None:
    """A header value pasted over several lines is rejoined into one."""
    wrapped = "VISITOR_INFO1_LIVE=abc;\n__Secure-3PAPISID=secret/value; \nSID=xyz"
    assert normalize_cookie(wrapped) == RAW_COOKIE


@pytest.mark.parametrize(
    "command",
    [
        # bash / zsh (Chrome & Firefox on macOS/Linux)
        "curl 'https://music.youtube.com/youtubei/v1/browse' \\\n"
        "  -H 'accept: */*' \\\n"
        f"  -H 'cookie: {RAW_COOKIE}' \\\n"
        "  -H 'x-origin: https://music.youtube.com'",
        # PowerShell (double quotes)
        'curl "https://music.youtube.com/youtubei/v1/browse" '
        f'-H "Cookie: {RAW_COOKIE}" -H "accept: */*"',
        # Windows cmd (caret-escaped quotes)
        'curl ^"https://music.youtube.com/youtubei/v1/browse^" ^\n'
        f'  -H ^"cookie: {RAW_COOKIE}^" ^\n'
        '  -H ^"accept: */*^"',
        # the -b/--cookie option form
        f"curl https://music.youtube.com/ -b '{RAW_COOKIE}' -H 'accept: */*'",
    ],
)
def test_copy_as_curl_command(command: str) -> None:
    """The cookie is extracted from a browser's 'Copy as cURL' command."""
    assert normalize_cookie(command) == RAW_COOKIE


def test_copy_as_curl_cmd_unescapes_carets() -> None:
    """The cmd variant's caret escapes inside the value are removed, not stored."""
    command = (
        'curl ^"https://music.youtube.com/^" ^\n'
        '  -H ^"cookie: PREF=f6=40000000^&tz=Europe.Amsterdam; __Secure-3PAPISID=a^%b^|c^" ^\n'
        '  -H ^"accept: */*^"'
    )
    assert normalize_cookie(command) == (
        "PREF=f6=40000000&tz=Europe.Amsterdam; __Secure-3PAPISID=a%b|c"
    )


def test_netscape_export() -> None:
    """A cookies.txt export yields the youtube.com cookies, HttpOnly marker included."""
    cookies_txt = (
        "# Netscape HTTP Cookie File\n"
        "# https://curl.haxx.se/rfc/cookie_spec.html\n"
        "\n"
        ".youtube.com\tTRUE\t/\tTRUE\t1790000000\tVISITOR_INFO1_LIVE\tabc\n"
        "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1790000000\t__Secure-3PAPISID\tsecret/value\n"
        ".google.com\tTRUE\t/\tTRUE\t1790000000\tNID\tignored\n"
        "music.youtube.com\tTRUE\t/\tTRUE\t0\tSID\txyz\n"
    )
    assert normalize_cookie(cookies_txt) == RAW_COOKIE


def test_empty_paste() -> None:
    """An empty paste normalizes to an empty string rather than failing."""
    assert normalize_cookie("  \n") == ""


def test_build_headers_signs_the_request() -> None:
    """A complete cookie yields the SAPISIDHASH authorization YouTube expects."""
    headers = build_headers(RAW_COOKIE)
    assert headers["Cookie"] == RAW_COOKIE
    assert headers["Authorization"].startswith("SAPISIDHASH ")


def test_build_headers_rejects_signed_out_cookie() -> None:
    """A cookie without __Secure-3PAPISID is refused with the localized key."""
    with pytest.raises(LoginFailed) as exc_info:
        build_headers("VISITOR_INFO1_LIVE=abc; SID=xyz")
    assert exc_info.value.translation_key == "cookie_missing_sapisid"


def test_build_headers_rejects_unparsable_cookie() -> None:
    """A cookie SimpleCookie cannot parse (a stray space in a value) is reported, not crashed on."""
    with pytest.raises(LoginFailed) as exc_info:
        build_headers("VISITOR_INFO1_LIVE=abc; __Secure-3PAPISID=secret value; SID=xyz")
    assert exc_info.value.translation_key == "cookie_malformed"
