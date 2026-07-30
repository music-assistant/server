"""Tests for the pure helper functions of the Metadata Controller."""

import pytest

from music_assistant.controllers.metadata.helpers import (
    _detect_image_format,
    _normalize_imageproxy_format,
)


class TestDetectImageFormat:
    """`_detect_image_format` maps a path/extension to a served image format."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("cover.svg", "svg"),
            ("cover.png", "png"),
            ("cover.jpg", "jpg"),
            ("cover.jpeg", "jpg"),
            ("cover.webp", "jpg"),
            ("noextension", "jpg"),
            ("http://host/path/IMAGE.PNG", "png"),
            ("http://host/path/art.png?cs=abc123", "png"),
        ],
    )
    def test_detect(self, path: str, expected: str) -> None:
        """The extension (after stripping any query suffix) drives the format."""
        assert _detect_image_format(path) == expected


class TestNormalizeImageproxyFormat:
    """`_normalize_imageproxy_format` validates the client-supplied `fmt` value."""

    @pytest.mark.parametrize("value", ["jpg", "JPG", " png ", "jpeg", "svg"])
    def test_valid(self, value: str) -> None:
        """Known formats normalize to their lowercase, trimmed form."""
        assert _normalize_imageproxy_format(value) == value.strip().lower()

    @pytest.mark.parametrize("value", [None, "", "gif", "bmp", "exe"])
    def test_invalid(self, value: str | None) -> None:
        """Unknown or empty formats return None."""
        assert _normalize_imageproxy_format(value) is None
