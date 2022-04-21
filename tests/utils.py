"""Tests for utility functions."""

from music_assistant.helpers import util


def test_version_extract():
    """Test the extraction of version from title."""

    test_str = "Bam Bam (feat. Ed Sheeran) - Karaoke Version"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Bam Bam"
    assert version == "Karaoke Version"
