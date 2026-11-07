"""Tests for the ICY metadata frame helper."""

from __future__ import annotations

import pytest

from music_assistant.helpers.audio import format_icy_metadata_frame


def _parse_icy_frame(frame: bytes) -> bytes:
    """
    Return the metadata payload of an ICY frame, stripping length prefix and padding.

    :param frame: Raw ICY metadata frame.
    :return: Inner metadata bytes with trailing NULs removed.
    """
    assert len(frame) >= 1
    expected_payload_len = frame[0] * 16
    payload = frame[1:]
    assert len(payload) == expected_payload_len, (
        f"length byte {frame[0]} disagrees with payload length {len(payload)}"
    )
    return payload.rstrip(b"\x00")


def test_basic_title_only() -> None:
    """A title-only frame round-trips through the parser."""
    frame = format_icy_metadata_frame("Hello World")
    assert _parse_icy_frame(frame) == b"StreamTitle='Hello World';"


def test_with_image_url() -> None:
    """Both StreamTitle and StreamURL fields appear when an image is given."""
    frame = format_icy_metadata_frame("Title", "https://example.com/art.jpg")
    payload = _parse_icy_frame(frame)
    assert payload == b"StreamTitle='Title';StreamURL='https://example.com/art.jpg'"


@pytest.mark.parametrize(
    "title",
    [
        "",
        "a",
        "x" * 15,
        "y" * 16,
        "z" * 17,
        "long-ish title with spaces and punctuation, etc.",
    ],
)
def test_padding_aligns_to_16_bytes(title: str) -> None:
    """The frame is always a multiple of 16 bytes after the length byte."""
    frame = format_icy_metadata_frame(title)
    payload_len = len(frame) - 1
    assert payload_len % 16 == 0
    assert payload_len // 16 == frame[0]


def test_no_image_when_url_is_none() -> None:
    """No StreamURL field is added when image_url is None."""
    frame = format_icy_metadata_frame("Just a title", None)
    payload = _parse_icy_frame(frame)
    assert b"StreamURL" not in payload
    assert payload == b"StreamTitle='Just a title';"
