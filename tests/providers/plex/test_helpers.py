"""Test Plex provider helper functions."""

from unittest.mock import Mock

import pytest

from music_assistant.providers.plex.helpers import (
    get_explicit,
    get_musicbrainz_id,
    parse_plex_lyrics_payload,
)

SYNCED_JSON = (
    '{"MediaContainer": {"Lyrics": [{"Line": ['
    '{"Span": [{"text": "Hello ", "startOffset": 12000},'
    ' {"text": "world", "startOffset": 12000}]},'
    '{"Span": [{"text": "second line", "startOffset": 15500}]}'
    "]}]}}"
)

LRC_TEXT = "[00:12.00]Hello world\n[00:15.50]second line\n"

PLAIN_TEXT = "Hello world\nsecond line"

VALID_MBID = "b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d"


def test_parse_lyrics_structured_json_synced() -> None:
    """Structured Plex JSON with offsets parses to a synced LRC string."""
    result = parse_plex_lyrics_payload(SYNCED_JSON)
    assert result == ("[00:12.00]Hello world\n[00:15.50]second line", True)


def test_parse_lyrics_raw_lrc() -> None:
    """Raw LRC text is detected as synced and returned verbatim (stripped)."""
    assert parse_plex_lyrics_payload(LRC_TEXT) == (LRC_TEXT.strip(), True)


def test_parse_lyrics_plain_text() -> None:
    """Plain text without timestamps is returned as unsynced lyrics."""
    assert parse_plex_lyrics_payload(PLAIN_TEXT) == (PLAIN_TEXT, False)


@pytest.mark.parametrize("content", ["", "   ", "\n\n"])
def test_parse_lyrics_empty(content: str) -> None:
    """Empty payloads yield no lyrics."""
    assert parse_plex_lyrics_payload(content) is None


def test_parse_lyrics_json_unsynced() -> None:
    """Structured JSON without offsets falls back to plain unsynced text."""
    payload = '{"MediaContainer": {"Lyrics": [{"Line": [{"Span": [{"text": "no timing"}]}]}]}}'
    assert parse_plex_lyrics_payload(payload) == ("no timing", False)


def test_parse_lyrics_malformed_json_as_plain() -> None:
    """Malformed JSON that is not LRC is treated as plain text."""
    assert parse_plex_lyrics_payload("{not valid json") == ("{not valid json", False)


def test_parse_lyrics_long_offset_no_minute_wrap() -> None:
    """Offsets beyond one hour keep counting minutes instead of wrapping at 60."""
    payload = (
        '{"MediaContainer": {"Lyrics": [{"Line": ['
        '{"Span": [{"text": "late line", "startOffset": 3661230}]}'
        "]}]}}"
    )
    assert parse_plex_lyrics_payload(payload) == ("[61:01.23]late line", True)


def _guid_elem(guid_id: str) -> Mock:
    elem = Mock()
    elem.attrib = {"id": guid_id}
    return elem


def _plex_obj(
    *,
    guids: list[str] | None = None,
    attrib: dict[str, str] | None = None,
) -> Mock:
    obj = Mock()
    obj._data.findall.return_value = [_guid_elem(guid_id) for guid_id in (guids or [])]
    obj._data.attrib = attrib or {}
    return obj


def test_get_musicbrainz_id_from_guid() -> None:
    """A mbid:// guid yields the bare MusicBrainz identifier."""
    obj = _plex_obj(guids=["plex://album/abc", f"mbid://{VALID_MBID}"])
    assert get_musicbrainz_id(obj) == VALID_MBID


def test_get_musicbrainz_id_no_mbid() -> None:
    """Objects without a mbid:// guid return None."""
    obj = _plex_obj(guids=["plex://album/abc"])
    assert get_musicbrainz_id(obj) is None


@pytest.mark.parametrize(
    ("content_rating", "expected"),
    [("explicit", True), ("Explicit", True), ("clean", False), ("", None), (None, None)],
)
def test_get_explicit(content_rating: str | None, expected: bool | None) -> None:
    """Content rating maps to explicit only for the 'explicit' value."""
    attrib = {"contentRating": content_rating} if content_rating is not None else {}
    assert get_explicit(_plex_obj(attrib=attrib)) is expected
