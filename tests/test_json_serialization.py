"""Tests for JSON serialization helpers."""

from music_assistant_models.streamdetails import MultiPartPath

from music_assistant.helpers.json import json_dumps


def test_multipartpath_list_serializes() -> None:
    """Ensure a list of MultiPartPath serializes to JSON."""
    data = [MultiPartPath("http://a"), MultiPartPath("b", 12.3)]
    json_str = json_dumps(data)
    assert "http://a" in json_str
    assert "12.3" in json_str
