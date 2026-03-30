"""Unit tests for QQ Music api_client helpers."""

from __future__ import annotations

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.qqmusic.api_client import (
    extract_uin,
    parse_cookie_header,
    parse_credential_fields,
    validate_cookie_shape,
)


def test_parse_cookie_header_valid() -> None:
    """Parse a valid cookie header to dict."""
    cookie = "uin=o12345; qqmusic_key=abc; qm_keyst=abc; foo=bar"
    parsed = parse_cookie_header(cookie)
    assert parsed["uin"] == "o12345"
    assert parsed["qqmusic_key"] == "abc"
    assert parsed["foo"] == "bar"


def test_parse_cookie_header_empty_raises() -> None:
    """Empty cookie header should raise."""
    with pytest.raises(InvalidDataError):
        parse_cookie_header("")


def test_extract_uin_strips_o_prefix() -> None:
    """QQ cookies often prefix uin with 'o'."""
    assert extract_uin({"uin": "o493355621"}) == "493355621"


def test_validate_cookie_shape_missing_session_token_raises() -> None:
    """Cookie without session key should fail validation."""
    with pytest.raises(InvalidDataError):
        validate_cookie_shape({"uin": "493355621"})


def test_parse_credential_fields_uses_qm_keyst_and_login_type() -> None:
    """Credential fields should be extracted from cookie."""
    cookie = "uin=o493355621; qm_keyst=test_key; tmeLoginType=2"
    uin, musickey, login_type = parse_credential_fields(cookie)
    assert uin == 493355621
    assert musickey == "test_key"
    assert login_type == 2


def test_parse_credential_fields_default_login_type() -> None:
    """Missing or invalid tmeLoginType should default to 2."""
    cookie = "uin=493355621; qqmusic_key=test_key; tmeLoginType=invalid"
    uin, musickey, login_type = parse_credential_fields(cookie)
    assert uin == 493355621
    assert musickey == "test_key"
    assert login_type == 2
